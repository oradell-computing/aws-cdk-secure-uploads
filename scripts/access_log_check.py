"""Check that the API's access logs actually arrive. `make smoke` runs it for you.

HTTP API access logging can fail silently: the stage is created and the requests work, but
nothing is delivered to the log group. This waits for a log stream to appear, then reads
a few events to confirm they are in the format the stack configures. If nothing arrives it
prints what the stage and the account actually have configured, to show why.

To run it alone, use `make access-log-check`, or by hand with the project's interpreter:
.venv/bin/python scripts/access_log_check.py [--stack-name NAME] [--profile P] [--region R]
                                             [--wait SECONDS]
Credentials come from the usual places (see teardown_check.py). Exits non-zero on failure.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import boto3
from awsutil import make_clients
from botocore.exceptions import ClientError
from teardown_check import DEFAULT_STACK_NAME, valid_stack_name

DEFAULT_WAIT_SECONDS = 120
POLL_INTERVAL_SECONDS = 5
SAMPLE_EVENTS = 10
STAGE_NAME = "$default"
EXPECTED_FIELDS = ("requestId", "routeKey", "status")


def find_log_group(logs: Any, stack_name: str) -> dict[str, Any]:
    """The stack's access log group (named <stack>-AccessLogs...), as AWS describes it."""
    prefix = f"{stack_name}-AccessLogs"
    pages = logs.get_paginator("describe_log_groups").paginate(
        logGroupNamePrefix=prefix
    )
    groups = [group for page in pages for group in page["logGroups"]]
    if len(groups) != 1:
        raise SystemExit(
            f"Expected one log group starting with {prefix}, found {len(groups)}. "
            f"Is {stack_name} deployed in this account and region?"
        )
    return dict(groups[0])


def log_group_arn(group: dict[str, Any]) -> str:
    """The group's ARN without the trailing ":*", which is what a resource-scoped policy
    lookup needs. describe_log_groups returns it as logGroupArn (and the other form as arn)."""
    arn = group.get("logGroupArn") or group["arn"].removesuffix(":*")
    return str(arn)


def find_api_id(cloudformation: Any, stack_name: str) -> str:
    """The HTTP API's id, from the stack's own resources."""
    pages = cloudformation.get_paginator("list_stack_resources").paginate(
        StackName=stack_name
    )
    ids = [
        resource["PhysicalResourceId"]
        for page in pages
        for resource in page["StackResourceSummaries"]
        if resource["ResourceType"] == "AWS::ApiGatewayV2::Api"
    ]
    if len(ids) != 1:
        raise SystemExit(f"Expected one HTTP API in {stack_name}, found {len(ids)}.")
    return str(ids[0])


