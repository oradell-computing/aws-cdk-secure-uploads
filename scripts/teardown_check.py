"""Check that nothing is left behind after `cdk destroy`. Needs boto3 (installed in the venv).

Normally run through the Makefile: `make baseline` before the first deploy, and
`make teardown-check` after `make destroy`. By hand, use the project's interpreter
(.venv/bin/python, which has boto3). Before the first deploy, record which resources
already carry the project tag:

    .venv/bin/python scripts/teardown_check.py --write-baseline ~/demo-serverless-baseline.txt

After `cdk destroy`, compare against that file:

    .venv/bin/python scripts/teardown_check.py --baseline ~/demo-serverless-baseline.txt

The baseline holds resource ARNs, which contain your account ID. Keep it outside the
repository (the script refuses to write it inside) and never commit it.

Credentials come from the usual places: pass --profile and --region, or set
AWS_PROFILE and AWS_DEFAULT_REGION. Nothing is hardcoded. Exits non-zero if anything
is left, so it can gate a script.
"""

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from awsutil import make_clients
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STACK_NAME = "DemoServerlessStack"
TAG_KEY = "project"
TAG_VALUE = "demo-serverless"
BASELINE_HEADER = (
    "# demo-serverless tag baseline. Contains account IDs: never commit this file.\n"
)


@dataclass
class Finding:
    """The result of one check: it passes when there is nothing left."""

    check: str
    left_behind: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.left_behind


# --- pure helpers ---------------------------------------------------------------------------


def parse_baseline(text: str) -> set[str]:
    """ARNs from a baseline file: one per line, blank lines and # comments ignored."""
    lines = (line.strip() for line in text.splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def format_baseline(arns: Iterable[str]) -> str:
    return BASELINE_HEADER + "".join(f"{arn}\n" for arn in sorted(arns))


def new_since(current: Iterable[str], baseline: Iterable[str]) -> list[str]:
    """Items present now that were not in the baseline, sorted."""
    return sorted(set(current) - set(baseline))


def with_prefix(names: Iterable[str], prefix: str) -> list[str]:
    return sorted(name for name in names if name.startswith(prefix))


def valid_stack_name(name: str) -> bool:
    return bool(name) and all(char.isalnum() or char == "-" for char in name)


# --- AWS lookups (clients are passed in, so tests can stub them) -----------------------------


def stack_status(cloudformation: Any, stack_name: str) -> str | None:
    """The stack's status, or None if it doesn't exist (CloudFormation reports a deleted
    stack as a ValidationError "does not exist")."""
    try:
        stacks = cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError as error:
        details = error.response["Error"]
        if (
            details["Code"] == "ValidationError"
            and "does not exist" in details["Message"]
        ):
            return None
        raise
    return str(stacks[0]["StackStatus"])


def tagged_arns(tagging: Any) -> set[str]:
    pages = tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": TAG_KEY, "Values": [TAG_VALUE]}]
    )
    return {
        mapping["ResourceARN"]
        for page in pages
        for mapping in page["ResourceTagMappingList"]
    }


def log_group_names(logs: Any, prefixes: Sequence[str]) -> list[str]:
    names: list[str] = []
    for prefix in prefixes:
        pages = logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=prefix
        )
        names += [
            group["logGroupName"] for page in pages for group in page["logGroups"]
        ]
    return sorted(set(names))


def role_names(iam: Any) -> list[str]:
    pages = iam.get_paginator("list_roles").paginate()
    return [role["RoleName"] for page in pages for role in page["Roles"]]


# --- the checks ---------------------------------------------------------------------------------


def run_checks(
    clients: dict[str, Any], stack_name: str, baseline: set[str]
) -> list[Finding]:
    status = stack_status(clients["cloudformation"], stack_name)
    stack = Finding(
        f"stack {stack_name} is gone",
        [] if status is None else [f"{stack_name} still exists ({status})"],
    )
    tags = Finding(
        f"no resources tagged {TAG_KEY}={TAG_VALUE} beyond the baseline",
        new_since(tagged_arns(clients["resourcegroupstaggingapi"]), baseline),
    )
    # Our log groups are named "<StackName>-..."; Lambda's own are "/aws/lambda/<StackName>-...".
    log_prefixes = [stack_name, f"/aws/lambda/{stack_name}"]
    log_groups = Finding(
        f"no log groups starting with {' or '.join(log_prefixes)}",
        log_group_names(clients["logs"], log_prefixes),
    )
    roles = Finding(
        f"no IAM roles starting with {stack_name}-",
        with_prefix(role_names(clients["iam"]), f"{stack_name}-"),
    )
    return [stack, tags, log_groups, roles]


