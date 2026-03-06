"""MySQL upsert logic for obituaries and scrape logging."""

from datetime import date

import mysql.connector

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

# SQL statements
INSERT_OBIT_SQL = """
    INSERT IGNORE INTO obituaries
        (site_id, legacy_url, deceased_name, published_date, death_date, funeral_home, obit_text)
    VALUES
        (%(site_id)s, %(legacy_url)s, %(deceased_name)s, %(published_date)s,
         %(death_date)s, %(funeral_home)s, %(obit_text)s)
"""

INSERT_LOG_SQL = """
    INSERT INTO scrape_log (site_id, run_date, obits_found, obits_new, errors)
    VALUES (%(site_id)s, %(run_date)s, %(obits_found)s, %(obits_new)s, %(errors)s)
"""

CHECK_URL_SQL = """
    SELECT 1 FROM obituaries WHERE legacy_url = %s LIMIT 1
"""


def _get_connection():
    """Create a MySQL connection using AWS Secrets Manager (or env fallback)."""
    creds = get_db_credentials()
    return mysql.connector.connect(
        host=creds["DB_HOST"],
        port=int(creds["DB_PORT"]),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        database=creds["DB_NAME"],
    )


def url_exists(conn, url):
    """Check if an obituary URL is already in the database.

    Args:
        conn: MySQL connection.
        url: Legacy.com obituary URL.

    Returns:
        True if the URL already exists.
    """
    cursor = conn.cursor()
    cursor.execute(CHECK_URL_SQL, (url,))
    result = cursor.fetchone()
    cursor.close()
    return result is not None


def upsert_obit(conn, obit_dict, site_id):
    """Insert an obituary, ignoring duplicates on legacy_url.

    Args:
        conn: MySQL connection.
        obit_dict: Dict with keys matching the INSERT columns.
        site_id: Market site ID string.

    Returns:
        True if a new row was inserted, False if duplicate.
    """
    params = {
        "site_id": site_id,
        "legacy_url": obit_dict["legacy_url"],
        "deceased_name": obit_dict.get("deceased_name"),
        "published_date": obit_dict.get("published_date"),
        "death_date": obit_dict.get("death_date"),
        "funeral_home": obit_dict.get("funeral_home"),
        "obit_text": obit_dict.get("obit_text"),
    }
    cursor = conn.cursor()
    cursor.execute(INSERT_OBIT_SQL, params)
    conn.commit()
    inserted = cursor.rowcount > 0
    cursor.close()
    return inserted


def log_run(conn, site_id, found, new, errors=None):
    """Write a scrape run summary to the scrape_log table.

    Args:
        conn: MySQL connection.
        site_id: Market site ID.
        found: Total obituaries found on the listing page.
        new: Number of new obituaries inserted.
        errors: Error text or None.
    """
    params = {
        "site_id": site_id,
        "run_date": date.today(),
        "obits_found": found,
        "obits_new": new,
        "errors": errors,
    }
    cursor = conn.cursor()
    cursor.execute(INSERT_LOG_SQL, params)
    conn.commit()
    cursor.close()
