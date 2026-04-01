"""Upload obituary photos to S3 (hamster-storage1 bucket).

Uses separate S3 credentials from .env (not the AWS Secrets Manager creds).
Photos are stored under the claude/legacy/{site_id}/ prefix.

For each photo, two versions are uploaded:
  - {id}.jpg          — original image
  - {id}_16x9.jpg     — 16:9 landscape for news stories (blurred background fill)
"""

import os
from io import BytesIO
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageFilter

from utils.logger import get_logger

logger = get_logger(__name__)

S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "hamster-storage1")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_PREFIX = os.environ.get("S3_PREFIX", "claude/legacy")

# 16:9 output size (standard HD landscape)
TARGET_W, TARGET_H = 1280, 720

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
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def _create_16x9(image_bytes):
    """Create a 16:9 (1280x720) version of a portrait/square photo.

    Places the sharp original centered on a blurred, stretched background.
    This is the standard approach used by TV news and Instagram for
    portrait photos in landscape frames.

    Args:
        image_bytes: Raw image bytes.

    Returns:
        bytes of the 16:9 JPEG image.
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    # Create blurred background: stretch original to fill 16:9 canvas
    bg = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))

    # Resize original to fit within canvas, preserving aspect ratio
    scale = min(TARGET_W / orig_w, TARGET_H / orig_h)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)
    sharp = img.resize((new_w, new_h), Image.LANCZOS)

    # Center the sharp image on the blurred background
    x_offset = (TARGET_W - new_w) // 2
    y_offset = (TARGET_H - new_h) // 2
    bg.paste(sharp, (x_offset, y_offset))

    buf = BytesIO()
    bg.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.getvalue()


def _upload_bytes(client, data, key, content_type="image/jpeg"):
    """Upload raw bytes to S3 with public-read ACL."""
    client.upload_fileobj(
        BytesIO(data),
        S3_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
    )
    return f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"


def upload_photo(session, photo_url, site_id, obit_id):
    """Download a photo from Legacy CDN and upload original + 16:9 to S3.

    Args:
        session: requests.Session.
        photo_url: Original CDN URL (e.g. https://cache.legacy.net/photos/12345.jpg).
        site_id: Market site ID (used in S3 key path).
        obit_id: Obituary row ID (used in S3 key filename).

    Returns:
        dict {"original": s3_url, "16x9": s3_url} or None on failure.
    """
    client = _get_client()
    if not client:
        return None

    try:
        resp = session.get(photo_url, timeout=15)
        if resp.status_code != 200:
            logger.warning("[%s] Photo download failed (%d): %s", site_id, resp.status_code, photo_url)
            return None

        content_type = resp.headers.get("Content-Type", "")
        ext = _guess_extension(photo_url, content_type)
        s3_content_type = _CONTENT_TYPES.get(ext, "image/jpeg")

        # Upload original
        key_orig = f"{S3_PREFIX}/{site_id}/{obit_id}{ext}"
        url_orig = _upload_bytes(client, resp.content, key_orig, s3_content_type)

        # Create and upload 16:9 version
        key_16x9 = f"{S3_PREFIX}/{site_id}/{obit_id}_16x9.jpg"
        data_16x9 = _create_16x9(resp.content)
        url_16x9 = _upload_bytes(client, data_16x9, key_16x9, "image/jpeg")

        return {"original": url_orig, "16x9": url_16x9}

    except ClientError as e:
        logger.warning("[%s] S3 upload failed for obit %s: %s", site_id, obit_id, e)
        return None
    except Exception as e:
        logger.warning("[%s] Photo upload error for obit %s: %s", site_id, obit_id, e)
        return None
