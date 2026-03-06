"""AWS Secrets Manager integration for database credentials.

Tries AWS Secrets Manager first, falls back to environment variables / .env
for local development. Caches the result after first fetch.

AWS secret: /ben/ai-tool/db99 in us-east-1
Contains: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
DB_NAME is NOT in AWS — it's project-specific and read from env.
"""

import json
import os

from utils.logger import get_logger

logger = get_logger(__name__)

# AWS Secrets Manager config
SECRET_ID = "/ben/ai-tool/db99"
AWS_REGION = "us-east-1"

# Cached credentials (populated on first call)
_cached_creds = None


def _fetch_from_aws():
    """Fetch DB credentials from AWS Secrets Manager.

    Returns:
        dict with DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, or None on failure.
    """
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        resp = client.get_secret_value(SecretId=SECRET_ID)
        secret = json.loads(resp["SecretString"])
        logger.info("[OK] Loaded DB credentials from AWS Secrets Manager")
        return secret
    except ImportError:
        logger.warning("boto3 not installed — skipping AWS Secrets Manager")
        return None
    except Exception as e:
        logger.warning("AWS Secrets Manager unavailable: %s — falling back to env vars", e)
        return None


def _fetch_from_env():
    """Fetch DB credentials from environment variables.

    Returns:
        dict with DB_HOST, DB_PORT, DB_USER, DB_PASSWORD.

    Raises:
        KeyError if DB_USER or DB_PASSWORD are not set.
    """
    creds = {
        "DB_HOST": os.environ.get("DB_HOST") or "db99.rds.blockshopper.com",
        "DB_PORT": os.environ.get("DB_PORT") or "3306",
        "DB_USER": os.environ["DB_USER"],
        "DB_PASSWORD": os.environ["DB_PASSWORD"],
    }
    logger.info("[OK] Loaded DB credentials from environment variables")
    return creds


def get_db_credentials():
    """Get database connection credentials (cached after first call).

    Priority: AWS Secrets Manager -> environment variables / .env

    Returns:
        dict with keys: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
    """
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds

    creds = _fetch_from_aws()
    if creds is None:
        creds = _fetch_from_env()

    # DB_NAME is always from env — it's project-specific, not a secret
    creds["DB_NAME"] = os.environ.get("DB_NAME") or "funeral_homes"

    _cached_creds = creds
    return _cached_creds
