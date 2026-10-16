"""access_log_check: polling, event parsing, diagnostics and the checks, against stubbed clients."""

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import access_log_check as alc
import pytest
from botocore.session import get_session
from botocore.stub import Stubber

STACK = "DemoServerlessStack"
GROUP = f"{STACK}-AccessLogs8B620ECA-x"
# describe_log_groups gives the ARN both without (logGroupArn) and with (arn) a trailing ":*".
GROUP_ARN = f"arn:aws:logs:us-east-1:ACCOUNT:log-group:{GROUP}"
GROUP_INFO = {
    "logGroupName": GROUP,
    "logGroupArn": GROUP_ARN,
    "arn": f"{GROUP_ARN}:*",
    "logGroupClass": "STANDARD",
}
DELIVERY_POLICY = json.dumps(
    {
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "delivery.logs.amazonaws.com"},
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"{GROUP_ARN}:log-stream:*",
            }
        ]
    }
)
STREAM_QUERY = {
    "logGroupName": GROUP,
    "limit": 1,
    "orderBy": "LastEventTime",
    "descending": True,
}
GOOD_EVENT = json.dumps(
    {"requestId": "r-1", "routeKey": "POST /uploads", "status": "200", "ip": "x"}
)


def client(service: str) -> Any:
    return get_session().create_client(service, region_name="us-east-1")


@contextmanager
def stubbed(*services: str) -> Iterator[dict[str, Stubber]]:
    with ExitStack() as stack:
        stubbers = {name: Stubber(client(name)) for name in services}
        for stubber in stubbers.values():
            stack.enter_context(stubber)
        yield stubbers
        for stubber in stubbers.values():
            stubber.assert_no_pending_responses()


class FakeTime:
    """A clock that only moves when sleep() is called, so tests never wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- polling -------------------------------------------------------------------------------


def test_a_stream_that_exists_is_returned_without_waiting() -> None:
    logs = client("logs")
    fake = FakeTime()
    with Stubber(logs) as stubber:
        stubber.add_response(
            "describe_log_streams",
            {"logStreams": [{"logStreamName": "s1"}]},
            STREAM_QUERY,
        )
        assert alc.wait_for_stream(logs, GROUP, 60, fake.sleep, fake.clock) == "s1"
    assert fake.sleeps == []


def test_polling_continues_until_a_stream_appears() -> None:
    logs = client("logs")
    fake = FakeTime()
    with Stubber(logs) as stubber:
        for _ in range(2):
            stubber.add_response(
                "describe_log_streams", {"logStreams": []}, STREAM_QUERY
            )
        stubber.add_response(
            "describe_log_streams",
            {"logStreams": [{"logStreamName": "late"}]},
            STREAM_QUERY,
        )
        assert alc.wait_for_stream(logs, GROUP, 60, fake.sleep, fake.clock) == "late"
    assert fake.sleeps == [alc.POLL_INTERVAL_SECONDS] * 2


def test_polling_gives_up_after_the_deadline() -> None:
    logs = client("logs")
    fake = FakeTime()
    with Stubber(logs) as stubber:
        for _ in range(3):  # at 0 s, 5 s and 10 s; the deadline is 10 s
            stubber.add_response(
                "describe_log_streams", {"logStreams": []}, STREAM_QUERY
            )
        assert alc.wait_for_stream(logs, GROUP, 10, fake.sleep, fake.clock) is None
    assert fake.now == 10


# --- parsing ---------------------------------------------------------------------------------


def test_only_json_objects_with_the_expected_fields_are_access_events() -> None:
    messages = [
        GOOD_EVENT,
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"requestId": "r", "status": "200"}),  # no routeKey
        '{"requestId": "r-2", "routeKey": "GET /", "status": "200"}',
    ]
    events = alc.parse_access_events(messages)
    assert [event["requestId"] for event in events] == ["r-1", "r-2"]


# --- lookups -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("found", "ok"), [([GROUP], True), ([], False), ([GROUP, GROUP + "2"], False)]
)
def test_exactly_one_access_log_group_must_exist(found: list[str], ok: bool) -> None:
    logs = client("logs")
    with Stubber(logs) as stubber:
        stubber.add_response(
            "describe_log_groups",
            {"logGroups": [{**GROUP_INFO, "logGroupName": name} for name in found]},
            {"logGroupNamePrefix": f"{STACK}-AccessLogs"},
        )
        if ok:
            group = alc.find_log_group(logs, STACK)
            assert group["logGroupName"] == GROUP
            assert group["logGroupClass"] == "STANDARD"
        else:
            with pytest.raises(SystemExit, match="Expected one log group"):
                alc.find_log_group(logs, STACK)


def resource(kind: str, physical_id: str) -> dict[str, Any]:
    return {
        "LogicalResourceId": "x",
        "PhysicalResourceId": physical_id,
        "ResourceType": kind,
        "LastUpdatedTimestamp": "2026-01-01T00:00:00Z",
        "ResourceStatus": "CREATE_COMPLETE",
    }


def test_the_api_id_comes_from_the_stacks_own_resources() -> None:
    cloudformation = client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stubber.add_response(
            "list_stack_resources",
            {
                "StackResourceSummaries": [
                    resource("AWS::S3::Bucket", "bucket"),
                    resource("AWS::ApiGatewayV2::Api", "abc123"),
                ]
            },
            {"StackName": STACK},
        )
        assert alc.find_api_id(cloudformation, STACK) == "abc123"


# --- diagnostics ---------------------------------------------------------------------------------


def test_the_group_arn_has_no_trailing_wildcard() -> None:
    assert alc.log_group_arn(GROUP_INFO) == GROUP_ARN
    # If only the "arn" form is present, the ":*" is removed.
    assert alc.log_group_arn({"arn": f"{GROUP_ARN}:*"}) == GROUP_ARN


def stub_stage(stubber: Stubber) -> None:
    stubber.add_response(
        "get_stage",
        {
            "StageName": "$default",
            "AccessLogSettings": {"DestinationArn": GROUP_ARN + ":*"},
        },
        {"ApiId": "abc123", "StageName": "$default"},
    )


def stub_own_policy(stubber: Stubber) -> None:
    """The group's own policy: a RESOURCE-scoped lookup with the ARN lacking ":*"."""
    stubber.add_response(
        "describe_resource_policies",
        {
            "resourcePolicies": [
                {
                    "policyName": "AWSLogDeliveryWrite-x",
                    "policyDocument": DELIVERY_POLICY,
                }
            ]
        },
        {"policyScope": "RESOURCE", "resourceArn": GROUP_ARN},
    )


