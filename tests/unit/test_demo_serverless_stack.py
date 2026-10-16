"""Assertions on the synthesized template: the security properties are tested, not assumed.

The stack is built with a placeholder environment: a region and no account. It does no
lookups, so no AWS access or account ID is needed.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import ids
import pytest
from aws_cdk import Validations, assertions
from cdk_nag import AwsSolutionsChecks

from demo_serverless import demo_serverless_stack as stack_module
from demo_serverless.demo_serverless_stack import DemoServerlessStack

CDK_CONTEXT = json.loads((Path(__file__).parents[2] / "cdk.json").read_text())[
    "context"
]
ENV = cdk.Environment(region="us-east-1")
TAG = {"Key": "project", "Value": "demo-serverless"}
FUNCTION_NAMES = ("Page", "Presign", "Results", "Processor")


def build(reserved_concurrency: int = 2) -> tuple[cdk.App, DemoServerlessStack]:
    app = cdk.App(context=CDK_CONTEXT)
    stack = DemoServerlessStack(
        app, "Test", env=ENV, reserved_concurrency=reserved_concurrency
    )
    return app, stack


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    return assertions.Template.from_stack(build()[1])


def resources(
    template: assertions.Template, type_: str
) -> Mapping[str, Mapping[str, Any]]:
    return template.find_resources(type_)


def tag_pairs(properties: dict[str, Any]) -> list[dict[str, str]]:
    """Tags as [{Key, Value}], whether the resource uses a list or a dict."""
    tags = properties["Tags"]
    if isinstance(tags, dict):
        return [{"Key": key, "Value": value} for key, value in tags.items()]
    return tags


def role_statements(template: assertions.Template, role: str) -> list[dict[str, Any]]:
    """The statements in the inline policy of one of our function roles (e.g. "Presign")."""
    statements: list[dict[str, Any]] = []
    for logical_id, policy in resources(template, "AWS::IAM::Policy").items():
        if logical_id.startswith(f"{role}RoleDefaultPolicy"):
            statements += policy["Properties"]["PolicyDocument"]["Statement"]
    return statements


def actions_of(statement: Mapping[str, Any]) -> list[str]:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else list(actions)


def policy_grants(template: assertions.Template, role: str) -> set[tuple[str, str]]:
    """(action, object-key pattern) pairs for the S3 access in the role's inline policy."""
    grants = set()
    for statement in role_statements(template, role):
        for action in actions_of(statement):
            if action.startswith("s3:"):
                # Object ARNs are {"Fn::Join": ["", [bucket-arn, "/uploads/*"]]}
                grants.add((action, statement["Resource"]["Fn::Join"][1][-1]))
    return grants


# --- bucket -------------------------------------------------------------------------


def test_bucket_blocks_all_public_access(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_bucket_is_encrypted_and_acls_are_off(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "OwnershipControls": {
                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
            },
        },
    )


def test_bucket_denies_plain_http_and_old_tls(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        ),
                        assertions.Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {
                                    "NumericLessThan": {"s3:TlsVersion": 1.2}
                                },
                            }
                        ),
                    ]
                )
            }
        },
    )


def test_objects_and_partial_uploads_expire_after_one_day(
    template: assertions.Template,
) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "ExpirationInDays": 1,
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            }
        },
    )


def test_cors_allows_only_post_from_the_api_origin(
    template: assertions.Template,
) -> None:
    (bucket,) = resources(template, "AWS::S3::Bucket").values()
    (rule,) = bucket["Properties"]["CorsConfiguration"]["CorsRules"]
    assert rule["AllowedMethods"] == ["POST"]
    (origin,) = rule["AllowedOrigins"]
    assert origin != "*"
    assert (
        origin["Fn::GetAtt"][1] == "ApiEndpoint"
    )  # the API's own origin, not a wildcard


