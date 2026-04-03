"""Send Telegram notifications via Biscotcho bot.

Uses bot token and chat ID from environment variables.
Falls back to hardcoded defaults for the Biscotcho bot.
"""

import os
import requests

from utils.logger import get_logger

logger = get_logger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8205837525:AAGycNPE2SNFkdN-6TnBW5a6NKU7C2PUJy0")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "523535187")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def send_message(text, parse_mode="HTML"):
    """Send a message via Telegram.

    Args:
        text: Message text (supports HTML formatting).
        parse_mode: "HTML" or "Markdown".

    Returns:
        True if sent successfully, False on failure.
    """
    try:
        resp = requests.post(API_URL, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }, timeout=10)
        if resp.status_code == 200:
            return True
        logger.warning("Telegram send failed (%d): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.warning("Telegram send error: %s", e)
        return False
