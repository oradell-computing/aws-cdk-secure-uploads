"""Repository-wide rules that a linter would enforce on some platforms but not others."""

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SKIPPED_DIRS = {
    ".git",
    ".venv",
    "cdk.out",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


def python_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not SKIPPED_DIRS & set(path.relative_to(REPO_ROOT).parts)
    ]


def test_no_python_file_has_a_shebang() -> None:
    """Everything runs through the Makefile or cdk.json with the venv's Python, so a
    shebang would only invite running a script with the wrong interpreter. Ruff's EXE001
    flags a shebang on a file that isn't executable, but ruff doesn't enforce its executable-file
    rules on every platform (WSL is one example), so a shebang could pass a local check and fail
    in CI. This test makes the rule hold wherever the tests run."""
    files = python_files()
    assert files, "found no Python files: is the repository root right?"
    with_shebang = [
        str(path.relative_to(REPO_ROOT))
        for path in files
        if path.read_bytes().startswith(b"#!")
    ]
    assert with_shebang == []
