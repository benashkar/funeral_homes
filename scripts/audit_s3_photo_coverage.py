"""Telegram-pipe report on S3 photo coverage.

Quantifies how well the inline S3 upload path (utils/s3_uploader.upload_photo
called from scheduler/run_daily.py) is keeping up:

  1. Overall: how many obits have photo_url vs s3_photo_url (the gap is
     the inline-upload backlog).
  2. Last-24h capture quality: of the obits scraped in the last 24h that
     HAVE a photo_url, what fraction successfully also got s3_photo_url
     populated? That's the live success rate of the inline uploader.
  3. Last-7-day trend: per-day capture vs upload counts so you can spot
     a sudden drop in success rate.

Designed to run as a Render one-off; output flows via Telegram since
Render `/v1/logs` doesn't surface cron runtime logs.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.db_writer import get_connection
from utils.logger import get_logger
from utils.telegram import send_message as telegram_send

logger = get_logger("audit_s3_photo_coverage")


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return row[0] if row else 0


def _rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def audit():
    conn = get_connection()
    cur = conn.cursor()

    # ---- 1. Overall coverage ----
    total = _scalar(cur, "SELECT COUNT(*) FROM obituaries WHERE is_deleted = 0")
    with_photo_url = _scalar(cur, """
        SELECT COUNT(*) FROM obituaries
        WHERE is_deleted = 0 AND photo_url IS NOT NULL AND photo_url != ''
    """)
    with_s3 = _scalar(cur, """
        SELECT COUNT(*) FROM obituaries
        WHERE is_deleted = 0 AND s3_photo_url IS NOT NULL AND s3_photo_url != ''
    """)
    with_s3_16x9 = _scalar(cur, """
        SELECT COUNT(*) FROM obituaries
        WHERE is_deleted = 0 AND s3_photo_url_16x9 IS NOT NULL AND s3_photo_url_16x9 != ''
    """)
    gap = with_photo_url - with_s3

    # ---- 2. Last 24h success rate (inline uploader) ----
    one_day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    new_with_photo = _scalar(cur, """
        SELECT COUNT(*) FROM obituaries
        WHERE is_deleted = 0
          AND scraped_at >= %s
          AND photo_url IS NOT NULL AND photo_url != ''
    """, (one_day_ago,))
    new_with_s3 = _scalar(cur, """
        SELECT COUNT(*) FROM obituaries
        WHERE is_deleted = 0
          AND scraped_at >= %s
          AND photo_url IS NOT NULL AND photo_url != ''
          AND s3_photo_url IS NOT NULL AND s3_photo_url != ''
    """, (one_day_ago,))

    # ---- 3. 7-day trend ----
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    trend = _rows(cur, """
        SELECT DATE(scraped_at) AS d,
               COUNT(*) AS scraped,
               SUM(CASE WHEN photo_url IS NOT NULL AND photo_url != '' THEN 1 ELSE 0 END) AS had_photo,
               SUM(CASE WHEN s3_photo_url IS NOT NULL AND s3_photo_url != '' THEN 1 ELSE 0 END) AS had_s3
        FROM obituaries
        WHERE scraped_at >= %s AND is_deleted = 0
        GROUP BY DATE(scraped_at)
        ORDER BY d DESC
    """, (seven_days_ago,))

    cur.close()
    conn.close()

    # ---- Format Telegram message ----
    def pct(num, denom):
        return f"{(num * 100 // denom)}%" if denom else "n/a"

    lines = ["<b>S3 Photo Coverage Audit</b>", ""]
    lines.append("<b>1. Overall:</b>")
    lines.append(f"  Total obits:       {total:,}")
    lines.append(f"  With photo_url:    {with_photo_url:,} ({pct(with_photo_url, total)})")
    lines.append(f"  With s3_photo_url: {with_s3:,} ({pct(with_s3, with_photo_url)} of those with photos)")
    lines.append(f"  With 16:9 variant: {with_s3_16x9:,}")
    lines.append(f"  Backlog (photo_url but no S3): {gap:,}")
    if gap > 0:
        lines.append(f"  [--] Run `python scripts/backfill_s3_photos.py` to drain.")
    else:
        lines.append(f"  [OK] No backlog.")

    lines.append("")
    lines.append(f"<b>2. Inline uploader success (last 24h):</b>")
    lines.append(f"  Captured with photo:    {new_with_photo:,}")
    lines.append(f"  Of those, S3-uploaded:  {new_with_s3:,} ({pct(new_with_s3, new_with_photo)})")
    if new_with_photo < 50:
        lines.append("  [--] Sample too small for a meaningful ratio.")
    elif new_with_photo and (new_with_s3 * 100 // new_with_photo) >= 80:
        lines.append("  [OK] Inline uploader is keeping up.")
    elif new_with_photo and (new_with_s3 * 100 // new_with_photo) >= 50:
        lines.append("  [--] Inline uploader missing some — worth investigating S3 errors.")
    else:
        lines.append("  [WARN] Inline uploader is failing on most captures. Check S3 creds / quota.")

    lines.append("")
    lines.append("<b>3. Last-7-day trend (date | scraped | with_photo | s3_uploaded):</b>")
    for d, scraped, had_photo, had_s3 in trend:
        rate = pct(had_s3, had_photo)
        lines.append(f"  {d} | {scraped:>5} | {had_photo:>4} | {had_s3:>4} ({rate})")

    msg = "\n".join(lines)
    logger.info("[OK] Audit:\n%s", msg)
    telegram_send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(audit())
