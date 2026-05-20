"""Post-fix data-quality audit. Telegrams a concise health report.

Quantifies what today's PR set actually delivered:
  1. Polluted-name residue — rows where deceased_name still matches a
     19YY/20YY token (the backfill should have driven this near zero).
  2. Today's obit capture quality — counts + breakdown of rows scraped
     in the last 24h with NULL funeral_home, NULL obit_text, or both.
     Validates that PR #7's listing-metadata harvest is actually
     populating those fields on the new /person/ URLs.
  3. Per-state capture counts (last 24h) — sanity-check that no state
     silently dropped to zero in the wild.

Designed to run as a Render one-off; output flows via Telegram so the
data-warehouse-VPC isolation doesn't block visibility. Exit code is 0
even when issues are found — this is a reporter, not a gate.
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

logger = get_logger("audit_data_quality")


def _scalar(cursor, sql, params=None):
    cursor.execute(sql, params or ())
    row = cursor.fetchone()
    return row[0] if row else 0


def _rows(cursor, sql, params=None):
    cursor.execute(sql, params or ())
    return cursor.fetchall()


def audit():
    conn = get_connection()
    cursor = conn.cursor()

    # ---- 1. Polluted-name residue ----
    polluted_total = _scalar(cursor, """
        SELECT COUNT(*) FROM obituaries
        WHERE deceased_name REGEXP '(19|20)[0-9]{2}'
          AND is_deleted = 0
    """)

    # ---- 2. Today's capture quality (last 24h) ----
    one_day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    cap_24h_total = _scalar(cursor, """
        SELECT COUNT(*) FROM obituaries
        WHERE scraped_at >= %s
          AND is_deleted = 0
    """, (one_day_ago,))
    cap_24h_no_fh = _scalar(cursor, """
        SELECT COUNT(*) FROM obituaries
        WHERE scraped_at >= %s
          AND is_deleted = 0
          AND (funeral_home IS NULL OR funeral_home = '')
    """, (one_day_ago,))
    cap_24h_no_text = _scalar(cursor, """
        SELECT COUNT(*) FROM obituaries
        WHERE scraped_at >= %s
          AND is_deleted = 0
          AND (obit_text IS NULL OR obit_text = '')
    """, (one_day_ago,))

    # ---- 3. Per-state breakdown ----
    state_rows = _rows(cursor, """
        SELECT SUBSTRING_INDEX(site_id, '-', 1) AS state, COUNT(*) AS n
        FROM obituaries
        WHERE scraped_at >= %s
          AND is_deleted = 0
        GROUP BY state
        ORDER BY n DESC
        LIMIT 15
    """, (one_day_ago,))

    cursor.close()
    conn.close()

    # ---- Format Telegram message ----
    def pct(num, denom):
        return f"{(num * 100 // denom) if denom else 0}%"

    lines = ["<b>Funeral Homes Data-Quality Audit</b>", ""]
    lines.append(f"<b>1. Polluted-name residue:</b> {polluted_total:,} rows still match (19|20)YY pattern")
    if polluted_total == 0:
        lines.append("   [OK] Backfill cleaned everything.")
    elif polluted_total < 50:
        lines.append("   [--] Residual — likely names with legit year tokens (e.g. baseball cards 1998).")
    else:
        lines.append("   [WARN] Higher than expected — may be re-pollution from a re-fetch path.")

    lines.append("")
    lines.append(f"<b>2. Last-24h capture quality ({cap_24h_total:,} obits):</b>")
    lines.append(f"   No funeral_home: {cap_24h_no_fh:,} ({pct(cap_24h_no_fh, cap_24h_total)})")
    lines.append(f"   No obit_text:    {cap_24h_no_text:,} ({pct(cap_24h_no_text, cap_24h_total)})")
    no_fh_pct = (cap_24h_no_fh * 100 // cap_24h_total) if cap_24h_total else 0
    if cap_24h_total < 100:
        lines.append("   [--] Sample too small for a meaningful ratio.")
    elif no_fh_pct < 10:
        lines.append("   [OK] Listing-metadata harvest is doing its job.")
    elif no_fh_pct < 30:
        lines.append("   [--] Some markets serve only /person/ URLs without rich snippets.")
    else:
        lines.append("   [WARN] >=30% missing FH — the canary would have already paged.")

    lines.append("")
    lines.append("<b>3. Top 15 states by capture (last 24h):</b>")
    if not state_rows:
        lines.append("   (no rows captured in last 24h)")
    else:
        for state, n in state_rows:
            lines.append(f"   {state}: {n:,}")

    msg = "\n".join(lines)
    logger.info("[OK] Audit report:\n%s", msg)
    telegram_send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(audit())
