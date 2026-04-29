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

# Presigned upload-part URLs are short-lived — they only need to live long
# enough for the browser to finish uploading one part. Cap at 1 hour to keep
# leaked URLs useless quickly while still allowing for very slow connections.
PRESIGN_UPLOAD_PART_EXPIRY = 21600  # 6h — long enough that slow connections don't expire mid-upload

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


def head_object(key: str) -> dict:
    """Return HeadObject response (ContentLength, ETag, etc.) or raise."""
    return get_client().head_object(Bucket=R2_BUCKET, Key=key)


# ---------------------------------------------------------------------------
# Browser-direct multipart upload
# ---------------------------------------------------------------------------
#
# Flow:
#   1. Server calls create_multipart_upload(key) → upload_id.
#   2. Server presigns one URL per part via presign_upload_part(...).
#   3. Browser PUTs each part directly to R2, collecting ETags from response
#      headers. (The bucket must have CORS configured to expose the ETag
#      header — see configure_cors() below.)
#   4. Server calls complete_multipart_upload(key, upload_id, parts) once the
#      browser reports all parts done.
#
# If the browser drops out, server should call abort_multipart_upload(...) —
# otherwise R2 keeps the partial parts billable. A bucket lifecycle rule that
# aborts incomplete uploads after 24h is a good belt-and-suspenders.

def create_multipart_upload(key: str, content_type: str = "application/pdf") -> str:
    """Initiate a multipart upload. Returns the upload_id."""
    resp = get_client().create_multipart_upload(
        Bucket=R2_BUCKET, Key=key, ContentType=content_type,
    )
    return resp["UploadId"]


def presign_upload_part(key: str, upload_id: str, part_number: int) -> str:
    """Presign a single upload_part PUT URL. Browser PUTs the slice here."""
    return get_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=PRESIGN_UPLOAD_PART_EXPIRY,
    )


def complete_multipart_upload(key: str, upload_id: str, parts: list) -> dict:
    """Finalize a multipart upload.

    `parts` is a list of {"PartNumber": int, "ETag": str} dicts, in order.
    Returns the CompleteMultipartUpload response.
    """
    return get_client().complete_multipart_upload(
        Bucket=R2_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_multipart_upload(key: str, upload_id: str) -> None:
    """Best-effort abort. Swallows errors so callers can use this in cleanup."""
    try:
        get_client().abort_multipart_upload(
            Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
        )
    except ClientError as exc:
        logger.warning("abort_multipart_upload(%s, %s) failed: %s", key, upload_id, exc)


def configure_cors(allowed_origins: Iterable[str]) -> None:
    """One-shot helper: set the bucket CORS policy needed for browser-direct
    multipart uploads. Run this once after deploying or whenever the allowed
    origins list changes — e.g.:

        python -c "from storage import configure_cors; \
            configure_cors(['https://knightshiftai.com', 'http://localhost:8080'])"

    The ETag exposure is the part most people forget — without it, the browser
    can't read the per-part ETags it needs to send back to complete the upload.
    """
    rules = [{
        "AllowedMethods": ["GET", "PUT", "HEAD"],
        "AllowedOrigins": list(allowed_origins),
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3600,
    }]
    get_client().put_bucket_cors(
        Bucket=R2_BUCKET,
        CORSConfiguration={"CORSRules": rules},
    )
    logger.info("CORS configured on bucket %s for origins: %s",
                R2_BUCKET, list(allowed_origins))


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
