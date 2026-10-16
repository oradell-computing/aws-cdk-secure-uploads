import io
import json

import pytest
import results
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from .helpers import BUCKET, VALID_ID, body_json


def get(result_id: str) -> dict:
    return results.handler({"pathParameters": {"id": result_id}}, None)


def test_returns_the_stored_result(s3_stub: Stubber) -> None:
    stored = json.dumps({"status": "ok"}).encode()
    s3_stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(stored), len(stored))},
        {"Bucket": BUCKET, "Key": f"results/{VALID_ID}.json"},
    )
    response = get(VALID_ID)
    assert response["statusCode"] == 200
    assert body_json(response) == {"status": "ok"}
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Type"].startswith("application/json")


@pytest.mark.parametrize("code", ["NoSuchKey", "AccessDenied"])
def test_a_missing_result_is_pending(s3_stub: Stubber, code: str) -> None:
    # Without s3:ListBucket, S3 reports a missing key as AccessDenied, so both mean "pending".
    s3_stub.add_client_error("get_object", service_error_code=code)
    response = get(VALID_ID)
    assert response["statusCode"] == 200
    assert body_json(response) == {"status": "pending"}
    assert response["headers"]["Cache-Control"] == "no-store"  # never cache "pending"


def test_other_s3_errors_surface(s3_stub: Stubber) -> None:
    s3_stub.add_client_error("get_object", service_error_code="InternalError")
    with pytest.raises(ClientError):
        get(VALID_ID)


@pytest.mark.parametrize(
    "bad_id",
    ["", "abc", VALID_ID.upper(), "../" + VALID_ID[3:], VALID_ID + "0", "g" * 32],
)
def test_invalid_ids_are_rejected_before_s3_is_called(
    s3_stub: Stubber, bad_id: str
) -> None:
    response = get(bad_id)  # the stub has no responses queued, so an S3 call would fail
    assert response["statusCode"] == 400
    assert body_json(response) == {"error": "invalid id"}


def test_responses_carry_the_security_headers(s3_stub: Stubber) -> None:
    headers = get("bad")["headers"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in headers