def test_only_uploads_trigger_the_processor(template: assertions.Template) -> None:
    (notification,) = resources(template, "Custom::S3BucketNotifications").values()
    (config,) = notification["Properties"]["NotificationConfiguration"][
        "LambdaFunctionConfigurations"
    ]
    rules = {r["Name"]: r["Value"] for r in config["Filter"]["Key"]["FilterRules"]}
    assert rules == {"prefix": ids.UPLOAD_PREFIX, "suffix": ids.UPLOAD_SUFFIX}
    assert config["LambdaFunctionArn"]["Fn::GetAtt"][0].startswith("ProcessorFunction")


def test_bucket_is_destroyed_with_its_contents(template: assertions.Template) -> None:
    template.has_resource(
        "AWS::S3::Bucket",
        {"DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete"},
    )
    template.resource_count_is("Custom::S3AutoDeleteObjects", 1)


def test_stack_prefixes_match_the_function_code() -> None:
    assert stack_module.UPLOAD_PREFIX == ids.UPLOAD_PREFIX
    assert stack_module.RESULT_PREFIX == ids.RESULT_PREFIX
    assert stack_module.UPLOAD_SUFFIX == ids.UPLOAD_SUFFIX


# --- functions and IAM ----------------------------------------------------------------


def our_functions(template: assertions.Template) -> list[dict[str, Any]]:
    return [
        function["Properties"]
        for function in resources(template, "AWS::Lambda::Function").values()
        if function["Properties"]["Runtime"] == "python3.14"
    ]


def test_four_python_314_functions_with_reserved_concurrency(
    template: assertions.Template,
) -> None:
    functions = our_functions(template)
    assert len(functions) == len(FUNCTION_NAMES)
    for properties in functions:
        assert properties["ReservedConcurrentExecutions"] == 2
        assert properties["Architectures"] == ["arm64"]


def test_zero_reserved_concurrency_means_no_reservation() -> None:
    template = assertions.Template.from_stack(build(reserved_concurrency=0)[1])
    for properties in our_functions(template):
        assert "ReservedConcurrentExecutions" not in properties


def test_functions_have_exactly_the_s3_access_they_need(
    template: assertions.Template,
) -> None:
    assert policy_grants(template, "Page") == set()
    assert policy_grants(template, "Presign") == {("s3:PutObject", "/uploads/*")}
    assert policy_grants(template, "Results") == {("s3:GetObject", "/results/*")}
    assert policy_grants(template, "Processor") == {
        ("s3:GetObject", "/uploads/*"),
        ("s3:PutObject", "/results/*"),
    }


def test_no_inline_policy_lists_the_bucket_or_uses_wildcard_resources(
    template: assertions.Template,
) -> None:
    for policy in resources(template, "AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement["Effect"] != "Allow":
                continue  # a Deny on "*" only removes access
            for action in actions_of(statement):
                if action.startswith("s3:"):
                    assert not action.startswith("s3:List")
                    assert not action.startswith("s3:GetBucket")
                    assert action != "s3:*"
            assert statement["Resource"] != "*"


def test_our_functions_have_no_managed_policy_and_log_only_to_their_own_group(
    template: assertions.Template,
) -> None:
    roles = resources(template, "AWS::IAM::Role")
    for name in FUNCTION_NAMES:
        (role,) = [
            role
            for logical_id, role in roles.items()
            if logical_id.startswith(f"{name}Role")
        ]
        assert "ManagedPolicyArns" not in role["Properties"]

        log_statements = [
            statement
            for statement in role_statements(template, name)
            if any(action.startswith("logs:") for action in actions_of(statement))
        ]
        (statement,) = log_statements
        assert sorted(actions_of(statement)) == [
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        ]
        # Exactly this function's own log group, not a wildcard
        assert statement["Resource"]["Fn::GetAtt"][0].startswith(f"{name}Logs")


def test_no_policy_in_the_stack_allows_creating_log_groups_for_our_functions(
    template: assertions.Template,
) -> None:
    for name in FUNCTION_NAMES:
        for statement in role_statements(template, name):
            assert "logs:CreateLogGroup" not in actions_of(statement)
            assert "logs:*" not in actions_of(statement)


# --- API ---------------------------------------------------------------------------------


def test_only_the_five_expected_routes_exist(template: assertions.Template) -> None:
    keys = {
        route["Properties"]["RouteKey"]
        for route in resources(template, "AWS::ApiGatewayV2::Route").values()
    }
    assert keys == {
        "GET /",
        "GET /app.js",
        "GET /app.css",
        "POST /uploads",
        "GET /results/{id}",
    }


def test_stage_is_throttled_with_a_tighter_limit_on_uploads(
    template: assertions.Template,
) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "DefaultRouteSettings": {
                "ThrottlingRateLimit": 10,
                "ThrottlingBurstLimit": 20,
            },
            "RouteSettings": {
                "POST /uploads": {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 2}
            },
            "AccessLogSettings": assertions.Match.object_like(
                {"DestinationArn": assertions.Match.any_value()}
            ),
        },
    )


