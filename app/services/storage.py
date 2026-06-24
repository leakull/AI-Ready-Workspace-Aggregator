"""S3 / MinIO object storage.

Binary attachments live in object storage; Postgres keeps only the bucket+key
reference. This separation keeps the relational store small and lets large blobs
be served directly from S3.
"""

from __future__ import annotations

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client = None


def get_s3_client():
    """Lazily build a path-style S3 client (MinIO needs path-style addressing)."""
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
    return _client


def ensure_bucket(bucket: str | None = None) -> None:
    bucket = bucket or settings.s3_bucket
    client = get_s3_client()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)
        log.info("s3.bucket_created", bucket=bucket)


def upload_bytes(
    key: str, data: bytes, content_type: str | None = None, bucket: str | None = None
) -> tuple[str, str]:
    """Upload bytes and return (bucket, key)."""
    bucket = bucket or settings.s3_bucket
    extra = {"ContentType": content_type} if content_type else {}
    get_s3_client().put_object(Bucket=bucket, Key=key, Body=data, **extra)
    log.info("s3.uploaded", bucket=bucket, key=key, size=len(data))
    return bucket, key


def build_object_key(source_system: str, message_external_id: str, filename: str) -> str:
    """Deterministic key so re-downloading the same attachment overwrites in place."""
    safe_name = filename.replace("/", "_")
    return f"{source_system}/{message_external_id}/{safe_name}"