def report(findings: Sequence[Finding]) -> int:
    for finding in findings:
        print(f"{'PASS' if finding.passed else 'FAIL'}  {finding.check}")
        for item in finding.left_behind:
            print(f"        left behind: {item}")
    failed = [finding for finding in findings if not finding.passed]
    if failed:
        print(f"\n{len(failed)} check(s) failed.")
        print(
            "The tagging API can lag by a few minutes after a delete; if only the tag "
            "check failed, wait a bit and run it again."
        )
        return 1
    print("\nNothing left behind.")
    return 0


# --- command line -------------------------------------------------------------------------------


def baseline_exists_message(path: Path) -> str:
    return (
        f"A baseline already exists at {path}, so nothing was written and nothing was "
        "overwritten.\n"
        "It was probably recorded before an earlier deploy. If `make teardown-check` passed "
        "after that deploy was destroyed, it is still the right baseline: skip this step and "
        "go on to `make deploy`.\n"
        "To start over, delete that file yourself (or use BASELINE=<another path>) and run "
        "this again. Only do that once nothing from the demo is left in the account: a "
        "baseline recorded while leftovers exist would count them as normal and hide them "
        "from the teardown check."
    )


def check_baseline_target(path: Path) -> None:
    """Refuse, before any AWS call, to write a baseline that would leak or clobber."""
    if path.resolve().is_relative_to(REPO_ROOT):
        raise SystemExit(
            f"Refusing to write {path} inside the repository: a baseline contains "
            "account IDs. Choose a path outside it."
        )
    if path.exists():
        raise SystemExit(baseline_exists_message(path))


def write_baseline(path: Path, arns: set[str]) -> None:
    """Create the baseline file. Never overwrites one, and never writes inside the repo."""
    check_baseline_target(path)
    try:
        with path.open("x", encoding="utf-8") as file:  # "x": never overwrite
            file.write(format_baseline(arns))
    except FileExistsError:  # created by someone else since the check above
        raise SystemExit(baseline_exists_message(path)) from None
    print(f"Wrote {len(arns)} ARN(s) to {path}. Do not commit this file.")


def read_baseline(path: Path) -> set[str]:
    try:
        return parse_baseline(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"Baseline file {path} not found. Create it before deploying with "
            "--write-baseline."
        ) from None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check nothing is left after cdk destroy."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--baseline", type=Path, help="compare against this baseline file"
    )
    mode.add_argument(
        "--write-baseline", type=Path, help="record the currently tagged resources here"
    )
    parser.add_argument("--stack-name", default=DEFAULT_STACK_NAME)
    parser.add_argument(
        "--profile", help="AWS CLI profile (default: AWS_PROFILE or default chain)"
    )
    parser.add_argument(
        "--region", help="AWS region (default: AWS_DEFAULT_REGION or CLI config)"
    )
    args = parser.parse_args(argv)
    if not valid_stack_name(args.stack_name):
        parser.error("--stack-name may only contain letters, digits and hyphens")
    return args


def main(
    argv: Sequence[str],
    session_factory: Callable[..., Any] = boto3.Session,
) -> int:
    args = parse_args(argv)
    if args.write_baseline:
        check_baseline_target(args.write_baseline)  # fail fast, before any AWS call
    session = session_factory(profile_name=args.profile, region_name=args.region)
    clients = make_clients(
        session, ("cloudformation", "resourcegroupstaggingapi", "logs", "iam")
    )
    if args.write_baseline:
        write_baseline(
            args.write_baseline, tagged_arns(clients["resourcegroupstaggingapi"])
        )
        return 0
    baseline = read_baseline(args.baseline)
    return report(run_checks(clients, args.stack_name, baseline))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
