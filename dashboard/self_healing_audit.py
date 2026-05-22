"""Self-healing audit trail + kill-switch helpers.

Pulled out of app.py so the new /self-healing-audit route stays under the
~250 LOC budget for the whole feature. All HTTP is via stdlib urllib so we
don't have to touch requirements-web.txt or rely on `git` being present in
the slim production container.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = "benashkar/funeral_homes"
RENDER_OWNER_ID = "tea-d5uccevpm1nc739tsigg"
SCRAPER_PREFIXES = ("funeral-homes-scraper-", "cr-rescue-scraper-")
PAUSE_FILE = ".self_healing_paused"
TRIGGER_ID = "trig_01EzPjGTvjG6cK9BW5NDY3bz"


def _http_get_json(url, headers=None, timeout=10):
    """Stdlib GET → (status_code, json_or_text). Never raises."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def _gh_headers():
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "fh-self-healing-audit"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _relative_age(dt):
    """Human-readable delta vs now (UTC). Accepts ISO string or datetime."""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def fetch_auto_merges(since_days=30, http_get=None):
    """List `[AUTO-MERGE]` commits on master in the last N days via GitHub API.

    Returns a list of dicts with date/subject/sha/short_sha/url/author/files.
    `http_get` is injectable for tests.
    """
    http_get = http_get or _http_get_json
    since_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # GitHub `since` is ISO-8601; subtract N days.
    since_ts = datetime.now(timezone.utc).timestamp() - since_days * 86400
    since_iso = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"https://api.github.com/repos/{REPO}/commits"
           f"?sha=master&since={since_iso}&per_page=100")
    status, data = http_get(url, headers=_gh_headers())
    if status != 200 or not isinstance(data, list):
        return []
    out = []
    for c in data:
        commit = c.get("commit") or {}
        msg = commit.get("message") or ""
        subject = msg.split("\n", 1)[0]
        if "[AUTO-MERGE]" not in subject:
            continue
        sha = c.get("sha") or ""
        author = (commit.get("author") or {}).get("name") or ""
        date_str = (commit.get("author") or {}).get("date") or ""
        files = _fetch_commit_files(sha, http_get) if sha else []
        out.append({
            "sha": sha,
            "short_sha": sha[:7],
            "subject": subject,
            "author": author,
            "date_iso": date_str,
            "date_relative": _relative_age(date_str),
            "url": f"https://github.com/{REPO}/commit/{sha}",
            "files": files,
        })
    return out


def _fetch_commit_files(sha, http_get):
    url = f"https://api.github.com/repos/{REPO}/commits/{sha}"
    status, data = http_get(url, headers=_gh_headers())
    if status != 200 or not isinstance(data, dict):
        return []
    return [f.get("filename") for f in (data.get("files") or []) if f.get("filename")]


def parse_git_log_output(text):
    """Parse `git log --pretty=format:'%H|%ad|%s|%an'` output.

    Kept around for tests + future use if we add `git` to the container.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, date_iso, subject, author = parts
        if "[AUTO-MERGE]" not in subject:
            continue
        rows.append({
            "sha": sha,
            "short_sha": sha[:7],
            "date_iso": date_iso,
            "subject": subject,
            "author": author,
            "url": f"https://github.com/{REPO}/commit/{sha}",
            "date_relative": _relative_age(date_iso),
            "files": [],
        })
    return rows


def fetch_suspended_scrapers(http_get=None):
    """List Render scraper services where suspended != 'not_suspended'.

    Requires RENDER_API_KEY env var. Returns [] if missing or call fails so
    the dashboard stays up.
    """
    http_get = http_get or _http_get_json
    api_key = os.environ.get("RENDER_API_KEY")
    if not api_key:
        return []
    url = (f"https://api.render.com/v1/services?ownerId={RENDER_OWNER_ID}"
           "&limit=100")
    headers = {"Authorization": f"Bearer {api_key}",
               "Accept": "application/json"}
    status, data = http_get(url, headers=headers)
    if status != 200 or not isinstance(data, list):
        return []
    out = []
    for entry in data:
        svc = entry.get("service") if isinstance(entry, dict) and "service" in entry else entry
        if not isinstance(svc, dict):
            continue
        name = svc.get("name") or ""
        if not name.startswith(SCRAPER_PREFIXES):
            continue
        suspended = svc.get("suspended") or "not_suspended"
        if suspended == "not_suspended":
            continue
        svc_id = svc.get("id") or ""
        out.append({
            "name": name,
            "service_id": svc_id,
            "suspended": suspended,
            "suspended_at": svc.get("suspendedAt") or svc.get("updatedAt") or "",
            "suspended_relative": _relative_age(
                svc.get("suspendedAt") or svc.get("updatedAt") or ""
            ),
            "resume_url": f"https://dashboard.render.com/web/{svc_id}",
        })
    return out


def fetch_pause_status(http_get=None):
    """200 → paused, 404 → active, anything else → unknown (treat as active)."""
    http_get = http_get or _http_get_json
    url = (f"https://api.github.com/repos/{REPO}/contents/{PAUSE_FILE}"
           "?ref=master")
    status, _ = http_get(url, headers=_gh_headers())
    if status == 200:
        return {"paused": True, "known": True}
    if status == 404:
        return {"paused": False, "known": True}
    return {"paused": False, "known": False}
