"""The CI workflow must stay credential-free and read-only (see roadmap step 6)."""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "check.yml"

# `uses: owner/name@<40 lowercase hex characters> # v<version>`: a tag or branch can be moved
# to different code after the fact, a full commit SHA can't, and the comment says which release it is.
PINNED_USES = re.compile(
    r"uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+\S*"
)


def text() -> str:
    return WORKFLOW.read_text()


def unpinned_uses(content: str) -> list[str]:
    """The `uses:` lines that are not pinned to a full SHA with a version comment."""
    lines = [line.strip() for line in content.splitlines()]
    uses = [
        line.removeprefix("- ")
        for line in lines
        if not line.startswith("#") and line.removeprefix("- ").startswith("uses:")
    ]
    return [line for line in uses if not PINNED_USES.fullmatch(line)]


def test_the_workflow_runs_make_check() -> None:
    assert "run: make check" in text()


def test_the_workflow_has_no_aws_credentials_or_secrets() -> None:
    lowered = text().lower()
    for forbidden in (
        "secrets.",
        "configure-aws-credentials",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "role-to-assume",
        "id-token",
    ):
        assert forbidden not in lowered, forbidden


def test_the_workflow_only_reads_the_repository_and_never_deploys() -> None:
    content = text()
    assert "permissions:\n  contents: read\n" in content
    assert "write" not in content.split("permissions:")[1].split("concurrency:")[0]
    for command in ("cdk deploy", "cdk destroy", "cdk bootstrap", "make deploy"):
        assert command not in content


def test_tool_versions_are_pinned() -> None:
    content = text()
    assert 'UV_VERSION: "' in content and 'CDK_CLI_VERSION: "' in content
    assert "@latest" not in content


def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment() -> None:
    content = text()
    assert "uses:" in content  # the check below must have something to check
    assert unpinned_uses(content) == []


SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


@pytest.mark.parametrize(
    "line",
    [
        "uses: actions/checkout@v4",  # a tag
        "uses: actions/checkout@v7.0.1",
        "uses: actions/checkout@main",  # a branch
        "uses: actions/checkout@3d3c42e",  # a short SHA
        f"uses: actions/checkout@{SHA[:39]}",  # 39 characters
        f"uses: actions/checkout@{SHA}1",  # 41 characters
        f"uses: actions/checkout@{SHA.upper()} # v7.0.1",  # uppercase
        f"uses: actions/checkout@{SHA}",  # no version comment
        f"uses: actions/checkout@{SHA} # latest",  # a comment that isn't a version
        f"uses: actions/checkout@{SHA} # 7.0.1",  # missing the leading v
        "uses: ./local-action",  # not a pinned remote action
        "uses: docker://alpine:3",
    ],
)
def test_the_pin_check_rejects_anything_that_is_not_a_sha_with_a_version(
    line: str,
) -> None:
    assert unpinned_uses(f"steps:\n  - {line}\n") == [line]


def test_the_pin_check_accepts_a_sha_with_a_version_comment() -> None:
    good = f"uses: actions/checkout@{SHA} # v7.0.1"
    assert unpinned_uses(f"steps:\n  - {good}\n    with:\n      x: y\n") == []
    assert unpinned_uses(f"steps:\n  - {good}\n  - name: x\n    {good}\n") == []


def test_commented_out_uses_lines_are_ignored() -> None:
    assert unpinned_uses("# - uses: actions/checkout@v4\n") == []


def test_the_workflow_keeps_persist_credentials_off() -> None:
    assert "persist-credentials: false" in text()
