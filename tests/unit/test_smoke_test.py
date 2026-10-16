"""The smoke test only runs against a deployed stack, but its helpers are checked here."""

import json
from email.message import Message
from typing import Any

import pytest
import smoke_test

HEADERS = Message()


def fake_request(responses: list[tuple[int, dict[str, Any]]]) -> Any:
    """A stand-in for smoke_test.request that replays the given (status, JSON body) pairs."""
    queue = list(responses)

    def request(url: str, *args: Any, **kwargs: Any) -> tuple[int, Message, bytes]:
        status, body = queue.pop(0)
        return status, HEADERS, json.dumps(body).encode()

    return request


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke_test.time, "sleep", lambda seconds: None)


def test_polling_stops_at_the_first_result_that_is_not_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = (200, {"status": "pending"})
    done = (200, {"status": "ok", "fingerprint": {}})
    monkeypatch.setattr(smoke_test, "request", fake_request([pending, pending, done]))
    status, _, result = smoke_test.wait_for_result("https://site", "id")
    assert (status, result["status"]) == (200, "ok")


def test_an_error_result_also_ends_the_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke_test, "request", fake_request([(200, {"status": "error", "error": "x"})])
    )
    assert smoke_test.wait_for_result("https://site", "id")[2]["error"] == "x"


def test_a_non_200_ends_the_polling_with_that_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke_test, "request", fake_request([(500, {})]))
    status, _, result = smoke_test.wait_for_result("https://site", "id")
    assert (status, result) == (500, {})


def test_polling_gives_up_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke_test, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        smoke_test, "request", fake_request([(200, {"status": "pending"})] * 3)
    )
    status, _, result = smoke_test.wait_for_result("https://site", "id")
    assert (status, result) == (200, {"status": "pending"})


def test_multipart_puts_the_file_last_as_s3_requires() -> None:
    body, content_type = smoke_test.multipart({"key": "k", "policy": "p"}, b"DATA")
    boundary = content_type.split("boundary=")[1]
    parts = body.decode().split(f"--{boundary}")
    names = [
        part.split('name="')[1].split('"')[0] for part in parts if 'name="' in part
    ]
    assert names == ["key", "policy", "file"]
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_sample_matches_what_the_checks_expect() -> None:
    assert smoke_test.SAMPLE.count(b"Hello") == 2
