"""Upload obituary photos to S3 (hamster-storage1 bucket).

Uses separate S3 credentials from .env (not the AWS Secrets Manager creds).
Photos are stored under the claude/legacy/{site_id}/ prefix.
"""

import os
from io import BytesIO
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from utils.logger import get_logger

logger = get_logger(__name__)

S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "hamster-storage1")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "claude/legacy")

# Content type mapping for common image extensions
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_s3_client = None


def _get_client():
    """Lazy-init S3 client with dedicated credentials."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    if not S3_ACCESS_KEY_ID or not S3_SECRET_ACCESS_KEY:
        logger.warning("S3 credentials not configured — photo upload disabled")
        return None
    _s3_client = boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )
    return _s3_client


def _guess_extension(photo_url, content_type=""):
    """Determine file extension from URL path or Content-Type header."""
    path = urlparse(photo_url).path
    for ext in _CONTENT_TYPES:
        if path.lower().endswith(ext):
            return ext
    # Fallback to content type
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def upload_photo(session, photo_url, site_id, obit_id):
    """Download a photo from Legacy CDN and upload to S3.

    Args:
        session: requests.Session (with rate limiting).
        photo_url: Original CDN URL (e.g. https://cache.legacy.net/photos/12345.jpg).
        site_id: Market site ID (used in S3 key path).
        obit_id: Obituary row ID (used in S3 key filename).

    Returns:
        S3 public URL string, or None on failure.
    """
    client = _get_client()
    if not client:
        return None

    try:
        # Download image (no rate limiting — CDN, not Legacy.com)
        resp = session.get(photo_url, timeout=15)
        if resp.status_code != 200:
            logger.warning("[%s] Photo download failed (%d): %s", site_id, resp.status_code, photo_url)
            return None

        content_type = resp.headers.get("Content-Type", "")
        ext = _guess_extension(photo_url, content_type)
        s3_content_type = _CONTENT_TYPES.get(ext, "image/jpeg")

        key = f"{S3_PREFIX}/{site_id}/{obit_id}{ext}"

        client.upload_fileobj(
            BytesIO(resp.content),
            S3_BUCKET,
            key,
            ExtraArgs={
                "ContentType": s3_content_type,
                "ACL": "public-read",
            },
        )

        s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"
        return s3_url

    except ClientError as e:
        logger.warning("[%s] S3 upload failed for obit %s: %s", site_id, obit_id, e)
        return None
    except Exception as e:
        logger.warning("[%s] Photo upload error for obit %s: %s", site_id, obit_id, e)
        return None
