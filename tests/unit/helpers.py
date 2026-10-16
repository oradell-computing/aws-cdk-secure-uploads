"""Constants and helpers shared by the handler tests."""

import json
from typing import Any

BUCKET = "test-bucket"
UPLOAD_ORIGIN = f"https://{BUCKET}.s3.us-east-1.amazonaws.com"
VALID_ID = "0123456789abcdef0123456789abcdef"


def body_json(response: dict[str, Any]) -> Any:
    return json.loads(response["body"])