def test_diagnostics_show_the_stage_the_group_and_its_own_policy() -> None:
    with stubbed("apigatewayv2", "logs") as stubbers:
        stub_stage(stubbers["apigatewayv2"])
        stub_own_policy(stubbers["logs"])
        stubbers["logs"].add_response(
            "describe_resource_policies", {"resourcePolicies": []}
        )  # the account-level list, which is empty
        text = alc.diagnostics(
            stubbers["apigatewayv2"].client,
            stubbers["logs"].client,
            "abc123",
            GROUP_INFO,
        )
    assert GROUP_ARN in text  # the stage's destination
    assert "class: STANDARD" in text
    assert "KMS key: none" in text
    assert "AWSLogDeliveryWrite-x" in text
    assert (
        "delivery.logs.amazonaws.com" in text
    )  # the policy document, parsed and shown
    assert "logs:CreateLogStream" in text
    assert "Account-level" in text
    assert "logs:CreateLogDelivery" in text


def test_a_kms_key_on_the_group_is_shown() -> None:
    group = {**GROUP_INFO, "kmsKeyId": "arn:aws:kms:us-east-1:ACCOUNT:key/abc"}
    with stubbed("apigatewayv2", "logs") as stubbers:
        stub_stage(stubbers["apigatewayv2"])
        stubbers["logs"].add_response(
            "describe_resource_policies",
            {"resourcePolicies": []},
            {"policyScope": "RESOURCE", "resourceArn": GROUP_ARN},
        )
        stubbers["logs"].add_response(
            "describe_resource_policies", {"resourcePolicies": []}
        )
        text = alc.diagnostics(
            stubbers["apigatewayv2"].client, stubbers["logs"].client, "abc123", group
        )
    assert "KMS key: arn:aws:kms:us-east-1:ACCOUNT:key/abc" in text
    assert "key policy" in text


def test_a_failing_diagnostic_is_reported_not_hidden() -> None:
    with stubbed("apigatewayv2", "logs") as stubbers:
        stubbers["apigatewayv2"].add_client_error(
            "get_stage", service_error_code="AccessDenied", service_message="no"
        )
        stubbers["logs"].add_client_error(
            "describe_resource_policies",
            service_error_code="AccessDeniedException",
            service_message="no",
        )
        stubbers["logs"].add_response(
            "describe_resource_policies", {"resourcePolicies": []}
        )
        text = alc.diagnostics(
            stubbers["apigatewayv2"].client,
            stubbers["logs"].client,
            "abc123",
            GROUP_INFO,
        )
    assert "could not read this (AccessDenied)" in text
    assert "could not read this (AccessDeniedException)" in text


