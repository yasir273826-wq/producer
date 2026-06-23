"""
b2_client.py
─────────────────────────────────────────────────────────────────────────────
Shared Backblaze B2 storage helper — copy this file verbatim into BOTH the
producer repo and the uploader repo (same reason as supabase_client.py:
two separate GitHub repos, no shared import path).

Backblaze B2 exposes an S3-compatible API, so this uses plain `boto3`
instead of the native b2sdk. This keeps the dependency list small and the
code identical to "any other S3 bucket" — Backblaze's S3-compatible
endpoints are officially documented and stable.

Replaces what used to be Supabase Storage (and before that, Cloudflare R2)
for the actual clip .mp4 files. Supabase Postgres is still used for all
metadata (processed_videos / clips / daily_slots tables) — only the BLOB
storage moved.

Env vars required (set as GitHub Secrets in BOTH repos):
  B2_ENDPOINT_URL    – e.g. https://s3.us-west-004.backblazeb2.com
                        (Bucket Details page on the B2 dashboard shows the
                        exact endpoint for your bucket's region)
  B2_KEY_ID          – Application Key ID (Account → App Keys)
  B2_APPLICATION_KEY – Application Key secret (shown once at creation time)
  B2_BUCKET_NAME     – name of the bucket holding clip .mp4 files

Bucket setup (one-time, in the Backblaze dashboard):
  1. Create a bucket (Private is fine — service-style access only).
  2. Create an Application Key scoped to that bucket
     (Account → App Keys → Add a New Application Key).
  3. Note the "S3 Endpoint" shown on the bucket's detail page — this is
     B2_ENDPOINT_URL. It encodes the region, e.g.:
       https://s3.us-west-004.backblazeb2.com
"""

import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

BUCKET_NAME_ENV = "B2_BUCKET_NAME"


def _get_bucket_name() -> str:
    bucket = os.environ.get(BUCKET_NAME_ENV, "").strip()
    if not bucket:
        raise RuntimeError(f"{BUCKET_NAME_ENV} must be set as a GitHub Secret.")
    return bucket


def get_client():
    """
    Build a boto3 S3 client pointed at Backblaze B2's S3-compatible endpoint.
    """
    endpoint_url = os.environ.get("B2_ENDPOINT_URL", "").strip()
    key_id = os.environ.get("B2_KEY_ID", "").strip()
    app_key = os.environ.get("B2_APPLICATION_KEY", "").strip()

    if not endpoint_url or not key_id or not app_key:
        raise RuntimeError(
            "B2_ENDPOINT_URL, B2_KEY_ID and B2_APPLICATION_KEY must all be set "
            "as GitHub Secrets (Settings → Secrets → Actions)."
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=key_id,
        aws_secret_access_key=app_key,
        config=Config(signature_version="s3v4"),
    )


def upload_clip_to_storage(client, local_path: str, storage_path: str) -> str:
    """
    Upload a local .mp4 file to the B2 bucket.
    Returns storage_path (used as the DB foreign key, same as before).
    """
    bucket = _get_bucket_name()
    try:
        client.upload_file(
            Filename=local_path,
            Bucket=bucket,
            Key=storage_path,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    except ClientError as e:
        raise RuntimeError(f"B2 upload failed for {storage_path}: {e}") from e
    return storage_path


def download_clip_from_storage(client, storage_path: str, local_path: str) -> None:
    """
    Download a clip from the B2 bucket to a local path.
    Raises on failure.
    """
    bucket = _get_bucket_name()
    try:
        client.download_file(Bucket=bucket, Key=storage_path, Filename=local_path)
    except ClientError as e:
        raise RuntimeError(f"B2 download failed for {storage_path}: {e}") from e


def delete_clip_from_storage(client, storage_path: str) -> None:
    """Delete a clip object from the B2 bucket (called after successful TikTok upload)."""
    bucket = _get_bucket_name()
    try:
        client.delete_object(Bucket=bucket, Key=storage_path)
    except ClientError as e:
        print(f"  ⚠️  Could not delete {storage_path} from B2: {e}")
