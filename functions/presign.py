"""POST /uploads: hand the browser a short-lived presigned POST for one new upload key."""

from typing import Any

import aws
import ids
import responses

EXPIRES_SECONDS = 300
CONTENT_TYPE = "text/plain"


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # The request body is ignored: the server alone picks the key and the limits.
    upload_id = ids.new_id()
    post = aws.s3_client().generate_presigned_post(
        Bucket=aws.bucket_name(),
        Key=ids.upload_key(upload_id),
        Fields={"Content-Type": CONTENT_TYPE},
        Conditions=[
            {"Content-Type": CONTENT_TYPE},
            ["content-length-range", 1, ids.MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=EXPIRES_SECONDS,
    )
    return responses.json_response(
        200, {"id": upload_id, "url": post["url"], "fields": post["fields"]}
    )