# --- the check -------------------------------------------------------------------------------------


SERVICES = ("cloudformation", "logs", "apigatewayv2")


def stub_group_and_stream(
    stubbers: dict[str, Stubber], streams: list[dict[str, str]]
) -> None:
    stubbers["logs"].add_response(
        "describe_log_groups",
        {"logGroups": [GROUP_INFO]},
        {"logGroupNamePrefix": f"{STACK}-AccessLogs"},
    )
    stubbers["logs"].add_response(
        "describe_log_streams", {"logStreams": streams}, STREAM_QUERY
    )


def test_the_check_passes_when_access_logs_arrive() -> None:
    with stubbed(*SERVICES) as stubbers:
        stub_group_and_stream(stubbers, [{"logStreamName": "s1"}])
        stubbers["logs"].add_response(
            "get_log_events",
            {"events": [{"timestamp": 1, "message": GOOD_EVENT, "ingestionTime": 1}]},
            {
                "logGroupName": GROUP,
                "logStreamName": "s1",
                "limit": 10,
                "startFromHead": False,
            },
        )
        clients = {name: s.client for name, s in stubbers.items()}
        ok, message = alc.check(clients, STACK, wait_seconds=0)
    assert ok
    assert "POST /uploads" in message and "r-1" in message


def test_the_check_fails_with_evidence_when_no_stream_appears() -> None:
    """The case seen on the first live run: requests worked, the log group stayed empty."""
    with stubbed(*SERVICES) as stubbers:
        stub_group_and_stream(stubbers, [])
        stubbers["cloudformation"].add_response(
            "list_stack_resources",
            {"StackResourceSummaries": [resource("AWS::ApiGatewayV2::Api", "abc123")]},
            {"StackName": STACK},
        )
        stubbers["apigatewayv2"].add_response(
            "get_stage",
            {"StageName": "$default"},
            {"ApiId": "abc123", "StageName": "$default"},
        )
        stub_own_policy(stubbers["logs"])
        stubbers["logs"].add_response(
            "describe_resource_policies", {"resourcePolicies": []}
        )
        clients = {name: s.client for name, s in stubbers.items()}
        ok, message = alc.check(clients, STACK, wait_seconds=0)
    assert not ok
    assert "No log stream appeared" in message
    assert "its own resource policies" in message
    assert "AWSLogDeliveryWrite-x" in message
    assert "CloudWatch Logs resource policies" in message


def test_the_check_fails_if_events_are_not_in_the_configured_format() -> None:
    with stubbed(*SERVICES) as stubbers:
        stub_group_and_stream(stubbers, [{"logStreamName": "s1"}])
        stubbers["logs"].add_response(
            "get_log_events",
            {
                "events": [
                    {"timestamp": 1, "message": "127.0.0.1 - GET /", "ingestionTime": 1}
                ]
            },
            {
                "logGroupName": GROUP,
                "logStreamName": "s1",
                "limit": 10,
                "startFromHead": False,
            },
        )
        clients = {name: s.client for name, s in stubbers.items()}
        ok, message = alc.check(clients, STACK, wait_seconds=0)
    assert not ok and "format" in message


# --- command line -------------------------------------------------------------------------------------


def test_arguments_are_validated() -> None:
    with pytest.raises(SystemExit):
        alc.parse_args(["--stack-name", "bad name"])
    with pytest.raises(SystemExit):
        alc.parse_args(["--wait", "-1"])
    with pytest.raises(SystemExit):
        alc.parse_args(["--wait", "100000"])
    args = alc.parse_args([])
    assert args.stack_name == "DemoServerlessStack" and args.wait == 120
    assert args.profile is None and args.region is None


def test_main_reports_the_result_as_an_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Session:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def client(self, name: str) -> str:
            return name

    results = iter([(True, "all good"), (False, "nothing arrived")])
    monkeypatch.setattr(alc, "check", lambda clients, stack, wait: next(results))
    assert alc.main(["--wait", "0"], Session) == 0
    assert "PASS  all good" in capsys.readouterr().out
    assert alc.main(["--wait", "0"], Session) == 1
    assert "FAIL  nothing arrived" in capsys.readouterr().out
