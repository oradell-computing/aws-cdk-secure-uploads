import base64
import json
from datetime import UTC, datetime, timedelta

import ids
import presign

from .helpers import BUCKET, UPLOAD_ORIGIN, body_json


def post() -> dict:
    return body_json(presign.handler({}, None))


def policy_of(payload: dict) -> dict:
    return json.loads(base64.b64decode(payload["fields"]["policy"]))


def test_returns_a_server_generated_id_and_matching_key() -> None:
    payload = post()
    assert ids.is_valid_id(payload["id"])
    assert payload["fields"]["key"] == f"uploads/{payload['id']}.txt"


def test_every_call_gets_a_new_key() -> None:
    assert post()["id"] != post()["id"]


def test_upload_url_is_the_regional_bucket_origin_used_by_the_csp_and_cors() -> None:
    # A different host (e.g. the global s3.amazonaws.com) could redirect and break CORS.
    assert post()["url"] == f"{UPLOAD_ORIGIN}/"


def test_policy_limits_type_size_bucket_and_key() -> None:
    payload = post()
    conditions = policy_of(payload)["conditions"]
    assert {"Content-Type": "text/plain"} in conditions
    assert ["content-length-range", 1, 100 * 1024] in conditions
    assert {"bucket": BUCKET} in conditions
    assert {"key": f"uploads/{payload['id']}.txt"} in conditions
    assert payload["fields"]["Content-Type"] == "text/plain"


def test_policy_expires_in_five_minutes() -> None:
    expiration = policy_of(post())["expiration"]
    expires = datetime.strptime(expiration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    remaining = expires - datetime.now(UTC)
    assert timedelta(minutes=4, seconds=50) < remaining <= timedelta(minutes=5)


def test_response_is_not_cacheable_and_has_security_headers() -> None:
    headers = presign.handler({}, None)["headers"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Type"].startswith("application/json")
