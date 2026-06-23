"""
r2_client.py
─────────────────────────────────────────────────────────────────────────────
Shared Cloudflare R2 storage helper — copy this file verbatim into BOTH the
producer repo and the uploader repo (same reason as supabase_client.py:
two separate GitHub repos, no shared import path).

Cloudflare R2 exposes an S3-compatible API, so this uses plain `boto3`
instead of any Cloudflare-specific SDK. This keeps the dependency list small
and the code straightforward — R2's S3-compatible endpoint is officially
documented and stable.

Replaces Backblaze B2 for the actual clip .mp4 files. Supabase Postgres is
still used for all metadata (processed_videos / clips / daily_slots tables)
— only the BLOB storage changed.

Env vars required (set as GitHub Secrets in BOTH repos):
  R2_ENDPOINT_URL       – https://<account_id>.r2.cloudflarestorage.com
                          (found in R2 dashboard → your bucket → Settings)
  R2_ACCESS_KEY_ID      – R2 API Token Access Key ID
                          (R2 dashboard → Manage R2 API Tokens → Create token)
  R2_SECRET_ACCESS_KEY  – R2 API Token Secret Access Key
                          (shown once at token creation time — save it!)
  R2_BUCKET_NAME        – name of the R2 bucket holding clip .mp4 files

Bucket & token setup (one-time, in the Cloudflare dashboard):
  1. Go to https://dash.cloudflare.com → R2 Object Storage → Create bucket.
  2. Go to R2 → Manage R2 API Tokens → Create API Token.
     Give it "Object Read & Write" permissions scoped to your bucket.
  3. Copy the "Access Key ID" and "Secret Access Key" shown (one-time reveal).
  4. The endpoint URL format is:
       https://<your-account-id>.r2.cloudflarestorage.com
     Your account ID is visible in the R2 dashboard URL or Overview page.
"""

import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

BUCKET_NAME_ENV = "R2_BUCKET_NAME"


def _get_bucket_name() -> str:
    bucket = os.environ.get(BUCKET_NAME_ENV, "").strip()
    if not bucket:
        raise RuntimeError(f"{BUCKET_NAME_ENV} must be set as a GitHub Secret.")
    return bucket


def get_client():
    """
    Build a boto3 S3 client pointed at Cloudflare R2's S3-compatible endpoint.
    """
    endpoint_url = os.environ.get("R2_ENDPOINT_URL", "").strip()
    access_key_id = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

    if not endpoint_url or not access_key_id or not secret_access_key:
        raise RuntimeError(
            "R2_ENDPOINT_URL, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must all be set "
            "as GitHub Secrets (Settings → Secrets → Actions)."
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",  # R2 requires "auto" as the region
    )


def upload_clip_to_storage(client, local_path: str, storage_path: str) -> str:
    """
    Upload a local .mp4 file to the R2 bucket.
    Returns storage_path (used as the DB foreign key).
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
        raise RuntimeError(f"R2 upload failed for {storage_path}: {e}") from e
    return storage_path


def download_clip_from_storage(client, storage_path: str, local_path: str) -> None:
    """
    Download a clip from the R2 bucket to a local path.
    Raises on failure.
    """
    bucket = _get_bucket_name()
    try:
        client.download_file(Bucket=bucket, Key=storage_path, Filename=local_path)
    except ClientError as e:
        raise RuntimeError(f"R2 download failed for {storage_path}: {e}") from e


def delete_clip_from_storage(client, storage_path: str) -> None:
    """Delete a clip object from the R2 bucket (called after successful TikTok upload)."""
    bucket = _get_bucket_name()
    try:
        client.delete_object(Bucket=bucket, Key=storage_path)
    except ClientError as e:
        print(f"  ⚠️  Could not delete {storage_path} from R2: {e}")
