"""teardown_check: pure helpers, AWS lookups against botocore Stubbers, and the CLI.

Clients are real botocore clients with fake credentials (see conftest) whose responses are
stubbed, so nothing reaches AWS.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest
import teardown_check
from botocore.exceptions import MissingDependencyException
from botocore.session import get_session
from botocore.stub import Stubber

STACK = "DemoServerlessStack"
ARN_A = "arn:aws:s3:::bucket-a"
ARN_B = "arn:aws:logs:us-east-1:ACCOUNT:log-group:b"


# --- pure helpers ---------------------------------------------------------------------------


def test_baseline_round_trips_and_ignores_comments_and_blank_lines() -> None:
    text = teardown_check.format_baseline({ARN_B, ARN_A})
    assert text.startswith("# ")  # the header warns not to commit it
    assert teardown_check.parse_baseline(text) == {ARN_A, ARN_B}
    assert teardown_check.parse_baseline("\n  \n# note\n" + ARN_A + "\n") == {ARN_A}


def test_only_resources_new_since_the_baseline_count() -> None:
    assert teardown_check.new_since({ARN_A, ARN_B}, {ARN_A}) == [ARN_B]
    assert teardown_check.new_since({ARN_A}, {ARN_A, ARN_B}) == []  # others may vanish
    assert teardown_check.new_since(set(), set()) == []


def test_prefix_filter() -> None:
    names = ["DemoServerlessStack-Role", "Other", "DemoServerlessStackX", "demo"]
    assert teardown_check.with_prefix(names, "DemoServerlessStack-") == [
        "DemoServerlessStack-Role"
    ]


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("DemoServerlessStack", True),
        ("a-b-1", True),
        ("", False),
        ("a b", False),
        ("a/b", False),
        ("a;b", False),
    ],
)
def test_stack_name_validation(name: str, valid: bool) -> None:
    assert teardown_check.valid_stack_name(name) is valid


# --- AWS lookups -------------------------------------------------------------------------------


def client(service: str) -> Any:
    return get_session().create_client(service, region_name="us-east-1")


@contextmanager
def stubbed(*services: str) -> Iterator[dict[str, Stubber]]:
    """Clients for the given services, each with an active Stubber."""
    with ExitStack() as stack:
        stubbers = {name: Stubber(client(name)) for name in services}
        for stubber in stubbers.values():
            stack.enter_context(stubber)
        yield stubbers
        for stubber in stubbers.values():
            stubber.assert_no_pending_responses()


def stack_error(stubber: Stubber, code: str, message: str) -> None:
    stubber.add_client_error(
        "describe_stacks", service_error_code=code, service_message=message
    )


def test_a_deleted_stack_is_reported_as_gone() -> None:
    cloudformation = client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stack_error(stubber, "ValidationError", f"Stack with id {STACK} does not exist")
        assert teardown_check.stack_status(cloudformation, STACK) is None


def test_an_existing_stack_reports_its_status() -> None:
    cloudformation = client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stubber.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": STACK,
                        "CreationTime": "2026-01-01T00:00:00Z",
                        "StackStatus": "DELETE_IN_PROGRESS",
                    }
                ]
            },
        )
        assert (
            teardown_check.stack_status(cloudformation, STACK) == "DELETE_IN_PROGRESS"
        )


def test_other_cloudformation_errors_are_not_mistaken_for_gone() -> None:
    cloudformation = client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stack_error(stubber, "AccessDenied", "User is not authorized")
        with pytest.raises(Exception, match="AccessDenied"):
            teardown_check.stack_status(cloudformation, STACK)
    with Stubber(cloudformation) as stubber:
        stack_error(
            stubber, "ValidationError", "Template format error"
        )  # not "does not exist"
        with pytest.raises(Exception, match="ValidationError"):
            teardown_check.stack_status(cloudformation, STACK)


def test_tagged_arns_follow_pagination() -> None:
    tagging = client("resourcegroupstaggingapi")
    filters = {"TagFilters": [{"Key": "project", "Values": ["demo-serverless"]}]}
    with Stubber(tagging) as stubber:
        stubber.add_response(
            "get_resources",
            {
                "PaginationToken": "next",
                "ResourceTagMappingList": [{"ResourceARN": ARN_A}],
            },
            filters,
        )
        stubber.add_response(
            "get_resources",
            {"PaginationToken": "", "ResourceTagMappingList": [{"ResourceARN": ARN_B}]},
            {**filters, "PaginationToken": "next"},
        )
        assert teardown_check.tagged_arns(tagging) == {ARN_A, ARN_B}


def test_log_groups_are_looked_up_by_each_prefix() -> None:
    logs = client("logs")
    prefixes = [STACK, f"/aws/lambda/{STACK}"]
    with Stubber(logs) as stubber:
        stubber.add_response(
            "describe_log_groups",
            {"logGroups": [{"logGroupName": f"{STACK}-AccessLogs-1"}]},
            {"logGroupNamePrefix": prefixes[0]},
        )
        stubber.add_response(
            "describe_log_groups",
            {"logGroups": [{"logGroupName": f"/aws/lambda/{STACK}-x"}]},
            {"logGroupNamePrefix": prefixes[1]},
        )
        assert teardown_check.log_group_names(logs, prefixes) == [
            f"/aws/lambda/{STACK}-x",
            f"{STACK}-AccessLogs-1",
        ]


def test_role_names_follow_pagination() -> None:
    iam = client("iam")
    role = {
        "Path": "/",
        "RoleId": "AROAEXAMPLEEXAMPLE1",
        "Arn": "arn:aws:iam::ACCOUNT:role/r",
        "CreateDate": "2026-01-01T00:00:00Z",
    }
    with Stubber(iam) as stubber:
        stubber.add_response(
            "list_roles",
            {"IsTruncated": True, "Marker": "m", "Roles": [{**role, "RoleName": "a"}]},
        )
        stubber.add_response(
            "list_roles",
            {"IsTruncated": False, "Roles": [{**role, "RoleName": "b"}]},
            {"Marker": "m"},
        )
        assert teardown_check.role_names(iam) == ["a", "b"]


# --- the checks together -----------------------------------------------------------------------


def stub_everything(
    stubbers: dict[str, Stubber],
    *,
    stack_exists: bool,
    extra_arn: str | None,
    log_groups: list[str],
    roles: list[str],
) -> None:
    if stack_exists:
        stubbers["cloudformation"].add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": STACK,
                        "CreationTime": "2026-01-01T00:00:00Z",
                        "StackStatus": "CREATE_COMPLETE",
                    }
                ]
            },
        )
    else:
        stack_error(
            stubbers["cloudformation"],
            "ValidationError",
            f"Stack with id {STACK} does not exist",
        )
    arns = [{"ResourceARN": ARN_A}] + (
        [{"ResourceARN": extra_arn}] if extra_arn else []
    )
    stubbers["resourcegroupstaggingapi"].add_response(
        "get_resources", {"PaginationToken": "", "ResourceTagMappingList": arns}
    )
    for prefix, found in (
        (STACK, [n for n in log_groups if not n.startswith("/")]),
        (f"/aws/lambda/{STACK}", [n for n in log_groups if n.startswith("/")]),
    ):
        stubbers["logs"].add_response(
            "describe_log_groups",
            {"logGroups": [{"logGroupName": n} for n in found]},
            {"logGroupNamePrefix": prefix},
        )
    role = {
        "Path": "/",
        "RoleId": "AROAEXAMPLEEXAMPLE1",
        "Arn": "arn:aws:iam::ACCOUNT:role/r",
        "CreateDate": "2026-01-01T00:00:00Z",
    }
    stubbers["iam"].add_response(
        "list_roles",
        {"IsTruncated": False, "Roles": [{**role, "RoleName": name} for name in roles]},
    )


SERVICES = ("cloudformation", "resourcegroupstaggingapi", "logs", "iam")


def run_with(**kwargs: Any) -> list[teardown_check.Finding]:
    with stubbed(*SERVICES) as stubbers:
        stub_everything(stubbers, **kwargs)
        clients = {name: stubber.client for name, stubber in stubbers.items()}
        return teardown_check.run_checks(clients, STACK, {ARN_A})


def test_a_clean_teardown_passes_every_check() -> None:
    findings = run_with(
        stack_exists=False, extra_arn=None, log_groups=[], roles=["Unrelated"]
    )
    assert [finding.passed for finding in findings] == [True, True, True, True]


def test_each_kind_of_leftover_fails_its_own_check() -> None:
    findings = run_with(
        stack_exists=True,
        extra_arn=ARN_B,
        log_groups=[f"{STACK}-NotificationsHelperLogs-1", f"/aws/lambda/{STACK}-F"],
        roles=[f"{STACK}-PageRole-1", "Unrelated"],
    )
    stack, tags, logs, roles = findings
    assert not stack.passed and "CREATE_COMPLETE" in stack.left_behind[0]
    assert tags.left_behind == [ARN_B]
    assert logs.left_behind == sorted(
        [f"{STACK}-NotificationsHelperLogs-1", f"/aws/lambda/{STACK}-F"]
    )
    assert roles.left_behind == [f"{STACK}-PageRole-1"]


def test_report_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert teardown_check.report([teardown_check.Finding("ok")]) == 0
    assert "Nothing left behind" in capsys.readouterr().out
    assert teardown_check.report([teardown_check.Finding("bad", ["x"])]) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output and "left behind: x" in output and "lag" in output


# --- command line --------------------------------------------------------------------------------


def test_baseline_cannot_be_written_inside_the_repository() -> None:
    inside = teardown_check.REPO_ROOT / "baseline.txt"
    with pytest.raises(SystemExit, match="outside"):
        teardown_check.write_baseline(inside, {ARN_A})
    assert not inside.exists()


def test_baseline_is_written_once_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "baseline.txt"
    teardown_check.write_baseline(path, {ARN_A, ARN_B})
    assert teardown_check.parse_baseline(path.read_text()) == {ARN_A, ARN_B}
    with pytest.raises(SystemExit, match="already exists"):
        teardown_check.write_baseline(path, set())
    assert teardown_check.parse_baseline(path.read_text()) == {ARN_A, ARN_B}


def test_a_missing_baseline_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--write-baseline"):
        teardown_check.read_baseline(tmp_path / "nope.txt")


def test_arguments_are_validated() -> None:
    with pytest.raises(SystemExit):  # one of --baseline / --write-baseline is required
        teardown_check.parse_args([])
    with pytest.raises(SystemExit):
        teardown_check.parse_args(["--baseline", "f", "--write-baseline", "g"])
    with pytest.raises(SystemExit):
        teardown_check.parse_args(["--baseline", "f", "--stack-name", "bad name!"])
    args = teardown_check.parse_args(["--baseline", "f"])
    assert args.stack_name == "DemoServerlessStack"
    assert args.profile is None and args.region is None  # nothing is hardcoded


def test_main_passes_profile_region_stack_and_baseline_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "b.txt"
    baseline.write_text(teardown_check.format_baseline({ARN_A}))
    session_args: dict[str, Any] = {}
    check_args: dict[str, Any] = {}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            session_args.update(kwargs)

        def client(self, name: str) -> str:
            return f"client:{name}"  # no real client, so nothing can reach AWS

    def fake_run_checks(
        clients: dict[str, Any], stack_name: str, baseline: set[str]
    ) -> list[teardown_check.Finding]:
        check_args.update(clients=clients, stack_name=stack_name, baseline=baseline)
        return [teardown_check.Finding("fake", ["left"])]

    monkeypatch.setattr(teardown_check, "run_checks", fake_run_checks)
    exit_code = teardown_check.main(
        [
            "--baseline",
            str(baseline),
            "--profile",
            "p",
            "--region",
            "r-1",
            "--stack-name",
            "Other-Stack",
        ],
        session_factory=FakeSession,
    )
    assert exit_code == 1  # a leftover makes the script fail
    assert session_args == {"profile_name": "p", "region_name": "r-1"}
    assert check_args["stack_name"] == "Other-Stack"
    assert check_args["baseline"] == {ARN_A}
    assert set(check_args["clients"]) == set(SERVICES)


def test_main_explains_a_missing_crt_package_instead_of_a_traceback(
    tmp_path: Path,
) -> None:
    """Reproduces the crash seen with `aws login` credentials and no botocore[crt]."""
    baseline = tmp_path / "b.txt"
    baseline.write_text(teardown_check.format_baseline(set()))

    class SessionWithoutCrt:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def client(self, name: str) -> Any:
            raise MissingDependencyException(
                msg='requires an additional dependency: pip install "botocore[crt]"'
            )

    with pytest.raises(SystemExit, match="make install"):
        teardown_check.main(["--baseline", str(baseline)], SessionWithoutCrt)
    with pytest.raises(SystemExit, match="make install"):
        teardown_check.main(
            ["--write-baseline", str(tmp_path / "new.txt")], SessionWithoutCrt
        )


def test_an_existing_baseline_is_refused_before_any_aws_call(tmp_path: Path) -> None:
    """A second deploy cycle: the baseline from the first one is still there."""
    path = tmp_path / "baseline.txt"
    path.write_text(teardown_check.format_baseline({ARN_A}))
    before = path.read_text()
    created_sessions: list[object] = []

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            created_sessions.append(kwargs)

        def client(self, name: str) -> Any:
            raise AssertionError("no AWS client should be created")

    with pytest.raises(SystemExit) as raised:
        teardown_check.main(["--write-baseline", str(path)], Session)
    assert created_sessions == []  # not even a session, let alone a call
    assert path.read_text() == before  # never overwritten
    message = str(raised.value)
    assert "already exists" in message
    assert "skip this step" in message and "make deploy" in message
    assert "BASELINE=" in message
    assert "hide them" in message  # the warning about re-recording over leftovers


def test_the_refusal_applies_even_if_the_file_appears_during_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "late.txt"
    # The up-front check passes, then someone creates the file before the write.
    monkeypatch.setattr(teardown_check, "check_baseline_target", lambda p: None)
    path.write_text("# someone else's baseline\n")
    with pytest.raises(SystemExit, match="already exists"):
        teardown_check.write_baseline(path, {ARN_A})
    assert path.read_text() == "# someone else's baseline\n"
