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
        from botocore.config import Config
        boto_config = Config(
            connect_timeout=5, read_timeout=5,
            retries={"max_attempts": 1},
        )
        client = boto3.client("secretsmanager", region_name=AWS_REGION, config=boto_config)
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
    """
    creds = {
        "DB_HOST": os.environ.get("DB_HOST") or "db99.rds.blockshopper.com",
        "DB_PORT": os.environ.get("DB_PORT") or "3306",
        "DB_USER": os.environ.get("DB_USER") or "root",
        "DB_PASSWORD": os.environ.get("DB_PASSWORD") or "",
    }
    logger.info("[OK] Loaded DB credentials from environment variables")
    return creds


def get_db_credentials():
    """Get database connection credentials (cached after first call).

    Priority: AWS Secrets Manager -> environment variables / .env
    Local override: if DB_HOST is set in env, it overrides the AWS value.
    This is needed because the VPC endpoint hostname from AWS is only
    reachable from Render (via PrivateLink), not from local machines.

    Returns:
        dict with keys: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
    """
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds

    creds = _fetch_from_aws()
    if creds is None:
        creds = _fetch_from_env()

    # Local dev override: env DB_HOST takes precedence over AWS value
    # (VPC endpoint is unreachable locally; use direct IP via .env)
    env_host = os.environ.get("DB_HOST")
    if env_host:
        creds["DB_HOST"] = env_host
        logger.info("[OK] Using DB_HOST from env: %s", env_host)

    # DB_NAME is always from env — it's project-specific, not a secret
    creds["DB_NAME"] = os.environ.get("DB_NAME") or "funeral_homes"

    _cached_creds = creds
    return _cached_creds
