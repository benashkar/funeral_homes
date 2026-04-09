"""Daily Cherry Road health check — send Telegram alert with stale cities.

Loads config/cherry_road_markets.json and checks each city's latest scrape
data against the obituaries table. Reports cities that haven't had any
new obit in 48+ hours.

Run via Render cron after the daily scrape completes.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.db_writer import get_connection
from utils.logger import get_logger
from utils.telegram import send_message

logger = get_logger("cherry_road_health")

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "cherry_road_markets.json",
)


def load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def check_city(cur, city, state):
    """Return (last_scraped_at, total_obits, obits_last_7d) for a CR city."""
    cur.execute(
        "SELECT MAX(scraped_at) as last_scraped, COUNT(*) as total, "
        "SUM(CASE WHEN scraped_at >= NOW() - INTERVAL 7 DAY THEN 1 ELSE 0 END) as last_7d "
        "FROM obituaries "
        "WHERE LOWER(death_city) = LOWER(%s) AND LOWER(death_state) = LOWER(%s) "
        "AND is_deleted = 0",
        (city, state),
    )
    row = cur.fetchone()
    return row["last_scraped"], int(row["total"] or 0), int(row["last_7d"] or 0)


def main():
    cities = load_manifest()
    logger.info("Checking %d Cherry Road cities", len(cities))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    green = []
    yellow = []
    red = []

    now = datetime.now()
    for entry in cities:
        last_scraped, total, last_7d = check_city(cur, entry["city"], entry["state"])

        if last_scraped is None:
            days_stale = None
            status = "red"
        else:
            days_stale = (now - last_scraped).days
            if days_stale <= 2:
                status = "green"
            elif days_stale <= 5:
                status = "yellow"
            else:
                status = "red"

        record = {
            **entry,
            "total": total,
            "last_7d": last_7d,
            "days_stale": days_stale,
        }

        if status == "green":
            green.append(record)
        elif status == "yellow":
            yellow.append(record)
        else:
            red.append(record)

    cur.close()
    conn.close()

    # Build Telegram message
    total = len(cities)
    msg_lines = [
        f"<b>Cherry Road Health Report</b>",
        f"Cities: {total} | Green: {len(green)} | Yellow: {len(yellow)} | Red: {len(red)}",
        "",
    ]

    if red:
        msg_lines.append(f"<b>RED ({len(red)}):</b> never or 5+ days stale")
        for r in red[:25]:
            stale = "never" if r["days_stale"] is None else f"{r['days_stale']}d"
            msg_lines.append(f"  • {r['city']}, {r['state']} ({r['site_id']}) — {stale}")
        if len(red) > 25:
            msg_lines.append(f"  ... +{len(red) - 25} more")
        msg_lines.append("")

    if yellow:
        msg_lines.append(f"<b>YELLOW ({len(yellow)}):</b> 3-5 days stale")
        for r in yellow[:15]:
            msg_lines.append(f"  • {r['city']}, {r['state']} ({r['site_id']}) — {r['days_stale']}d")
        if len(yellow) > 15:
            msg_lines.append(f"  ... +{len(yellow) - 15} more")
        msg_lines.append("")

    if not red and not yellow:
        msg_lines.append("All Cherry Road cities healthy.")

    msg = "\n".join(msg_lines)
    logger.info("Sending Telegram alert: red=%d, yellow=%d, green=%d", len(red), len(yellow), len(green))
    send_message(msg)


if __name__ == "__main__":
    main()
