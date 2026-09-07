"""Who has used the app, one row per person — read off Caddy's access log.

The API stores nothing, so the only record of use is the JSON access log the
edge proxy writes, which compose mounts read-only into this container. A
"person" here is an address plus a device class, which is as close as HTTP
metadata gets; a row is only made for someone who actually submitted a
receipt, because loading the page and leaving is not using the app.

Only aggregates leave this module. Addresses are needed to tell visitors
apart but are never returned, which is what lets /stats be an open URL:
there is nothing on it worth guarding with a login. Names, items and
assignments never reach the server at all, so they cannot be here either.
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


def summarise(paths) -> dict:
    """Fold every log line in `paths` into the report. Pure: no caching, no IO
    beyond reading the files given, so tests can hand it a temp directory."""
    visitors: "OrderedDict[tuple, dict]" = OrderedDict()
    totals = {
        "requests": 0,
        "visitors": 0,
        "receiptsParsed": 0,
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
            row = visitors.get((ip, device))
            if row is None:
                row = visitors[(ip, device)] = {
                    "device": device,
                    "firstSeen": ts,
                    "lastSeen": ts,
                    "pageViews": 0,
                    "receipts": 0,
                    "failedParses": 0,
                    "_parseSeconds": 0.0,
                }
            row["firstSeen"] = min(row["firstSeen"], ts)
            row["lastSeen"] = max(row["lastSeen"], ts)

            path_only = (req.get("uri") or "").split("?", 1)[0]
            method = req.get("method", "")
            status = entry.get("status", 0)

            if method == "POST" and "parse-receipt" in path_only:
                if status == 200:
                    row["receipts"] += 1
                    row["_parseSeconds"] += float(entry.get("duration") or 0)
                else:
                    row["failedParses"] += 1
            elif method == "GET" and path_only.startswith("/pay/"):
                # Someone opening the link they were sent. Counted, but it is
                # not "using the app" in the sense a row means.
                totals["payLinkOpens"] += 1
            elif method == "GET" and not path_only.startswith("/api/"):
                # An extensionless GET is the SPA loading, not an asset.
                if "." not in path_only.rsplit("/", 1)[-1]:
                    row["pageViews"] += 1

    totals["visitors"] = len(visitors)

    # Number people in the order they first appeared, so a row keeps its
    # number between refreshes as the log grows.
    rows = []
    for n, row in enumerate(visitors.values(), start=1):
        attempts = row["receipts"] + row["failedParses"]
        if not attempts:
            continue
        totals["receiptsParsed"] += row["receipts"]
        rows.append(
            {
                "visitor": n,
                "device": row["device"],
                "firstSeen": _iso(row["firstSeen"]),
                "lastSeen": _iso(row["lastSeen"]),
                "pageViews": row["pageViews"],
                "receipts": row["receipts"],
                "failedParses": row["failedParses"],
                "avgParseSeconds": (
                    round(row["_parseSeconds"] / row["receipts"], 1)
                    if row["receipts"]
                    else None
                ),
            }
        )
    rows.sort(key=lambda r: r["lastSeen"], reverse=True)

    return {
        "generatedAt": _iso(time.time()),
        "logFiles": len(paths),
        "totals": totals,
        "people": rows,
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


def render_html(report: dict) -> str:
    """A phone-readable table. Browsers ask for text/html; everything else
    gets the JSON, so the same URL serves both."""
    t = report["totals"]
    head = (
        f"{t['visitors']} visitors · {t['receiptsParsed']} receipts parsed · "
        f"{t['payLinkOpens']} pay links opened · {t['botHits']} bot hits · "
        f"{t['requests']} requests"
    )
    cells = "".join(
        "<tr>"
        f"<td>{r['visitor']}</td><td>{r['device']}</td>"
        f"<td>{r['firstSeen'][:16].replace('T', ' ')}</td>"
        f"<td>{r['lastSeen'][:16].replace('T', ' ')}</td>"
        f"<td>{r['pageViews']}</td><td>{r['receipts']}</td>"
        f"<td>{r['failedParses']}</td>"
        f"<td>{'' if r['avgParseSeconds'] is None else r['avgParseSeconds']}</td>"
        "</tr>"
        for r in report["people"]
    ) or '<tr><td colspan="8">Nobody has parsed a receipt yet.</td></tr>'
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SplitToWin — who used it</title>"
        "<style>body{font:15px system-ui,sans-serif;margin:1rem;color:#222}"
        "table{border-collapse:collapse;width:100%}th,td{padding:.4rem .5rem;"
        "text-align:left;border-bottom:1px solid #ddd;white-space:nowrap}"
        "th{font-weight:600}div{overflow-x:auto}p{color:#666}</style>"
        "<h2>Who used SplitToWin</h2>"
        f"<p>{head}</p><div><table><tr><th>#</th><th>device</th>"
        "<th>first seen</th><th>last seen</th><th>views</th><th>receipts</th>"
        f"<th>failed</th><th>avg s</th></tr>{cells}</table></div>"
        f"<p>Times in SGT. Generated {report['generatedAt'][:19].replace('T', ' ')}"
        f" from {report['logFiles']} log file(s).</p>"
    )
