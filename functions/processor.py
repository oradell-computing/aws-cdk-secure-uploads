"""S3 event (uploads/ only): write results/<id>.json for each uploaded file.

Safe to run twice for the same event: the output key is derived from the input key
and the output depends only on the input bytes, so a repeat rewrites the same result.
"""

import json
import logging
import urllib.parse
from typing import Any

import aws
import fingerprint
import ids

logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Lambda's root logger defaults to WARNING


def process(data: bytes) -> dict[str, Any]:
    """Expected bad input becomes an error result the page can show; anything else raises."""
    try:
        return {"status": "ok", "fingerprint": fingerprint.fingerprint(data)}
    except fingerprint.InvalidTextError as error:
        return {"status": "error", "error": str(error)}


def _result_for(upload_id: str) -> dict[str, Any]:
    # Ask for one byte more than the limit so an oversized object is detectable.
    obj = aws.s3_client().get_object(
        Bucket=aws.bucket_name(),
        Key=ids.upload_key(upload_id),
        Range=f"bytes=0-{ids.MAX_UPLOAD_BYTES}",
    )
    data = obj["Body"].read()
    if len(data) > ids.MAX_UPLOAD_BYTES:
        return {"status": "error", "error": "file is larger than 100 KB"}
    return process(data)


def handler(event: dict[str, Any], context: object) -> None:
    for record in event["Records"]:
        # Keys in S3 events are URL-encoded ('+' for spaces, %XX for the rest).
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        upload_id = ids.id_from_upload_key(key)
        if upload_id is None:
            logger.warning("ignoring unexpected key %r", key)
            continue

        result = _result_for(upload_id)
        aws.s3_client().put_object(
            Bucket=aws.bucket_name(),
            Key=ids.result_key(upload_id),
            Body=json.dumps(result, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("wrote %s", ids.result_key(upload_id))
