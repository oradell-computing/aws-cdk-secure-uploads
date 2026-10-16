"""Dependabot updates GitHub Actions only (no pip or npm updates yet)."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / ".github" / "dependabot.yml"


def text() -> str:
    return CONFIG.read_text()


def code() -> str:
    """The file without comment lines, so wording in comments can't affect the checks."""
    lines = text().splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


def test_the_config_exists() -> None:
    assert CONFIG.is_file()
    assert re.search(r"^version: 2$", code(), re.MULTILINE)


def test_only_the_github_actions_ecosystem_is_listed() -> None:
    ecosystems = re.findall(r'package-ecosystem:\s*"?([\w-]+)"?', code())
    assert ecosystems == ["github-actions"]


def test_it_checks_the_repository_root_weekly_with_a_ci_commit_prefix() -> None:
    content = code()
    assert re.search(r'^\s+directory: "/"\s*$', content, re.MULTILINE)
    assert re.search(r'^\s+interval: "weekly"\s*$', content, re.MULTILINE)
    assert re.search(r'^\s+prefix: "ci"\s*$', content, re.MULTILINE)


def test_the_ecosystem_check_would_catch_an_added_pip_entry() -> None:
    """A control: the pattern used above does see every ecosystem entry."""
    extra = code() + '\n  - package-ecosystem: "pip"\n    directory: "/"\n'
    assert re.findall(r'package-ecosystem:\s*"?([\w-]+)"?', extra) == [
        "github-actions",
        "pip",
    ]
