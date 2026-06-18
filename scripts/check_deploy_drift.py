"""Deploy-drift check for the funeral_homes Render fleet.

Every funeral-homes scraper + cr-rescue service has autoDeploy=OFF, so a merge
to master does NOT update them — they must be redeployed explicitly. When that
step is missed, a service silently runs stale code (e.g. the cr-rescue trio sat
on a pre-curl_cffi commit and reported BLOCKED 100% for days while the main
fleet was healthy). This script compares each service's live deploy commit
against origin/master and escalates via Telegram (Biscotcho) when any drift.

Run standalone or as a step in the daily self-healing agent:
    python -u scripts/check_deploy_drift.py            # alert only on drift
    python -u scripts/check_deploy_drift.py --always    # always send a summary
Exit code 1 if any service is drifted or not live, else 0 (CI/gating friendly).

Needs RENDER_API_KEY in the env. Telegram creds come from utils.telegram.
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.telegram import send_message
from utils.logger import get_logger

logger = get_logger("deploy_drift")

REPO = "benashkar/funeral_homes"
RENDER_API = "https://api.render.com/v1"

# name -> Render service id. Keep in sync with the self-healing service map.
SERVICES = {
    "scraper-1": "crn-d6li6f5m5p6s73chtqh0",
    "scraper-2": "crn-d74anl0ule4c73ev39n0",
    "scraper-3": "crn-d74anlhr0fns73csidgg",
    "scraper-4": "crn-d74anm5m5p6s73f49ue0",
    "scraper-5": "crn-d77hgu7kijhs73fkqn3g",
    "scraper-6": "crn-d7a6qkbuibrs73fr5pj0",
    "scraper-7": "crn-d7a6ql5m5p6s73f253tg",
    "scraper-8": "crn-d7a6qlggjchc739v4blg",
    "scraper-9": "crn-d7a6qmbuibrs73dh6ukg",
    "scraper-10": "crn-d7a6qmshg0os73bd3ai0",
    "scraper-11": "crn-d86kkoojs32c73ephgt0",
    "scraper-12": "crn-d8707mjtqb8s7381kfhg",
    "cr-rescue-1": "crn-d7dbjenavr4c73e0212g",
    "cr-rescue-2": "crn-d7dbjq67r5hc739v008g",
    "cr-rescue-3": "crn-d7dsenpf9bms738bpab0",
    "cherry-road-health": "crn-d7bv4628qa3s738mvo5g",
    "dashboard": "srv-d6lj29sr85hc73a8f1m0",
}


def _get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def master_head():
    """Latest origin/master commit SHA via the GitHub API (repo is public)."""
    data = _get(
        f"https://api.github.com/repos/{REPO}/commits/master",
        {"Accept": "application/vnd.github+json", "User-Agent": "deploy-drift"},
    )
    return data["sha"]


def latest_deploy(sid, key):
    deploys = _get(
        f"{RENDER_API}/services/{sid}/deploys?limit=1",
        {"Authorization": "Bearer " + key},
    )
    if not deploys:
        return None, None
    d = deploys[0].get("deploy", deploys[0])
    return (d.get("commit", {}) or {}).get("id"), d.get("status")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--always", action="store_true",
                    help="send a Telegram summary even when nothing is drifted")
    args = ap.parse_args()

    key = os.environ.get("RENDER_API_KEY")
    if not key:
        logger.error("[ERR] RENDER_API_KEY not set")
        sys.exit(2)

    head = master_head()
    logger.info("[OK] origin/master HEAD = %s", head[:9])

    drifted, not_live, ok = [], [], 0
    for name, sid in SERVICES.items():
        try:
            sha, status = latest_deploy(sid, key)
        except Exception as e:
            not_live.append((name, f"API error: {e}"))
            continue
        short = (sha or "?")[:9]
        if status != "live":
            not_live.append((name, f"deploy status={status} ({short})"))
        elif sha != head:
            drifted.append((name, short))
        else:
            ok += 1
        logger.info("  %-20s %-9s %s", name, short, status)

    n_issues = len(drifted) + len(not_live)
    logger.info("[OK] %d on HEAD, %d drifted, %d not-live", ok, len(drifted), len(not_live))

    if n_issues or args.always:
        lines = ["<b>Deploy-Drift Check — Funeral Homes</b>"]
        lines.append(f"<b>Status: {'DRIFT' if n_issues else 'OK'}</b>")
        lines.append(f"master HEAD: <code>{head[:9]}</code>")
        lines.append(f"On HEAD: {ok}/{len(SERVICES)}")
        if drifted:
            lines.append("\n<b>Drifted (stale code — redeploy):</b>")
            lines += [f"  • {n} @ <code>{s}</code>" for n, s in drifted]
        if not_live:
            lines.append("\n<b>Not live:</b>")
            lines += [f"  • {n}: {s}" for n, s in not_live]
        if n_issues:
            lines.append("\n<b>Fix:</b> POST /v1/services/{id}/deploys for each "
                         "(autoDeploy is OFF on these).")
        send_message("\n".join(lines))

    sys.exit(1 if n_issues else 0)


if __name__ == "__main__":
    main()
