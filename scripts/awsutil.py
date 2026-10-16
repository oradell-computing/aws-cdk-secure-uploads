"""Helpers shared by the scripts that call AWS (they need boto3, unlike smoke_test.py)."""

from collections.abc import Iterable
from typing import Any

from botocore.exceptions import MissingDependencyException

MISSING_CRT_MESSAGE = """\
boto3 can't read your AWS credentials because a package is missing.

Credentials created by `aws login` can only be read with the optional botocore[crt]
package, and it isn't installed in this environment.

Fix: run `make install`. It installs the version pinned in requirements-dev.txt.

Original error: {error}"""


def make_clients(session: Any, names: Iterable[str]) -> dict[str, Any]:
    """One boto3 client per service name, with a clear message if the CRT package is missing.

    boto3 loads credentials when a client is created, and raises MissingDependencyException
    there when the credentials came from `aws login` and botocore[crt] isn't installed.
    Any other missing dependency is unexpected and is left to surface as it is.
    """
    try:
        return {name: session.client(name) for name in names}
    except MissingDependencyException as error:
        if "crt" not in str(error).lower():
            raise
        raise SystemExit(MISSING_CRT_MESSAGE.format(error=error)) from None
