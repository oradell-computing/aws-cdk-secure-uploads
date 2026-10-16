"""GET /results/{id}: return a stored result through the API, never straight from S3."""

import json
import logging
from typing import Any

import aws
import ids
import responses
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Lambda's root logger defaults to WARNING

# The function has no s3:ListBucket, so S3 answers AccessDenied (not NoSuchKey) for a
# key that doesn't exist yet. Both mean "not ready", so the page keeps polling.
_NOT_READY_CODES = {"NoSuchKey", "AccessDenied"}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    upload_id = event["pathParameters"]["id"]
    if not ids.is_valid_id(upload_id):
        return responses.json_response(400, {"error": "invalid id"})

    try:
        obj = aws.s3_client().get_object(
            Bucket=aws.bucket_name(), Key=ids.result_key(upload_id)
        )
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in _NOT_READY_CODES:
            raise
        logger.info("result %s not ready (S3 error %s)", upload_id, code)
        # 200, not 404: "not ready yet" is a normal answer while polling, not an error.
        return responses.json_response(200, {"status": "pending"})

    # Parse and re-serialize so the API only ever emits well-formed JSON.
    return responses.json_response(200, json.loads(obj["Body"].read()))
