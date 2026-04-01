"""Backfill s3_photo_url for existing obituaries with photos.

Downloads each photo from Legacy CDN and uploads to S3 hamster-storage1 bucket.
Updates the s3_photo_url column in the obituaries table.

Run: python scripts/backfill_s3_photos.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.db_writer import get_connection
from utils.s3_uploader import upload_photo
from utils.rate_limiter import create_session
from utils.logger import get_logger

logger = get_logger("backfill_s3_photos")

UPDATE_SQL = "UPDATE obituaries SET s3_photo_url = %s WHERE id = %s"


def run():
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, site_id, photo_url FROM obituaries "
        "WHERE photo_url IS NOT NULL AND s3_photo_url IS NULL "
        "AND is_deleted = 0 ORDER BY id"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    logger.info("Found %d obits needing S3 photo upload", total)

    session = create_session()
    uploaded = 0
    failed = 0

    for i, row in enumerate(rows, 1):
        s3_url = upload_photo(session, row["photo_url"], row["site_id"], row["id"])

        if s3_url:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(UPDATE_SQL, (s3_url, row["id"]))
            conn.commit()
            cur.close()
            conn.close()
            uploaded += 1
        else:
            failed += 1

        if i % 200 == 0:
            logger.info(
                "Progress: %d/%d (uploaded=%d, failed=%d)",
                i, total, uploaded, failed,
            )

    logger.info(
        "S3 backfill complete: %d uploaded, %d failed, %d total",
        uploaded, failed, total,
    )


if __name__ == "__main__":
    run()
