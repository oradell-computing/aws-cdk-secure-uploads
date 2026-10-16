import pytest
from awsutil import make_clients
from botocore.exceptions import MissingDependencyException


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def client(self, name: str) -> str:
        if self.error:
            raise self.error
        return f"client:{name}"


def test_clients_are_built_by_name() -> None:
    assert make_clients(FakeSession(), ["logs", "iam"]) == {
        "logs": "client:logs",
        "iam": "client:iam",
    }


def test_a_missing_crt_package_gets_a_clear_message() -> None:
    error = MissingDependencyException(
        msg="Using the login credential provider requires an additional dependency. "
        'You will need to pip install "botocore[crt]" before proceeding.'
    )
    with pytest.raises(SystemExit) as raised:
        make_clients(FakeSession(error), ["logs"])
    message = str(raised.value)
    assert "aws login" in message
    assert "botocore[crt]" in message
    assert "make install" in message
    assert "Original error:" in message and "login credential provider" in message


def test_other_missing_dependencies_are_not_disguised() -> None:
    error = MissingDependencyException(msg="needs something else entirely")
    with pytest.raises(MissingDependencyException, match="something else"):
        make_clients(FakeSession(error), ["logs"])


def test_other_errors_propagate() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        make_clients(FakeSession(RuntimeError("boom")), ["logs"])
