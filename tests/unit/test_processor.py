import io
import json
from typing import Any

import fingerprint
import ids
import processor
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from .helpers import BUCKET, VALID_ID

UPLOAD_KEY = f"uploads/{VALID_ID}.txt"
RESULT_KEY = f"results/{VALID_ID}.json"


def event_for(*keys: str) -> dict[str, Any]:
    return {"Records": [{"s3": {"object": {"key": key}}} for key in keys]}


def expect_get(stub: Stubber, data: bytes) -> None:
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data))},
        {"Bucket": BUCKET, "Key": UPLOAD_KEY, "Range": "bytes=0-102400"},
    )


def ok_body(data: bytes) -> bytes:
    """The result the processor should store for this upload."""
    result = {"status": "ok", "fingerprint": fingerprint.fingerprint(data)}
    return json.dumps(result, sort_keys=True).encode()


def expect_put(stub: Stubber, body: bytes) -> None:
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": RESULT_KEY,
            "Body": body,
            "ContentType": "application/json",
        },
    )


def test_reads_the_upload_and_writes_the_result_under_results(s3_stub: Stubber) -> None:
    expect_get(s3_stub, b"hello")
    expect_put(s3_stub, ok_body(b"hello"))
    processor.handler(event_for(UPLOAD_KEY), None)


def test_url_encoded_keys_are_decoded_first(s3_stub: Stubber) -> None:
    expect_get(s3_stub, b"hello")
    expect_put(s3_stub, ok_body(b"hello"))
    processor.handler(event_for(UPLOAD_KEY.replace("/", "%2F")), None)


def test_a_repeated_event_rewrites_the_same_result(s3_stub: Stubber) -> None:
    # S3 can deliver an event twice; both runs must write identical bytes to the same key.
    for _ in range(2):
        expect_get(s3_stub, b"hello")
        expect_put(s3_stub, ok_body(b"hello"))
    processor.handler(event_for(UPLOAD_KEY, UPLOAD_KEY), None)


@pytest.mark.parametrize(
    "key",
    [
        RESULT_KEY,  # our own output must never be processed again
        f"uploads/{VALID_ID}.json",
        f"uploads/nested/{VALID_ID}.txt",
        "uploads/not-an-id.txt",
        "other/file.txt",
    ],
)
def test_unexpected_keys_are_skipped_without_touching_s3(
    s3_stub: Stubber, key: str
) -> None:
    processor.handler(event_for(key), None)  # the stub has no responses queued


def test_an_oversized_object_gets_an_error_result(s3_stub: Stubber) -> None:
    expect_get(s3_stub, b"x" * (ids.MAX_UPLOAD_BYTES + 1))
    expect_put(s3_stub, b'{"error": "file is larger than 100 KB", "status": "error"}')
    processor.handler(event_for(UPLOAD_KEY), None)


def test_an_object_at_the_limit_is_processed(s3_stub: Stubber) -> None:
    at_limit = b"x " * (ids.MAX_UPLOAD_BYTES // 2)
    expect_get(s3_stub, at_limit)
    expect_put(s3_stub, ok_body(at_limit))
    processor.handler(event_for(UPLOAD_KEY), None)


def test_s3_failures_are_not_swallowed(s3_stub: Stubber) -> None:
    s3_stub.add_client_error("get_object", service_error_code="InternalError")
    with pytest.raises(Exception, match="InternalError"):
        processor.handler(event_for(UPLOAD_KEY), None)


def test_the_stored_result_has_the_fingerprint_fields(s3_stub: Stubber) -> None:
    data = b"Hello, fingerprint. Hello again.\n"
    expected = json.loads(ok_body(data))
    assert expected["status"] == "ok"
    assert expected["fingerprint"]["word_count"] == 4
    assert expected["fingerprint"]["top_words"][0] == {"word": "hello", "count": 2}
    expect_get(s3_stub, data)
    expect_put(s3_stub, ok_body(data))
    processor.handler(event_for(UPLOAD_KEY), None)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"caf\xe9", "The file is not valid UTF-8 text."),
        (b"a\x00b", "The file contains binary data, not text."),
        (b"", "No words were found in the file."),
    ],
)
def test_text_that_cannot_be_fingerprinted_gets_an_error_result(
    s3_stub: Stubber, data: bytes, message: str
) -> None:
    expect_get(s3_stub, data)
    expect_put(s3_stub, json.dumps({"error": message, "status": "error"}).encode())
    processor.handler(event_for(UPLOAD_KEY), None)


def test_unexpected_errors_are_not_turned_into_results(
    s3_stub: Stubber, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(data: bytes) -> dict[str, Any]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(fingerprint, "fingerprint", broken)
    expect_get(s3_stub, b"hello")  # no put is queued: writing one would fail the test
    with pytest.raises(RuntimeError, match="a bug"):
        processor.handler(event_for(UPLOAD_KEY), None)
