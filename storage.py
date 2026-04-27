#!/usr/bin/env python3
"""
Knight Shift — Cloudflare R2 Storage Helpers
============================================
Thin wrapper over boto3 configured for Cloudflare R2 (S3-compatible).
Both the web process and RQ workers import this module — keep it lightweight
and free of Flask/RQ dependencies.

Object key layout:
    submissions/<submission_id>/uploads/<original-filename>.pdf
    submissions/<submission_id>/results/<output>.pdf
    submissions/<submission_id>/results/<output>.json
    submissions/<submission_id>/metadata.json
"""

import os
import logging
from typing import Iterable, Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from config import (
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_BUCKET, R2_ENDPOINT_URL, R2_SIGNED_URL_EXPIRY,
)

logger = logging.getLogger("nightshift.storage")


class StorageNotConfigured(RuntimeError):
    """Raised when R2 credentials/bucket are missing at call time."""


def _require_config():
    missing = [
        name for name, val in (
            ("R2_ACCOUNT_ID", R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
            ("R2_BUCKET", R2_BUCKET),
        )
        if not val
    ]
    if missing:
        raise StorageNotConfigured(
            "R2 storage is not configured — missing env vars: " + ", ".join(missing)
        )


_client = None


def get_client():
    """Return a cached boto3 S3 client pointed at R2.

    R2 uses signature v4 and the special region 'auto'. Per Cloudflare's
    docs, use addressing_style='virtual' for compatibility with presigned
    URLs that browsers will follow.
    """
    global _client
    if _client is None:
        _require_config()
        _client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _client


# ---------------------------------------------------------------------------
# Key conventions
# ---------------------------------------------------------------------------

def submission_prefix(submission_id: str) -> str:
    return f"submissions/{submission_id}/"


def upload_key(submission_id: str, filename: str) -> str:
    return f"{submission_prefix(submission_id)}uploads/{filename}"


def result_key(submission_id: str, filename: str) -> str:
    return f"{submission_prefix(submission_id)}results/{filename}"


def metadata_key(submission_id: str) -> str:
    return f"{submission_prefix(submission_id)}metadata.json"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def upload_file(local_path: str, key: str, content_type: Optional[str] = None) -> None:
    """Upload a local file to R2 under the given key. Multipart-aware."""
    extra_args = {"ContentType": content_type} if content_type else None
    get_client().upload_file(local_path, R2_BUCKET, key, ExtraArgs=extra_args)
    logger.info("Uploaded %s → r2://%s/%s", local_path, R2_BUCKET, key)


def download_file(key: str, local_path: str) -> None:
    """Download an object from R2 to a local path. Parent dir must exist."""
    get_client().download_file(R2_BUCKET, key, local_path)
    logger.info("Downloaded r2://%s/%s → %s", R2_BUCKET, key, local_path)


def put_bytes(data: bytes, key: str, content_type: str = "application/octet-stream") -> None:
    """Upload raw bytes (e.g. metadata.json contents) to R2."""
    get_client().put_object(
        Bucket=R2_BUCKET, Key=key, Body=data, ContentType=content_type,
    )


def get_bytes(key: str) -> bytes:
    obj = get_client().get_object(Bucket=R2_BUCKET, Key=key)
    return obj["Body"].read()


def object_exists(key: str) -> bool:
    try:
        get_client().head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def presigned_download_url(key: str, expires_in: Optional[int] = None) -> str:
    """Return a time-limited URL for downloading a private object."""
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key},
        ExpiresIn=expires_in or R2_SIGNED_URL_EXPIRY,
    )


def list_prefix(prefix: str) -> Iterable[dict]:
    """Yield {'Key', 'Size', 'LastModified'} for every object under prefix."""
    paginator = get_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            yield obj


def delete_prefix(prefix: str) -> int:
    """Delete every object under prefix. Returns count deleted."""
    keys = [{"Key": obj["Key"]} for obj in list_prefix(prefix)]
    if not keys:
        return 0
    # S3 delete_objects caps at 1000 keys per request.
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        get_client().delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch})
        deleted += len(batch)
    logger.info("Deleted %d objects under %s", deleted, prefix)
    return deleted