def test_stage_is_created_after_every_route(template: assertions.Template) -> None:
    """API Gateway rejects RouteSettings for a route that doesn't exist yet."""
    (stage,) = resources(template, "AWS::ApiGatewayV2::Stage").values()
    route_ids = set(resources(template, "AWS::ApiGatewayV2::Route"))
    assert route_ids <= set(stage["DependsOn"])


# --- logs, tags, teardown ------------------------------------------------------------------


def test_every_log_group_expires_and_is_destroyed(
    template: assertions.Template,
) -> None:
    log_groups = resources(template, "AWS::Logs::LogGroup")
    # 4 functions + API access logs + 2 CDK helper functions
    assert len(log_groups) == 7
    for log_group in log_groups.values():
        assert log_group["Properties"]["RetentionInDays"] == 7
        assert log_group["DeletionPolicy"] == "Delete"
        assert log_group["UpdateReplacePolicy"] == "Delete"


def test_every_function_logs_to_a_log_group_in_the_stack(
    template: assertions.Template,
) -> None:
    for function in resources(template, "AWS::Lambda::Function").values():
        log_group = function["Properties"]["LoggingConfig"]["LogGroup"]
        assert (
            "Ref" in log_group
        )  # a log group in this stack, not a name Lambda invents


HELPER_CUSTOM_RESOURCES = (
    "Custom::S3BucketNotifications",
    "Custom::S3AutoDeleteObjects",
)


def test_helper_delete_order_is_custom_resource_then_function_then_log_group(
    template: assertions.Template,
) -> None:
    """CloudFormation deletes dependents first. Each helper's custom resource references
    its function (ServiceToken), and the function references its own log group. This was
    already right when a log group was left behind after destroy (see the deny test)."""
    functions = resources(template, "AWS::Lambda::Function")
    log_groups = resources(template, "AWS::Logs::LogGroup")
    for custom_type in HELPER_CUSTOM_RESOURCES:
        (custom,) = resources(template, custom_type).values()
        function_id = custom["Properties"]["ServiceToken"]["Fn::GetAtt"][0]
        log_group_id = functions[function_id]["Properties"]["LoggingConfig"][
            "LogGroup"
        ]["Ref"]
        assert log_group_id in log_groups
        # No fixed name: a name built from the function would be a dependency cycle.
        assert "LogGroupName" not in log_groups[log_group_id]["Properties"]


def test_auto_delete_helper_role_denies_creating_log_groups_inline(
    template: assertions.Template,
) -> None:
    """Lambda recreates a deleted log group with the role's logs:CreateLogGroup when it
    flushes the last log lines after CloudFormation has already moved on. CDK builds this
    role and can't be told to use another, so it keeps its managed policy and gets an
    inline deny, which lasts exactly as long as the managed policy it overrides."""
    roles = resources(template, "AWS::IAM::Role")
    (role,) = [
        role
        for logical_id, role in roles.items()
        if logical_id.startswith("CustomS3AutoDelete")
    ]
    assert "ManagedPolicyArns" in role["Properties"]
    (policy,) = role["Properties"]["Policies"]
    assert policy["PolicyDocument"]["Statement"] == [
        {"Effect": "Deny", "Action": "logs:CreateLogGroup", "Resource": "*"}
    ]


