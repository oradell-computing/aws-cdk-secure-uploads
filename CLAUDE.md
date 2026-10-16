# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup and commands

Dependencies are managed with plain `requirements.txt` files and a `.venv` created by `cdk init` (not a uv project — no `pyproject.toml`). `aws-cdk-lib`, `constructs` and `cdk-nag` are pinned to exact versions in `requirements.txt`; dev tools (pytest, mypy, boto3, `botocore[crt]` for `aws login` credentials) are pinned in `requirements-dev.txt`.

Run `make check` before committing, and `make help` to see the other targets.

Linux is the supported platform and the CI runner (`ubuntu-24.04`) is the reference environment. Don't add code, docs or workarounds for any other platform, and don't let the developer's own environment shape the repo: if something passes locally but fails in CI, CI is right.

`cdk.json` runs the app with `.venv/bin/python app.py`, so `cdk` commands work without activating the venv. Run them from this directory, where `cdk.json` lives.

The `Makefile` wraps all of this for users (`make help`): `make check` runs every offline check (ruff, mypy, pytest, `cdk synth` with cdk-nag, checkov, and an account-ID search). `deploy`, `bootstrap` and `destroy` print the account and region and ask for the account ID to be typed back; Claude doesn't run them (nor `baseline`). `README.md` is the user-facing guide and must stay free of account IDs, profile names and details of anyone's machine or accounts. The author credit in its About section is intentional.

## Architecture

- `app.py` — CDK entry point; reads the account and region from CDK context (`account`, `region`), falling back to the deployer's credentials (`CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION`). A personal account and region pin, if someone wants one, belongs in the user-level `~/.cdk.json`, outside the repo. Never hardcode an account ID, region or profile in the repo.
- `demo_serverless/demo_serverless_stack.py` — all infrastructure is defined here. `suppressions.py` holds the helpers for every cdk-nag acknowledgement and checkov skip (each needs a reason); `config.py` validates the `reserved_concurrency` context value (default 2 per function, `0` = no reservation).
- `functions/` — the Lambda asset (one shared zip, four handlers: `page`, `presign`, `results`, `processor`) plus `static/` (the page). Standard library + the runtime's boto3 only. Not named `lambda`, a Python keyword. `pytest.ini` and `mypy.ini` put it on the import path.
- cdk-nag is v3: it is a CDK validation plugin (`Validations.of(app).add_plugins(...)`), unacknowledged findings fail `cdk synth`, and suppression is `Validations.of(construct).acknowledge(...)`. There is no `NagSuppressions`.
- `scripts/smoke_test.py <SiteUrl>` — post-deploy smoke test (standard library only). `scripts/access_log_check.py` — waits for the API's access logs to arrive (`make smoke` runs it). `scripts/teardown_check.py --baseline FILE` — post-destroy leftovers check (the last two need boto3 and share `scripts/awsutil.py`; the baseline holds account IDs, so keep it outside the repo; `*baseline*.txt` is gitignored). Deploys, destroys and bootstraps are run manually by the project owner.
- `tests/unit/` — stack tests use `aws_cdk.assertions.Template.from_stack(...)` to assert on the synthesized template, no AWS access needed (region-only environment, no account). Handler tests use botocore's `Stubber`; `conftest.py` forces fake credentials, `us-east-1`, null AWS config files and blocks network access for every test.
- `cdk.json` — besides the app command, holds CDK feature flags in `context`; these change synth output, so don't edit them casually.
- `mypy.ini` — excludes `cdk.out/`, which holds copies of Lambda asset code after synth and would cause duplicate-module errors.

## CI

`.github/workflows/check.yml` runs `make install` and `make check` on pushes to `main` and pull requests. It must stay credential-free: no secrets, no AWS roles, read-only repository permissions and no deploy commands (`tests/unit/test_ci_workflow.py` guards this). Every `uses:` line is pinned to a full 40-character commit SHA followed by a version comment, e.g. `actions/checkout@<sha> # v7.0.1`, because a tag can be moved to different code; the same test fails on a tag, a branch or a missing comment. Dependabot (`.github/dependabot.yml`) opens a weekly pull request for newer versions of the actions, changing the SHA and the comment together; it lists the `github-actions` ecosystem only, and a test keeps it that way. To update by hand, take the SHA of the new release's tag and change both.

## Roadmap

Work follows `docs/ROADMAP.md`. Read it before starting a task, and tick a step only when its "done when" check passes. Design decisions and their reasoning are in `docs/DESIGN.md`.

## Python version

Python 3.14 everywhere: the `.venv`, `mypy.ini`, `ruff.toml`, and every Lambda function's runtime (`Runtime.PYTHON_3_14`). Change them together, never one alone.

## Browser script

`functions/static/app.js` is the only JavaScript in this project: plain, dependency-free, no build step, always loaded as a file from the API, never inline. Its jobs: request an upload link, upload the visitor's file directly to S3 with it, poll for the result, show the result with `textContent` only (never `innerHTML`, so a file containing `<script>` displays as harmless text), and show a friendly message when something fails. It holds no secrets. Its checks are for user experience only: size, type and expiry are enforced by the presigned POST conditions in S3.