def wait_for_stream(
    logs: Any,
    log_group: str,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The name of the first log stream once one exists, or None if none appears in time."""
    deadline = clock() + wait_seconds
    while True:
        streams = logs.describe_log_streams(
            logGroupName=log_group, limit=1, orderBy="LastEventTime", descending=True
        )["logStreams"]
        if streams:
            return str(streams[0]["logStreamName"])
        if clock() >= deadline:
            return None
        sleep(POLL_INTERVAL_SECONDS)


def parse_access_events(messages: Sequence[str]) -> list[dict[str, Any]]:
    """The messages that are JSON objects with the fields the stack's format defines."""
    parsed = []
    for message in messages:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and all(field in event for field in EXPECTED_FIELDS):
            parsed.append(event)
    return parsed


def sample_messages(logs: Any, log_group: str, stream: str) -> list[str]:
    response = logs.get_log_events(
        logGroupName=log_group,
        logStreamName=stream,
        limit=SAMPLE_EVENTS,
        startFromHead=False,
    )
    return [str(event["message"]) for event in response["events"]]


def describe(call: Callable[[], Any]) -> str:
    """Run a diagnostic lookup; a failure is part of the answer, so it is shown, not hidden."""
    try:
        return json.dumps(call(), indent=2, default=str)
    except ClientError as error:
        return f"could not read this ({error.response['Error']['Code']}): {error}"


def group_policies(logs: Any, group: dict[str, Any]) -> list[dict[str, Any]]:
    """The policies attached to this log group itself. Account-level policies are a
    different list, and API Gateway's delivery grant is not in it."""
    response = logs.describe_resource_policies(
        policyScope="RESOURCE", resourceArn=log_group_arn(group)
    )
    policies = []
    for policy in response["resourcePolicies"]:
        document = policy["policyDocument"]
        try:  # the document arrives as a JSON string; show it as structure
            document = json.loads(document)
        except json.JSONDecodeError:
            pass
        policies.append(
            {"policyName": policy["policyName"], "policyDocument": document}
        )
    return policies


def diagnostics(apigateway: Any, logs: Any, api_id: str, group: dict[str, Any]) -> str:
    stage = describe(
        lambda: {
            key: value
            for key, value in apigateway.get_stage(
                ApiId=api_id, StageName=STAGE_NAME
            ).items()
            if key in ("AccessLogSettings", "DefaultRouteSettings", "StageName")
        }
    )
    own_policies = describe(lambda: group_policies(logs, group))
    account_policies = describe(
        lambda: [
            policy["policyName"]
            for policy in logs.describe_resource_policies()["resourcePolicies"]
        ]
    )
    return (
        f"Stage {STAGE_NAME} of API {api_id} reports:\n{stage}\n\n"
        f"Log group {group['logGroupName']}:\n"
        f"  class: {group.get('logGroupClass', 'not reported')}\n"
        f"  KMS key: {group.get('kmsKeyId', 'none')}\n"
        f"  its own resource policies:\n{own_policies}\n\n"
        f"Account-level CloudWatch Logs resource policies in this region (usually empty and "
        f"not where API Gateway's grant lives):\n{account_policies}\n\n"
        "What to check: the stage's AccessLogSettings should name this log group, and the "
        "group's own policy should allow delivery.logs.amazonaws.com to call "
        "logs:CreateLogStream and logs:PutLogEvents on it (with aws:SourceAccount and "
        "aws:SourceArn conditions). A KMS key on the group also needs a key policy that "
        "lets log delivery use it. If all of that is in place, delivery can simply lag: run "
        "`make access-log-check` again in a minute. The identity that deployed needs "
        "logs:CreateLogDelivery, logs:PutResourcePolicy, logs:DescribeResourcePolicies and "
        "logs:DescribeLogGroups for HTTP API logging; an administrator role has them."
    )


def check(
    clients: dict[str, Any], stack_name: str, wait_seconds: float
) -> tuple[bool, str]:
    group = find_log_group(clients["logs"], stack_name)
    log_group = str(group["logGroupName"])
    stream = wait_for_stream(clients["logs"], log_group, wait_seconds)
    if stream is None:
        api_id = find_api_id(clients["cloudformation"], stack_name)
        return False, (
            f"No log stream appeared in {log_group} within {wait_seconds:.0f} s.\n\n"
            + diagnostics(clients["apigatewayv2"], clients["logs"], api_id, group)
        )
    events = parse_access_events(sample_messages(clients["logs"], log_group, stream))
    if not events:
        return False, (
            f"A log stream exists in {log_group}, but none of its latest events has the "
            f"fields {EXPECTED_FIELDS}. The format configured on the stage may have changed."
        )
    latest = events[-1]
    return True, (
        f"Access logs arrive in {log_group}. Latest: {latest['routeKey']} -> "
        f"status {latest['status']} (request {latest['requestId']})."
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check API access logs are delivered.")
    parser.add_argument("--stack-name", default=DEFAULT_STACK_NAME)
    parser.add_argument("--profile")
    parser.add_argument("--region")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    args = parser.parse_args(argv)
    if not valid_stack_name(args.stack_name):
        parser.error("--stack-name may only contain letters, digits and hyphens")
    if not 0 <= args.wait <= 900:
        parser.error("--wait must be between 0 and 900 seconds")
    return args


def main(
    argv: Sequence[str], session_factory: Callable[..., Any] = boto3.Session
) -> int:
    args = parse_args(argv)
    session = session_factory(profile_name=args.profile, region_name=args.region)
    clients = make_clients(session, ("cloudformation", "logs", "apigatewayv2"))
    ok, message = check(clients, args.stack_name, args.wait)
    print(("PASS  " if ok else "FAIL  ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