def test_notifications_helper_has_our_own_minimal_role(
    template: assertions.Template,
) -> None:
    """The S3 event helper runs under a role we pass to the bucket: no managed policy, no
    inline policies, only s3:PutBucketNotification on this bucket (added by CDK) and writes
    to its own log group. It has no logs:CreateLogGroup, so it can't recreate a group."""
    roles = resources(template, "AWS::IAM::Role")
    (role_id,) = [k for k in roles if k.startswith("NotificationsHelperRole")]
    assert not {"ManagedPolicyArns", "Policies"} & set(roles[role_id]["Properties"])

    # The helper function runs as this role.
    (function,) = [
        f
        for k, f in resources(template, "AWS::Lambda::Function").items()
        if k.startswith("BucketNotificationsHandler")
    ]
    assert function["Properties"]["Role"] == {"Fn::GetAtt": [role_id, "Arn"]}

    # No role is created for the helper by CDK any more.
    assert not [k for k in roles if k.startswith("BucketNotificationsHandler")]

    (logs_statement,) = role_statements(template, "NotificationsHelper")
    assert sorted(actions_of(logs_statement)) == [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    ]
    assert logs_statement["Resource"]["Fn::GetAtt"][0].startswith(
        "NotificationsHelperLogs"
    )

    (notification_policy,) = [
        p
        for k, p in resources(template, "AWS::IAM::Policy").items()
        if k.startswith("BucketNotificationsHandlerPolicy")
    ]
    assert notification_policy["Properties"]["Roles"] == [{"Ref": role_id}]
    (statement,) = notification_policy["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:PutBucketNotification"
    (bucket_id,) = resources(template, "AWS::S3::Bucket")
    # this bucket only, not "*"
    assert statement["Resource"] == {"Fn::GetAtt": [bucket_id, "Arn"]}


def test_no_role_in_the_stack_can_create_log_groups_except_behind_a_deny(
    template: assertions.Template,
) -> None:
    for policy in resources(template, "AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert "logs:CreateLogGroup" not in actions_of(statement)


def test_no_separate_policy_resource_carries_a_deny(
    template: assertions.Template,
) -> None:
    """A separate AWS::IAM::Policy can be deleted at the start of a destroy, which would
    remove the deny while the allow is still attached."""
    for policy in resources(template, "AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement["Effect"] == "Allow"


def test_every_taggable_resource_carries_the_project_tag(
    template: assertions.Template,
) -> None:
    for type_ in (
        "AWS::S3::Bucket",
        "AWS::Logs::LogGroup",
        "AWS::Lambda::Function",
        "AWS::IAM::Role",
        "AWS::ApiGatewayV2::Api",
        "AWS::ApiGatewayV2::Stage",
    ):
        found = resources(template, type_)
        assert found, type_
        for logical_id, resource in found.items():
            assert TAG in tag_pairs(resource["Properties"]), f"{logical_id} is untagged"


def test_the_stack_has_no_hardcoded_account() -> None:
    _, stack = build()
    assert cdk.Token.is_unresolved(stack.account)


# --- cdk-nag -----------------------------------------------------------------------------


def test_cdk_nag_passes_with_only_the_documented_acknowledgements() -> None:
    app, _ = build()
    Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    app.synth()  # raises if any finding is not acknowledged


def test_cdk_nag_would_catch_a_violation() -> None:
    """Control: proves the check above isn't passing because nag is switched off."""
    from aws_cdk import aws_s3 as s3

    app = cdk.App(context=CDK_CONTEXT)
    stack = cdk.Stack(app, "Control", env=ENV)
    s3.Bucket(stack, "Unprotected")
    Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    with pytest.raises(Exception, match="(?i)validation|AwsSolutions"):
        app.synth()
