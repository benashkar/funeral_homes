"""Backfill cleanup: strip trailing year + funeral-home garbage from deceased_name.

Problem: Historical scrapes captured names like
  "Glenn Curran McCabe 2026 - Brantley Phillips Funeral Home"
caused by JSON-LD NewsArticle headlines falling through with insufficient
cleanup. A real person name never contains a 4-digit year, so the first
19YY/20YY token is the boundary between the actual name and trailing junk.

Safety: also cross-checks the cleaned first/last against the SSA + Census
reference tables that already live on db99 in the church_scrapes database
(ref_ssa_names, ref_census_surnames). Rows where neither name matches are
logged as SUSPECT but still cleaned — the year suffix is unambiguous junk.

Usage:
    python scripts/backfill_clean_names.py            # dry-run preview
    python scripts/backfill_clean_names.py --apply    # commit changes
    python scripts/backfill_clean_names.py --limit 100 --apply
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.db_writer import get_connection, ensure_schema
from utils.logger import get_logger

logger = get_logger("backfill_clean_names")

# Same regex as the parser's headline cleanup, but applied to deceased_name.
_NAME_TRAILING_JUNK_RE = re.compile(r"\s*(?:19|20)\d{2}\b.*$")

SELECT_SQL = """
    SELECT id, deceased_name
    FROM obituaries
    WHERE deceased_name REGEXP '(19|20)[0-9]{2}'
      AND is_deleted = 0
"""

UPDATE_SQL = "UPDATE obituaries SET deceased_name = %s WHERE id = %s"

# Cross-DB lookup against church_scrapes ref tables (same db99 instance).
SSA_LOOKUP_SQL = "SELECT 1 FROM church_scrapes.ref_ssa_names WHERE LOWER(name)=LOWER(%s) LIMIT 1"
CENSUS_LOOKUP_SQL = "SELECT 1 FROM church_scrapes.ref_census_surnames WHERE LOWER(name)=LOWER(%s) LIMIT 1"


def clean_name(text):
    if not text:
        return ""
    return _NAME_TRAILING_JUNK_RE.sub("", text).strip()


def split_first_last(name):
    """Cheap first/last split — first token, last token. Good enough for
    the SSA/Census lookups; we don't update first/middle/last columns here."""
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    # Drop trailing suffix like "Jr.", "III" before picking last_name
    suffix_tokens = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
    while len(parts) > 1 and parts[-1].rstrip(".").lower() in suffix_tokens:
        parts.pop()
    return parts[0], parts[-1] if len(parts) > 1 else ""


def name_recognized(cursor, first, last):
    """Return True if SSA recognizes the first OR Census recognizes the last."""
    try:
        if first:
            cursor.execute(SSA_LOOKUP_SQL, (first,))
            if cursor.fetchone():
                return True
        if last:
            cursor.execute(CENSUS_LOOKUP_SQL, (last,))
            if cursor.fetchone():
                return True
    except Exception as exc:
        logger.warning("ref-table lookup failed (%s); skipping validation", exc)
        return True
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="commit changes (default is dry-run)")
    p.add_argument("--limit", type=int, default=0, help="cap rows processed")
    args = p.parse_args()

    conn = get_connection()
    ensure_schema(conn)
    read = conn.cursor()
    write = conn.cursor()

    sql = SELECT_SQL + (f" LIMIT {int(args.limit)}" if args.limit else "")
    read.execute(sql)
    rows = read.fetchall()
    logger.info("[OK] Found %d candidate rows", len(rows))

    cleaned = 0
    suspect = 0
    skipped = 0
    examples = []
    suspect_examples = []

    for row_id, raw in rows:
        new_name = clean_name(raw)
        if not new_name or new_name == raw:
            skipped += 1
            continue
        first, last = split_first_last(new_name)
        if not name_recognized(write, first, last):
            suspect += 1
            if len(suspect_examples) < 10:
                suspect_examples.append((row_id, raw, new_name, first, last))

        if len(examples) < 10:
            examples.append((row_id, raw, new_name))

        if args.apply:
            write.execute(UPDATE_SQL, (new_name, row_id))
        cleaned += 1

    if args.apply:
        conn.commit()
        logger.info("[OK] Committed %d rows (suspect=%d, skipped=%d)", cleaned, suspect, skipped)
    else:
        logger.info(
            "[OK] DRY-RUN: would clean %d rows (suspect=%d, skipped=%d). "
            "Re-run with --apply to commit.", cleaned, suspect, skipped,
        )

    logger.info("[OK] Cleaning examples:")
    for row_id, before, after in examples:
        logger.info("       id=%s  %r  ->  %r", row_id, before, after)
    if suspect_examples:
        logger.info("[WARN] Suspect-name examples (cleaned but not in SSA/Census):")
        for row_id, before, after, first, last in suspect_examples:
            logger.info("       id=%s  %r  ->  %r  (first=%r last=%r)", row_id, before, after, first, last)

    read.close()
    write.close()
    conn.close()


if __name__ == "__main__":
    main()
