"""MySQL upsert logic for obituaries and scrape logging.

Optimized for scale: connection pooling, batch inserts, per-market URL lookups.
"""

from datetime import date

import mysql.connector
from mysql.connector import pooling

from utils.aws_secrets import get_db_credentials
from utils.logger import get_logger

logger = get_logger(__name__)

# --- Auto-migration: ensures funeral_homes table + FK exist (idempotent) ---

_MIGRATION_RAN = False


def ensure_schema(conn):
    """Create funeral_homes table and obituaries FK if they don't exist.

    Safe to call on every startup — all statements are idempotent.
    """
    global _MIGRATION_RAN
    if _MIGRATION_RAN:
        return
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS funeral_homes (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            legacy_fh_id    VARCHAR(20)   NOT NULL UNIQUE,
            name            VARCHAR(255)  NOT NULL,
            address         VARCHAR(500)  DEFAULT NULL,
            city            VARCHAR(100)  DEFAULT NULL,
            state           VARCHAR(50)   DEFAULT NULL,
            zip             VARCHAR(10)   DEFAULT NULL,
            lat             DECIMAL(10,7) DEFAULT NULL,
            lon             DECIMAL(10,7) DEFAULT NULL,
            legacy_url      VARCHAR(500)  DEFAULT NULL,
            created_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_city_state (city, state),
            INDEX idx_name (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    # Add funeral_home_id column if missing
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'obituaries'
          AND COLUMN_NAME = 'funeral_home_id'
    """)
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE obituaries ADD COLUMN funeral_home_id INT DEFAULT NULL AFTER funeral_home")
        cursor.execute("ALTER TABLE obituaries ADD INDEX idx_funeral_home_id (funeral_home_id)")
        cursor.execute("""
            ALTER TABLE obituaries ADD CONSTRAINT fk_obit_funeral_home
            FOREIGN KEY (funeral_home_id) REFERENCES funeral_homes(id) ON DELETE SET NULL
        """)
        logger.info("[OK] Migration: added funeral_home_id column + FK")
    # Add s3_photo_url column if missing
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'obituaries'
          AND COLUMN_NAME = 's3_photo_url'
    """)
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE obituaries ADD COLUMN s3_photo_url VARCHAR(500) DEFAULT NULL AFTER photo_url")
        logger.info("[OK] Migration: added s3_photo_url column")
    # Add s3_photo_url_16x9 column if missing
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'obituaries'
          AND COLUMN_NAME = 's3_photo_url_16x9'
    """)
    if cursor.fetchone()[0] == 0:
        cursor.execute("ALTER TABLE obituaries ADD COLUMN s3_photo_url_16x9 VARCHAR(500) DEFAULT NULL AFTER s3_photo_url")
        logger.info("[OK] Migration: added s3_photo_url_16x9 column")
    conn.commit()
    cursor.close()
    _MIGRATION_RAN = True
    logger.info("[OK] Schema check complete")


# SQL statements
INSERT_OBIT_SQL = """
    INSERT IGNORE INTO obituaries
        (site_id, legacy_url, deceased_name, published_date, death_date,
         death_city, death_state, funeral_home, funeral_home_id,
         photo_url, s3_photo_url, s3_photo_url_16x9, obit_text)
    VALUES
        (%(site_id)s, %(legacy_url)s, %(deceased_name)s, %(published_date)s,
         %(death_date)s, %(death_city)s, %(death_state)s, %(funeral_home)s,
         %(funeral_home_id)s, %(photo_url)s, %(s3_photo_url)s,
         %(s3_photo_url_16x9)s, %(obit_text)s)
"""

UPSERT_FH_SQL = """
    INSERT INTO funeral_homes (legacy_fh_id, name, city, state, legacy_url)
    VALUES (%(legacy_fh_id)s, %(name)s, %(city)s, %(state)s, %(legacy_url)s)
    ON DUPLICATE KEY UPDATE
        name = VALUES(name),
        city = COALESCE(VALUES(city), city),
        state = COALESCE(VALUES(state), state),
        legacy_url = COALESCE(VALUES(legacy_url), legacy_url)
"""

LOOKUP_FH_ID_SQL = """
    SELECT id FROM funeral_homes WHERE legacy_fh_id = %s LIMIT 1
"""

INSERT_LOG_SQL = """
    INSERT INTO scrape_log (site_id, run_date, obits_found, obits_new, errors)
    VALUES (%(site_id)s, %(run_date)s, %(obits_found)s, %(obits_new)s, %(errors)s)
"""

# Per-market URL lookup — only loads URLs for one site_id at a time
KNOWN_URLS_SQL = """
    SELECT legacy_url FROM obituaries WHERE site_id = %s
"""

# Module-level connection pool (created on first use)
_pool = None


def _get_pool():
    """Get or create the connection pool (lazy init, thread-safe)."""
    global _pool
    if _pool is not None:
        return _pool

    creds = get_db_credentials()
    _pool = pooling.MySQLConnectionPool(
        pool_name="obit_pool",
        pool_size=8,
        pool_reset_session=True,
        host=creds["DB_HOST"],
        port=int(creds["DB_PORT"]),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        database=creds["DB_NAME"],
        connect_timeout=10,
        autocommit=False,
    )
    logger.info("[OK] Created connection pool (size=8)")
    return _pool


def get_connection():
    """Get a connection from the pool."""
    return _get_pool().get_connection()


def _get_connection():
    """Legacy alias for get_connection (backwards compat)."""
    return get_connection()


def get_known_urls_for_site(conn, site_id):
    """Fetch existing legacy_urls for a single market.

    Only loads URLs for the given site_id, not the entire table.
    At scale (1M+ obits), this avoids loading hundreds of MB into memory.

    Args:
        conn: MySQL connection.
        site_id: Market site ID to look up.

    Returns:
        set of URL strings for this market.
    """
    cursor = conn.cursor()
    cursor.execute(KNOWN_URLS_SQL, (site_id,))
    urls = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return urls


def url_exists(conn, url):
    """Check if an obituary URL is already in the database.

    Args:
        conn: MySQL connection.
        url: Legacy.com obituary URL.

    Returns:
        True if the URL already exists.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM obituaries WHERE legacy_url = %s LIMIT 1", (url,))
    result = cursor.fetchone()
    cursor.close()
    return result is not None


def batch_insert_obits(conn, obits, site_id):
    """Insert a batch of obituaries in one transaction.

    Uses INSERT IGNORE so duplicates are silently skipped.

    Args:
        conn: MySQL connection.
        obits: List of obit dicts.
        site_id: Market site ID string.

    Returns:
        Number of new rows inserted.
    """
    if not obits:
        return 0

    cursor = conn.cursor()
    new_count = 0
    for obit in obits:
        params = {
            "site_id": site_id,
            "legacy_url": obit["legacy_url"],
            "deceased_name": obit.get("deceased_name"),
            "published_date": obit.get("published_date"),
            "death_date": obit.get("death_date"),
            "death_city": obit.get("death_city"),
            "death_state": obit.get("death_state"),
            "funeral_home": obit.get("funeral_home"),
            "funeral_home_id": obit.get("funeral_home_id"),
            "photo_url": obit.get("photo_url"),
            "s3_photo_url": obit.get("s3_photo_url"),
            "s3_photo_url_16x9": obit.get("s3_photo_url_16x9"),
            "obit_text": obit.get("obit_text"),
        }
        cursor.execute(INSERT_OBIT_SQL, params)
        if cursor.rowcount > 0:
            new_count += 1

    conn.commit()
    cursor.close()
    return new_count


def upsert_obit(conn, obit_dict, site_id):
    """Insert an obituary, ignoring duplicates on legacy_url.

    For single-obit inserts (backwards compat). Prefer batch_insert_obits.

    Args:
        conn: MySQL connection.
        obit_dict: Dict with keys matching the INSERT columns.
        site_id: Market site ID string.

    Returns:
        True if a new row was inserted, False if duplicate.
    """
    return batch_insert_obits(conn, [obit_dict], site_id) > 0


def flag_bad_and_dupes(conn):
    """Post-insert cleanup: flag invalid records and cross-market duplicates.

    Bad records: null/empty name, or name with no letters.
    Duplicates: same deceased_name + death_date + funeral_home across markets.
    Keeps the lowest id (first scraped) and flags the rest.
    """
    cursor = conn.cursor()

    # Flag bad names
    cursor.execute("""
        UPDATE obituaries SET is_deleted = 1
        WHERE is_deleted = 0
          AND (deceased_name IS NULL
               OR TRIM(deceased_name) = ''
               OR deceased_name REGEXP '^[^a-zA-Z]*$')
    """)
    bad = cursor.rowcount

    # Flag cross-market duplicates (same name + date + funeral home)
    # Keep the lowest id, flag the rest
    cursor.execute("""
        UPDATE obituaries o
        INNER JOIN (
            SELECT deceased_name, death_date, funeral_home, MIN(id) as keep_id
            FROM obituaries
            WHERE is_deleted = 0
              AND deceased_name IS NOT NULL
              AND death_date IS NOT NULL
              AND funeral_home IS NOT NULL
            GROUP BY deceased_name, death_date, funeral_home
            HAVING COUNT(*) > 1
        ) dupes ON o.deceased_name = dupes.deceased_name
               AND o.death_date = dupes.death_date
               AND o.funeral_home = dupes.funeral_home
               AND o.id != dupes.keep_id
        SET o.is_deleted = 1
        WHERE o.is_deleted = 0
    """)
    duped = cursor.rowcount

    conn.commit()
    cursor.close()

    if bad > 0 or duped > 0:
        logger.info("[OK] Flagged %d bad names, %d duplicates as is_deleted", bad, duped)

    return bad, duped


def upsert_funeral_home(conn, fh_data):
    """Insert or update a funeral home record, return the row ID.

    Uses INSERT ON DUPLICATE KEY UPDATE keyed on legacy_fh_id.

    Args:
        conn: MySQL connection.
        fh_data: Dict with keys: legacy_fh_id, name, city, state, legacy_url.

    Returns:
        int row ID, or None if fh_data is missing required fields.
    """
    if not fh_data or not fh_data.get("legacy_fh_id"):
        return None
    cursor = conn.cursor()
    cursor.execute(UPSERT_FH_SQL, fh_data)
    cursor.execute(LOOKUP_FH_ID_SQL, (fh_data["legacy_fh_id"],))
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row else None


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
