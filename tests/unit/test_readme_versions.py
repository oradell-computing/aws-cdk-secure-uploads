"""The versions the README tells people to install must be the ones CI uses."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
README = (ROOT / "README.md").read_text()
WORKFLOW = (ROOT / ".github" / "workflows" / "check.yml").read_text()


def workflow_value(name: str) -> str:
    match = re.search(rf'^\s*{name}: "(\d+\.\d+\.\d+)"\s*$', WORKFLOW, re.MULTILINE)
    assert match, f"{name} is not set in the workflow"
    return match.group(1)


def test_the_readme_installs_the_uv_version_ci_uses() -> None:
    versions = re.findall(r"astral\.sh/uv/(\d+\.\d+\.\d+)/install\.sh", README)
    assert versions == [workflow_value("UV_VERSION")]


def test_the_readme_installs_the_cdk_cli_version_ci_uses() -> None:
    versions = re.findall(r"aws-cdk@(\d+\.\d+\.\d+)", README)
    assert versions == [workflow_value("CDK_CLI_VERSION")]


def test_every_cdk_cli_version_the_readme_mentions_is_the_one_ci_uses() -> None:
    """CDK CLI versions look like 2.1145.0 (a four-digit minor). The library's own version
    (2.273.0) has three digits and isn't stated in the README at all."""
    mentioned = set(re.findall(r"\b2\.\d{4}\.\d+\b", README))
    assert mentioned == {workflow_value("CDK_CLI_VERSION")}


def test_the_readme_names_the_aws_cli_minimum_for_aws_login() -> None:
    assert "needs AWS CLI 2.32.0 or later" in README
    assert "`aws-cli/2.32.0` or later" in README
