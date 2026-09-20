"""Every receipt upload, one row each — read off Caddy's access log.

The API stores nothing, so the only record of use is the JSON access log the
edge proxy writes, which compose mounts read-only into this container. A row
here is one submitted receipt photo, successful or not; loading the page and
leaving is not using the app, so that is a headline count instead.

What a row can say is bounded by what the log knows. The receipt itself —
merchant, items, totals, who owes what — never reaches the server, and pay
link payloads are blanked by the log filter before they are written, so the
detail per upload is when, from what kind of device, how it ended, how long
it took and how big the photo was. Addresses are read only to tell visitors
apart and are never returned, which is what lets /stats be an open URL:
there is nothing on it worth guarding with a login.
"""

import glob
import gzip
import json
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

# The people at the table are in Singapore; the box is in UTC. A fixed
# offset avoids depending on tzdata being present in the slim image.
SGT = timezone(timedelta(hours=8))

BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "curl",
    "wget",
    "python-requests",
    "go-http-client",
    "censys",
    "zgrab",
    "masscan",
    "nmap",
)

# What the status code meant to the person holding the phone. Anything not
# listed is shown as the bare code rather than guessed at.
OUTCOMES = {
    # Caddy writes status 0 when nothing was ever sent back — the phone went
    # to sleep or the person gave up while the model was still reading.
    0: "connection dropped",
    200: "parsed",
    400: "bad upload",
    413: "photo too large",
    429: "rate limited",
    502: "parse failed",
    503: "still parsing",
}


def device_label(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua or any(m in ua for m in BOT_MARKERS):
        return "bot"
    if "iphone" in ua or "ipad" in ua:
        return "iPhone/iPad"
    if "android" in ua:
        return "Android"
    if "macintosh" in ua:
        return "Mac"
    if "windows" in ua:
        return "Windows"
    return "other"


def _read_lines(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", errors="replace") as f:
        yield from f


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, SGT).isoformat(timespec="seconds")


def _upload_kb(entry: dict, req: dict):
    """How big the photo was. Caddy records the request body it read; a line
    without that field may still carry the length the phone declared."""
    size = entry.get("bytes_read")
    if size is None:
        header = (req.get("headers", {}).get("Content-Length") or [None])[0]
        try:
            size = int(header)
        except (TypeError, ValueError):
            return None
    return round(size / 1024) or None


def summarise(paths) -> dict:
    """Fold every log line in `paths` into the report. Pure: no caching, no IO
    beyond reading the files given, so tests can hand it a temp directory."""
    # Visitors exist only to number the rows, so a returning phone is visible
    # without an address ever leaving. Insertion order is first-appearance
    # order, which keeps a number attached to the same person as the log grows.
    visitors: "OrderedDict[tuple, int]" = OrderedDict()
    uploads = []
    totals = {
        "requests": 0,
        "visitors": 0,
        "uploads": 0,
        "receiptsParsed": 0,
        "failedParses": 0,
        "pageViews": 0,
        "payLinkOpens": 0,
        "botHits": 0,
    }

    for path in paths:
        for line in _read_lines(path):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            ts = entry.get("ts")
            req = entry.get("request") or {}
            if ts is None or not req:
                continue
            totals["requests"] += 1

            agent = (req.get("headers", {}).get("User-Agent") or [""])[0]
            device = device_label(agent)
            if device == "bot":
                totals["botHits"] += 1
                continue

            ip = req.get("client_ip") or req.get("remote_ip") or "?"
            key = (ip, device)
            if key not in visitors:
                visitors[key] = len(visitors) + 1

            path_only = (req.get("uri") or "").split("?", 1)[0]
            method = req.get("method", "")
            status = entry.get("status", 0)

            if method == "POST" and "parse-receipt" in path_only:
                ok = status == 200
                totals["uploads"] += 1
                totals["receiptsParsed" if ok else "failedParses"] += 1
                uploads.append(
                    {
                        "at": _iso(ts),
                        "visitor": visitors[key],
                        "device": device,
                        "ok": ok,
                        "outcome": OUTCOMES.get(status, "error %d" % status),
                        "status": status,
                        "seconds": round(float(entry.get("duration") or 0), 1),
                        "photoKB": _upload_kb(entry, req),
                    }
                )
            elif method == "GET" and path_only.startswith("/pay/"):
                # Someone opening the link they were sent.
                totals["payLinkOpens"] += 1
            elif method == "GET" and not path_only.startswith("/api/"):
                # An extensionless GET is the SPA loading, not an asset.
                if "." not in path_only.rsplit("/", 1)[-1]:
                    totals["pageViews"] += 1

    totals["visitors"] = len(visitors)
    uploads.sort(key=lambda u: u["at"], reverse=True)

    return {
        "generatedAt": _iso(time.time()),
        "logFiles": len(paths),
        "totals": totals,
        "uploads": uploads,
    }


# One report per worker, refreshed at most every STATS_CACHE_S. The log is a
# few MB at most; the cache exists so an open URL cannot make a 1 vCPU box
# re-read it for every stranger's scanner, not because parsing is slow.
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "pattern": None, "report": None}


