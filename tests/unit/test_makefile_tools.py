"""make check must use the same pinned ruff and checkov everywhere (locally and in CI)."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MAKEFILE = (ROOT / "Makefile").read_text()


def assignment(name: str) -> str:
    match = re.search(rf"^{name} :?= (.+)$", MAKEFILE, re.MULTILINE)
    assert match, f"{name} is not assigned in the Makefile"
    return match.group(1)


def test_ruff_and_checkov_always_run_at_a_pinned_version_through_uvx() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", assignment("RUFF_VERSION"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", assignment("CHECKOV_VERSION"))
    assert assignment("RUFF") == "uvx ruff@$(RUFF_VERSION)"
    assert assignment("CHECKOV") == "uvx checkov@$(CHECKOV_VERSION)"


def test_the_tools_are_not_chosen_by_what_is_installed() -> None:
    assert "command -v" not in MAKEFILE


def test_ci_runs_the_same_make_check() -> None:
    lines = (ROOT / ".github" / "workflows" / "check.yml").read_text().splitlines()
    code = "\n".join(line for line in lines if not line.strip().startswith("#"))
    assert "run: make check" in code
    # No separate ruff or checkov invocation in CI that could use other versions.
    assert "ruff" not in code
    assert "checkov" not in code
