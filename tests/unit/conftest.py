"""Shared fixtures. Every test runs with fake credentials, a fixed region and no network."""

import socket
from collections.abc import Iterator

import aws
import pytest
from botocore.stub import Stubber

from .helpers import BUCKET, UPLOAD_ORIGIN


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fake credentials and region; never a real profile; any socket connect fails."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BUCKET_NAME", BUCKET)
    monkeypatch.setenv("UPLOAD_ORIGIN", UPLOAD_ORIGIN)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    aws.s3_client.cache_clear()
    yield
    aws.s3_client.cache_clear()


@pytest.fixture
def s3_stub() -> Iterator[Stubber]:
    """Stubber on the handlers' S3 client: any call without a queued response fails."""
    with Stubber(aws.s3_client()) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()