def access_log_report(pattern: str, max_age_s: int) -> dict:
    now = time.monotonic()
    with _lock:
        fresh = (
            _cache["report"] is not None
            and _cache["pattern"] == pattern
            and now - _cache["at"] < max_age_s
        )
        if fresh:
            return _cache["report"]
        paths = sorted(p for p in glob.glob(os.path.expanduser(pattern)) if os.path.isfile(p))
        report = summarise(paths)
        _cache.update(at=now, pattern=pattern, report=report)
        return report


def _photo(kb) -> str:
    """Phone photos are a couple of MB; KB past a thousand is hard to read."""
    if kb is None:
        return ""
    return "%d KB" % kb if kb < 1024 else "%.1f MB" % (kb / 1024)


def _row(u: dict) -> str:
    photo = _photo(u["photoKB"])
    return (
        '<tr class="%s">' % ("ok" if u["ok"] else "bad")
        + "<td>%s</td>" % u["at"][:16].replace("T", " ")
        + "<td>#%d</td>" % u["visitor"]
        + "<td>%s</td>" % u["device"]
        + "<td>%s</td>" % u["outcome"]
        + "<td>%s</td>" % u["seconds"]
        + "<td>%s</td>" % photo
        + "</tr>"
    )


def render_html(report: dict) -> str:
    """A phone-readable table, newest upload first. Browsers ask for
    text/html; everything else gets the JSON, so the same URL serves both."""
    t = report["totals"]
    head = " &middot; ".join(
        (
            "%d receipt%s uploaded" % (t["uploads"], "" if t["uploads"] == 1 else "s"),
            "%d parsed" % t["receiptsParsed"],
            "%d failed" % t["failedParses"],
            "%d visitor%s" % (t["visitors"], "" if t["visitors"] == 1 else "s"),
            "%d page view%s" % (t["pageViews"], "" if t["pageViews"] == 1 else "s"),
            "%d pay link%s opened" % (t["payLinkOpens"], "" if t["payLinkOpens"] == 1 else "s"),
            "%d bot hit%s" % (t["botHits"], "" if t["botHits"] == 1 else "s"),
            "%d request%s" % (t["requests"], "" if t["requests"] == 1 else "s"),
        )
    )
    rows = "".join(_row(u) for u in report["uploads"]) or (
        '<tr><td colspan="6">No receipts have been uploaded yet.</td></tr>'
    )
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SplitToWin &mdash; every receipt</title>"
        "<style>body{font:15px system-ui,sans-serif;margin:1rem;color:#222}"
        "table{border-collapse:collapse;width:100%}th,td{padding:.4rem .5rem;"
        "text-align:left;border-bottom:1px solid #ddd;white-space:nowrap}"
        "th{font-weight:600}tr.bad td:nth-child(4){color:#b00020}"
        "tr.bad td{background:#fff6f6}div{overflow-x:auto}p{color:#666}</style>"
        "<h2>Every receipt put through SplitToWin</h2>"
        "<p>" + head + "</p><div><table><tr><th>when</th><th>who</th>"
        "<th>device</th><th>outcome</th><th>secs</th><th>photo</th></tr>"
        + rows
        + "</table></div>"
        "<p>One row per upload, newest first. &ldquo;Who&rdquo; is a visitor "
        "number, not a person: the same number means the same device and "
        "address came back. The receipt itself never reaches the server, so "
        "what is on it cannot be shown here.</p>"
        "<p>Times in SGT. Generated "
        + report["generatedAt"][:19].replace("T", " ")
        + " from %d log file(s).</p>" % report["logFiles"]
    )
