"""The one place that builds boto3 clients, so tests can swap them out."""

import functools
import os
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@functools.cache
def s3_client() -> S3Client:
    """S3 client on the bucket's regional, virtual-hosted endpoint.

    The presigned POST URL is built from this endpoint, so it is
    ``https://<bucket>.s3.<region>.amazonaws.com/``. The global endpoint can answer
    a brand-new bucket with a redirect, which breaks the browser's CORS upload.
    """
    region = os.environ["AWS_DEFAULT_REGION"]  # always set by the Lambda runtime
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def bucket_name() -> str:
    return os.environ["BUCKET_NAME"]


def upload_origin() -> str:
    """``https://<bucket>.s3.<region>.amazonaws.com``, shared with the CSP and CORS rule."""
    return os.environ["UPLOAD_ORIGIN"]
