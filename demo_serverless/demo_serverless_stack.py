import json

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from . import suppressions

# These mirror functions/ids.py; a unit test checks they stay in sync.
UPLOAD_PREFIX = "uploads/"
RESULT_PREFIX = "results/"
UPLOAD_SUFFIX = ".txt"

LOG_RETENTION = logs.RetentionDays.ONE_WEEK
STAGE_RATE_LIMIT = 10
STAGE_BURST_LIMIT = 20
UPLOAD_ROUTE_RATE_LIMIT = 1
UPLOAD_ROUTE_BURST_LIMIT = 2

PROJECT_TAG = ("project", "demo-serverless")

# Construct ids of the two helpers CDK adds on its own (see _manage_helper_log_groups).
# find_child raises if a CDK upgrade renames them, so this can't fail silently.
AUTO_DELETE_PROVIDER_ID = "Custom::S3AutoDeleteObjectsCustomResourceProvider"
NOTIFICATIONS_HANDLER_ID = "BucketNotificationsHandler050a0587b7544547bf325f094a3db834"


class DemoServerlessStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        reserved_concurrency: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.api = apigwv2.HttpApi(
            self,
            "Api",
            api_name="demo-serverless",
            create_default_stage=False,
        )
        # The S3 event helper gets our own role and log group, created first because the
        # bucket takes the role as a property.
        notifications_logs = self._create_log_group("NotificationsHelperLogs")
        self.bucket = self._create_bucket(
            self._create_notifications_role(notifications_logs)
        )

        # One function per job, each with only the S3 access it needs.
        self.page_fn = self._create_function(
            "Page", "page.handler", reserved_concurrency
        )
        self.presign_fn = self._create_function(
            "Presign", "presign.handler", reserved_concurrency, timeout_seconds=5
        )
        self.results_fn = self._create_function(
            "Results", "results.handler", reserved_concurrency, timeout_seconds=5
        )
        self.processor_fn = self._create_function(
            "Processor", "processor.handler", reserved_concurrency, timeout_seconds=30
        )
        self._grant_object_access()

        self.bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.processor_fn),
            s3.NotificationKeyFilter(prefix=UPLOAD_PREFIX, suffix=UPLOAD_SUFFIX),
        )

        routes = self._add_routes()
        self._create_stage(routes)
        self._manage_helper_log_groups(notifications_logs)
        self._acknowledge_nag_findings()
        self._skip_checkov_findings(reserved_concurrency)

        Tags.of(self).add(*PROJECT_TAG)

        cdk.CfnOutput(self, "SiteUrl", value=self.api.api_endpoint)
        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)

    def _create_log_group(self, construct_id: str) -> logs.LogGroup:
        """A log group with the project's retention that is deleted with the stack."""
        return logs.LogGroup(
            self,
            construct_id,
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_notifications_role(self, log_group: logs.LogGroup) -> iam.Role:
        """Our own role for CDK's S3-notifications helper, instead of the one CDK builds.

        CDK adds s3:PutBucketNotification on this bucket itself. We add log permissions
        for the helper's own log group only (no logs:CreateLogGroup).
        """
        role = iam.Role(
            self,
            "NotificationsHelperRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group.grant_write(role)
        # CDK attaches AWSLambdaBasicExecutionRole (logs:CreateLogGroup on every log group)
        # to whatever role it is given. Clearing the property on the underlying CfnRole
        # removes it from the template and from what cdk-nag inspects.
        role_cfn = suppressions.cfn_of(role)
        assert isinstance(role_cfn, iam.CfnRole)
        role_cfn.managed_policy_arns = None
        return role

    def _create_bucket(self, notifications_role: iam.Role) -> s3.Bucket:
        return s3.Bucket(
            self,
            "Bucket",
            notifications_handler_role=notifications_role,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            # The browser uploads from the API's origin to the bucket's origin. CORS only
            # lets the page read the response; the presigned policy is the real control.
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST],
                    allowed_origins=[self.api.api_endpoint],
                    max_age=300,
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

    def _create_function(
        self,
        name: str,
        handler: str,
        reserved_concurrency: int,
        timeout_seconds: int = 5,
    ) -> lambda_.Function:
        # Created explicitly: CDK's default log group keeps logs for 2 years and survives destroy.
        log_group = self._create_log_group(f"{name}Logs")
        # Our own role instead of CDK's default, which attaches AWSLambdaBasicExecutionRole
        # (logs:CreateLogGroup on every log group in the account). grant_write adds only
        # CreateLogStream and PutLogEvents on this function's own log group, so the function
        # can never create a log group, and a group we deleted can't be recreated.
        role = iam.Role(
            self,
            f"{name}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group.grant_write(role)
        return lambda_.Function(
            self,
            f"{name}Function",
            role=role,
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler=handler,
            code=lambda_.Code.from_asset("functions", exclude=["__pycache__"]),
            memory_size=128,
            timeout=Duration.seconds(timeout_seconds),
            # 0 means "no reservation": accounts with a small quota can't reserve any.
            reserved_concurrent_executions=reserved_concurrency or None,
            log_group=log_group,
            environment={
                "BUCKET_NAME": self.bucket.bucket_name,
                # Shared by the presigned URL, the page's CSP and (in spirit) the CORS rule.
                "UPLOAD_ORIGIN": f"https://{self.bucket.bucket_regional_domain_name}",
            },
        )

    def _grant_object_access(self) -> None:
        """Explicit statements instead of grant_*: no s3:ListBucket and no bucket-level actions."""
        uploads = self.bucket.arn_for_objects(f"{UPLOAD_PREFIX}*")
        results = self.bucket.arn_for_objects(f"{RESULT_PREFIX}*")
        grants = [
            (self.presign_fn, "s3:PutObject", uploads),
            (self.processor_fn, "s3:GetObject", uploads),
            (self.processor_fn, "s3:PutObject", results),
            (self.results_fn, "s3:GetObject", results),
        ]
        for function, action, resource in grants:
            function.add_to_role_policy(
                iam.PolicyStatement(actions=[action], resources=[resource])
            )

    def _add_routes(self) -> list[apigwv2.HttpRoute]:
        route_specs = [
            ("/", apigwv2.HttpMethod.GET, self.page_fn),
            ("/app.js", apigwv2.HttpMethod.GET, self.page_fn),
            ("/app.css", apigwv2.HttpMethod.GET, self.page_fn),
            ("/uploads", apigwv2.HttpMethod.POST, self.presign_fn),
            ("/results/{id}", apigwv2.HttpMethod.GET, self.results_fn),
        ]
        routes: list[apigwv2.HttpRoute] = []
        for path, method, function in route_specs:
            routes += self.api.add_routes(
                path=path,
                methods=[method],
                integration=HttpLambdaIntegration(
                    f"Integration{function.node.id}", function
                ),
            )
        return routes

    def _create_stage(self, routes: list[apigwv2.HttpRoute]) -> None:
        access_logs = self._create_log_group("AccessLogs")
        stage = apigwv2.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(
                rate_limit=STAGE_RATE_LIMIT, burst_limit=STAGE_BURST_LIMIT
            ),
        )
        # API Gateway rejects RouteSettings for a route that doesn't exist yet, so the
        # stage must be created after the routes.
        stage.node.add_dependency(*routes)
        cfn_stage = stage.node.default_child
        assert isinstance(cfn_stage, apigwv2.CfnStage)
        cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "ip": "$context.identity.sourceIp",
                    "requestTime": "$context.requestTime",
                    "routeKey": "$context.routeKey",
                    "status": "$context.status",
                    "responseLength": "$context.responseLength",
                    "integrationError": "$context.integrationErrorMessage",
                }
            ),
        )
        # Uploads are what costs money when abused, so that route gets a tighter limit.
        # An override because CfnStage.route_settings is untyped and would emit
        # lower-case keys that CloudFormation doesn't recognise.
        cfn_stage.add_property_override(
            "RouteSettings",
            {
                "POST /uploads": {
                    "ThrottlingRateLimit": UPLOAD_ROUTE_RATE_LIMIT,
                    "ThrottlingBurstLimit": UPLOAD_ROUTE_BURST_LIMIT,
                }
            },
        )

    def _manage_helper_log_groups(self, notifications_logs: logs.LogGroup) -> None:
        """Give CDK's two helper functions log groups we own, so nothing is left after destroy.

        CDK adds a function that empties the bucket (auto_delete_objects) and one that
        sets up the S3 event. Both run during destroy. Without this, Lambda creates their
        log groups on the fly with no expiry and no tags, and they outlive the stack.
        The groups get CloudFormation-generated names: that gives the delete order
        custom resource -> function -> log group, and avoids a dependency cycle.
        """
        auto_delete = self.node.find_child(AUTO_DELETE_PROVIDER_ID)
        notifications = self.node.find_child(NOTIFICATIONS_HANDLER_ID)
        auto_delete_logs = self._create_log_group("AutoDeleteHelperLogs")
        for function, log_group in (
            (auto_delete.node.find_child("Handler"), auto_delete_logs),
            (notifications.node.find_child("Resource"), notifications_logs),
        ):
            assert isinstance(function, cdk.CfnResource)
            function.add_property_override(
                "LoggingConfig", {"LogGroup": log_group.log_group_name}
            )

        # The auto-delete provider is built from raw CfnResources, which the Tags
        # aspect skips, so tag its function and role by hand.
        tags = [{"Key": PROJECT_TAG[0], "Value": PROJECT_TAG[1]}]
        for child in ("Handler", "Role"):
            resource = auto_delete.node.find_child(child)
            assert isinstance(resource, cdk.CfnResource)
            resource.add_property_override("Tags", tags)

        # Why a log group was left behind after destroy: the delete order is right
        # (custom resource -> function -> log group), but the helpers send CloudFormation
        # their "done" response from inside their own invocation. CloudFormation then
        # deletes the function and the log group while Lambda is still flushing the
        # invocation's last log lines, and Lambda recreates the group with the role's
        # logs:CreateLogGroup. The notifications helper's role (_create_notifications_role)
        # has no such permission. The auto-delete helper's role is built inside CDK and
        # can't be replaced (CustomResourceProviderProps has no role property), so it keeps
        # AWSLambdaBasicExecutionRole and we deny logs:CreateLogGroup on it. The deny is
        # inline on the role, not a separate policy resource, which CloudFormation could
        # delete first; this way it lasts exactly as long as the allow it overrides.
        deny_create_log_group = [
            {
                "PolicyName": "DenyCreateLogGroup",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Deny",
                            "Action": "logs:CreateLogGroup",
                            "Resource": "*",
                        }
                    ],
                },
            }
        ]
        suppressions.cfn_of(auto_delete.node.find_child("Role")).add_property_override(
            "Policies", deny_create_log_group
        )

    @staticmethod
    def _role_of(function: lambda_.Function) -> iam.Role:
        """The execution role CDK created for the function (never None here)."""
        role = function.role
        assert isinstance(role, iam.Role)
        return role

    def _acknowledge_nag_findings(self) -> None:
        # No IAM4 acknowledgement is needed: none of our roles uses a managed policy. (The
        # auto-delete helper's role does, but cdk-nag never evaluates it: see DESIGN.md.)
        bucket_cfn = suppressions.cfn_of(self.bucket)
        bucket_id = self.get_logical_id(bucket_cfn)
        object_wildcards = {
            f"{UPLOAD_PREFIX}*": [self.presign_fn, self.processor_fn],
            f"{RESULT_PREFIX}*": [self.results_fn, self.processor_fn],
        }
        for pattern, fns in object_wildcards.items():
            suppressions.acknowledge(
                [self._role_of(fn).node.find_child("DefaultPolicy") for fn in fns],
                f"AwsSolutions-IAM5[Resource::<{bucket_id}.Arn>/{pattern}]",
                "Object keys are random ids that can't be listed in advance, so access is "
                f"granted to the {pattern} prefix only, with one action per statement and "
                "no bucket-level permissions.",
            )

        suppressions.acknowledge(
            [bucket_cfn],
            "AwsSolutions-S1",
            "Server access logs need a second bucket with its own retention and teardown. "
            "Objects here are random-named and deleted after 1 day, and Lambda logs record "
            "each processed key.",
        )

        routes = [
            child
            for child in self.api.node.children
            if isinstance(child, apigwv2.HttpRoute)
        ]
        suppressions.acknowledge(
            [suppressions.cfn_of(route) for route in routes],
            "AwsSolutions::AwsSolutions-APIG4",
            "A public demo with no user accounts. Abuse is limited by API throttling, "
            "5-minute presigned links, the 100 KB limit and 1-day object expiry.",
        )

    def _skip_checkov_findings(self, reserved_concurrency: int) -> None:
        own_functions = [
            self.page_fn,
            self.presign_fn,
            self.results_fn,
            self.processor_fn,
        ]
        api_functions = [self.page_fn, self.presign_fn, self.results_fn]
        helpers = [
            self.node.find_child(AUTO_DELETE_PROVIDER_ID).node.find_child("Handler"),
            self.node.find_child(NOTIFICATIONS_HANDLER_ID).node.find_child("Resource"),
        ]

        skip = suppressions.skip_checkov
        skip(
            self.bucket,
            {
                "CKV_AWS_18": "No access logs: needs a second bucket with its own retention "
                "and teardown. Objects are random-named and expire after 1 day.",
                "CKV_AWS_21": "No versioning: data is disposable and expires after 1 day; "
                "versions would only keep deleted or overwritten copies around.",
            },
        )

        for function in own_functions:
            skips = {
                "CKV_AWS_117": "No VPC: the function reaches only S3 and has no private "
                "resources; a VPC would need NAT or endpoints with hourly costs.",
                "CKV_AWS_173": "Environment variables hold only the bucket name and its "
                "public upload URL, no secrets.",
            }
            if function in api_functions:
                skips["CKV_AWS_116"] = (
                    "No DLQ: invoked synchronously by API Gateway, which returns the error "
                    "to the caller."
                )
            else:
                skips["CKV_AWS_116"] = (
                    "No DLQ (decided): failed S3 events are retried twice by Lambda and "
                    "logged; the page shows a clear failure/timeout message."
                )
            if not reserved_concurrency:
                skips["CKV_AWS_115"] = (
                    "Reserved concurrency was turned off (reserved_concurrency=0) for "
                    "accounts whose Lambda quota is too small to reserve any."
                )
            skip(function, skips)

        for helper in helpers:
            skip(
                helper,
                {
                    "CKV_AWS_115": "CDK-managed helper that only runs during deploy and "
                    "destroy; CDK offers no option to reserve concurrency for it.",
                    "CKV_AWS_116": "CDK-managed helper; CloudFormation reports its failures.",
                    "CKV_AWS_117": "CDK-managed helper with no private resources to reach.",
                },
            )

        log_groups = [
            child for child in self.node.children if isinstance(child, logs.LogGroup)
        ]
        for log_group in log_groups:
            skip(
                log_group,
                {
                    "CKV_AWS_158": "CloudWatch Logs already encrypts at rest. A KMS key adds "
                    "a monthly cost and a deletion waiting period after destroy.",
                },
            )
