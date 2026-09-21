"""Every receipt upload, one row each — read off Caddy's access log.

The API stores nothing, so the only record of use is the JSON access log the
edge proxy writes, which compose mounts read-only into this container. A row
here is one submitted receipt photo, successful or not; loading the page and
leaving is not using the app, so that is a headline count instead.

Reading a receipt happens on a background job now, which the log cannot see
into: the upload itself only ever answers 202 "accepted". So a row is built
from two things — the POST that handed the photo over, and the progress
checks that followed it, which carry the job id in their path. A check that
came back an error is how the parse failed; the last check tells us how long
the person actually waited. A job nobody ever checked on is a phone that
walked away.

What a row can say is still bounded by what the log knows. The receipt itself
— merchant, items, totals, who owes what — never reaches the server, and pay
link payloads are blanked by the log filter before they are written.
Addresses are read only to tell visitors apart and are never returned, which
is what lets /stats be an open URL: there is nothing on it worth guarding
with a login.
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

# What the upload itself came back with. 202 means the photo was taken and a
# job started — how that job went is decided by the progress checks below.
# 200 only appears on lines from before parsing moved off the request.
UPLOAD_OUTCOMES = {
    200: "parsed",
    202: None,  # decided by the progress checks
    400: "bad upload",
    413: "photo too large",
    429: "rate limited",
}

# What a failed progress check means. Both 422s say the photo was no use;
# the log cannot tell "that's a menu" from "no lines could be read", and the
# difference does not change what the person has to do about it.
CHECK_OUTCOMES = {
    404: "expired before it was collected",
    422: "not a usable receipt",
    500: "server error",
    502: "parse failed",
    503: "readers busy",
}

PARSE_PATH = "/parse-receipt"


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


def _job_id(path_only: str) -> str:
    """The job id a /parse-receipt/<id> path carries, or "" for the bare path."""
    marker = PARSE_PATH + "/"
    if marker not in path_only:
        return ""
    return path_only.split(marker, 1)[1].split("/", 1)[0][:64]


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
    # job id -> what its progress checks said
    checks: dict[str, dict] = {}
    totals = {
        "requests": 0,
        "visitors": 0,
        "uploads": 0,
        "receiptsParsed": 0,
        "failedParses": 0,
        "outcomeUnknown": 0,
        "progressChecks": 0,
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

            if method == "POST" and PARSE_PATH in path_only:
                totals["uploads"] += 1
                uploads.append(
                    {
                        "at": _iso(ts),
                        "_ts": ts,
                        "_job": _job_id(path_only),
                        "visitor": visitors[key],
                        "device": device,
                        "status": status,
                        "seconds": round(float(entry.get("duration") or 0), 1),
                        "photoKB": _upload_kb(entry, req),
                    }
                )
            elif method == "GET" and PARSE_PATH in path_only:
                # A phone asking how its parse is going, about once a second.
                totals["progressChecks"] += 1
                job = _job_id(path_only)
                if not job:
                    continue
                seen = checks.setdefault(job, {"last": ts, "failure": None})
                seen["last"] = max(seen["last"], ts)
                if status >= 400:
                    seen["failure"] = status
            elif method == "GET" and path_only.startswith("/pay/"):
                # Someone opening the link they were sent.
                totals["payLinkOpens"] += 1
            elif method == "GET" and not path_only.startswith("/api/"):
                # An extensionless GET is the SPA loading, not an asset.
                if "." not in path_only.rsplit("/", 1)[-1]:
                    totals["pageViews"] += 1

    for row in uploads:
        job = row.pop("_job")
        _decide(row, checks.get(job) if job else None, tracked=bool(job))
        if row["ok"] is None:
            totals["outcomeUnknown"] += 1
        else:
            totals["receiptsParsed" if row["ok"] else "failedParses"] += 1
        row.pop("_ts", None)

    totals["visitors"] = len(visitors)
    uploads.sort(key=lambda u: u["at"], reverse=True)

    return {
        "generatedAt": _iso(time.time()),
        "logFiles": len(paths),
        "totals": totals,
        "uploads": uploads,
    }


def _decide(row: dict, seen: dict | None, tracked: bool = True) -> None:
    """Turn an upload plus its progress checks into one outcome.

    The upload's own status decides it outright when the photo never got as
    far as a job. Otherwise the checks do, and their last timestamp is how
    long the person actually waited — the upload's own duration is now just
    how long it took to hand the photo over.

    `ok` is None where the log cannot honestly say. Those rows are counted
    apart from both the successes and the failures rather than guessed into
    one of them: an unknown rounded to a number is worse than an unknown
    that admits it.
    """
    status = row["status"]
    outcome = UPLOAD_OUTCOMES.get(status, "error %d" % status)
    if outcome is not None:
        row["outcome"] = outcome
        row["ok"] = status in (200, 202)
        return

    if not tracked:
        # An upload from before the job id travelled in the path. It was
        # accepted; nothing in the log ties it to how it ended.
        row["outcome"] = "accepted, outcome not logged"
        row["ok"] = None
        return

    if seen is None:
        # Accepted, and then nobody ever asked how it went. The parse may
        # well have succeeded; the person was not there to receive it.
        row["outcome"] = "not collected"
        row["ok"] = None
        return

    waited = round(seen["last"] - row["_ts"], 1)
    if waited >= 0:
        row["seconds"] = waited
    failure = seen["failure"]
    row["outcome"] = (
        "parsed" if failure is None else CHECK_OUTCOMES.get(failure, "error %d" % failure)
    )
    row["ok"] = failure is None


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
    return (
        '<tr class="%s">'
        % ("unknown" if u["ok"] is None else "ok" if u["ok"] else "bad")
        + "<td>%s</td>" % u["at"][:16].replace("T", " ")
        + "<td>#%d</td>" % u["visitor"]
        + "<td>%s</td>" % u["device"]
        + "<td>%s</td>" % u["outcome"]
        + "<td>%s</td>" % u["seconds"]
        + "<td>%s</td>" % _photo(u["photoKB"])
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
            "%d unknown" % t["outcomeUnknown"],
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
        "tr.unknown td{color:#8a8a8a}"
        "tr.bad td{background:#fff6f6}div{overflow-x:auto}p{color:#666}</style>"
        "<h2>Every receipt put through SplitToWin</h2>"
        "<p>" + head + "</p><div><table><tr><th>when</th><th>who</th>"
        "<th>device</th><th>outcome</th><th>waited</th><th>photo</th></tr>"
        + rows
        + "</table></div>"
        "<p>One row per upload, newest first. &ldquo;Waited&rdquo; is how long "
        "the phone was asking before it got an answer. &ldquo;Who&rdquo; is a "
        "visitor number, not a person: the same number means the same device "
        "and address came back. The receipt itself never reaches the server, "
        "so what is on it cannot be shown here.</p>"
        "<p>Times in SGT. Generated "
        + report["generatedAt"][:19].replace("T", " ")
        + " from %d log file(s), plus %d progress check(s) not shown as rows.</p>"
        % (report["logFiles"], t["progressChecks"])
    )
