"""/stats against a hand-written Caddy log. Run: python -m unittest -v"""

import gzip
import json
import os
import tempfile
import unittest

from app.services import stats

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/120 Mobile Safari"
T0 = 1_757_232_000  # 2025-09-07 16:00 SGT


def line(ts, ip, ua, method, uri, status=200, duration=0.01, bytes_read=None):
    entry = {
        "ts": ts,
        "request": {
            "client_ip": ip,
            "method": method,
            "uri": uri,
            "headers": {"User-Agent": [ua]},
        },
        "status": status,
        "duration": duration,
    }
    if bytes_read is not None:
        entry["bytes_read"] = bytes_read
    return json.dumps(entry)


def upload_and_checks(ts, ip, ua, job, waited, final_status=200, kb=512_000):
    """One receipt the way it now appears in the log: a 202 upload, then a
    progress check every second until the job answers."""
    out = [line(ts, ip, ua, "POST", "/api/parse-receipt/" + job, 202, 0.4, kb)]
    for n in range(1, waited):
        out.append(line(ts + n, ip, ua, "GET", "/api/parse-receipt/" + job, 200, 0.01))
    out.append(
        line(ts + waited, ip, ua, "GET", "/api/parse-receipt/" + job, final_status, 0.01)
    )
    return out


def write_logs(dirpath):
    live = [
        line(T0, "1.1.1.1", IPHONE, "GET", "/"),
        line(T0 + 5, "1.1.1.1", IPHONE, "GET", "/assets/index-abc.js"),
    ]
    live += upload_and_checks(T0 + 30, "1.1.1.1", IPHONE, "aaa", 12)
    live += upload_and_checks(T0 + 90, "1.1.1.1", IPHONE, "bbb", 6, 422)
    live += [
        line(T0 + 100, "1.1.1.1", IPHONE, "GET", "/split"),
        line(T0 + 200, "2.2.2.2", ANDROID, "GET", "/"),  # looked, left: no upload
        line(T0 + 300, "3.3.3.3", ANDROID, "GET", "/pay/_"),  # link opened
        line(T0 + 400, "4.4.4.4", "curl/8.0", "GET", "/api/healthz"),  # bot
        line(T0 + 500, "5.5.5.5", IPHONE, "GET", "/api/stats"),  # not a view
        "this is not json",
    ]
    older = [
        line(T0 - 86400, "6.6.6.6", IPHONE, "GET", "/"),
        # Before parsing moved to a background job, the upload itself carried
        # the outcome. Those lines have to keep reading correctly.
        line(T0 - 86300, "6.6.6.6", IPHONE, "POST", "/api/parse-receipt", 429),
        line(T0 - 86200, "6.6.6.6", IPHONE, "POST", "/api/parse-receipt", 200, 18.0),
    ]
    with open(os.path.join(dirpath, "split2win.log"), "w") as f:
        f.write("\n".join(live) + "\n")
    with gzip.open(os.path.join(dirpath, "split2win-2026-09-06T00-00-00.000.log.gz"), "wt") as f:
        f.write("\n".join(older) + "\n")


class SummariseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        write_logs(self.tmp.name)
        self.pattern = os.path.join(self.tmp.name, "split2win*.log*")
        stats._cache.update(at=0.0, pattern=None, report=None)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self):
        return stats.access_log_report(self.pattern, max_age_s=0)

    def test_an_accepted_upload_is_not_an_error(self):
        """The regression this was written for: the upload answers 202 now,
        and every good receipt was being counted as a failure."""
        rows = self.report()["uploads"]
        parsed = [u for u in rows if u["outcome"] == "parsed"]
        self.assertEqual(len(parsed), 2)  # the 202 job and the old 200 line
        for row in parsed:
            self.assertTrue(row["ok"])
        self.assertNotIn("error 202", [u["outcome"] for u in rows])

    def test_how_the_background_job_ended_comes_from_the_progress_checks(self):
        by_time = {u["at"][:19]: u for u in self.report()["uploads"]}
        good = by_time["2025-09-07T16:00:30"]
        self.assertEqual(good["outcome"], "parsed")
        self.assertTrue(good["ok"])
        # Waited is the gap to the last check, not the upload's own duration.
        self.assertEqual(good["seconds"], 12.0)

        refused = by_time["2025-09-07T16:01:30"]
        self.assertEqual(refused["outcome"], "not a usable receipt")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["seconds"], 6.0)

    def test_totals_count_checks_apart_from_receipts(self):
        totals = self.report()["totals"]
        self.assertEqual(totals["uploads"], 4)
        self.assertEqual(totals["receiptsParsed"], 2)
        self.assertEqual(totals["failedParses"], 2)  # the 422 and the old 429
        self.assertEqual(totals["outcomeUnknown"], 0)
        self.assertEqual(totals["progressChecks"], 18)  # 12 + 6, never rows
        self.assertEqual(totals["pageViews"], 4)
        self.assertEqual(totals["payLinkOpens"], 1)
        self.assertEqual(totals["botHits"], 1)

    def test_an_upload_nobody_ever_checked_on_is_neither_a_pass_nor_a_fail(self):
        """The parse may well have succeeded; the person just wasn't there to
        receive it. Counting it either way would be a guess."""
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 600, "7.7.7.7", ANDROID, "POST",
                         "/api/parse-receipt/zzz", 202, 0.3) + "\n")
        report = self.report()
        row = report["uploads"][0]
        self.assertEqual(row["outcome"], "not collected")
        self.assertIsNone(row["ok"])
        self.assertEqual(report["totals"]["outcomeUnknown"], 1)
        self.assertEqual(report["totals"]["failedParses"], 2)  # unchanged

    def test_an_upload_from_before_job_ids_admits_it_cannot_say(self):
        """Lines already in the log posted to the bare path, so nothing ties
        them to the checks that followed. They were accepted; that is all
        that can honestly be claimed."""
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 650, "7.7.7.7", ANDROID, "POST",
                         "/api/parse-receipt", 202, 0.3) + "\n")
        report = self.report()
        row = report["uploads"][0]
        self.assertEqual(row["outcome"], "accepted, outcome not logged")
        self.assertIsNone(row["ok"])
        self.assertEqual(report["totals"]["outcomeUnknown"], 1)

    def test_a_job_the_server_forgot_reads_as_expired_not_as_a_bad_receipt(self):
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write("\n".join(upload_and_checks(T0 + 700, "8.8.8.8", IPHONE,
                                                "ddd", 3, 404)) + "\n")
        row = self.report()["uploads"][0]
        self.assertEqual(row["outcome"], "expired before it was collected")

    def test_an_upload_refused_at_the_door_never_reaches_the_checks(self):
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 800, "9.9.9.9", IPHONE, "POST",
                         "/api/parse-receipt/eee", 413, 0.2) + "\n")
        row = self.report()["uploads"][0]
        self.assertEqual(row["outcome"], "photo too large")
        self.assertFalse(row["ok"])

    def test_no_addresses_leave_however_the_rows_were_built(self):
        dumped = json.dumps(self.report())
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "5.5.5.5", "6.6.6.6"):
            self.assertNotIn(ip, dumped)

    def test_a_returning_phone_keeps_its_visitor_number(self):
        rows = self.report()["uploads"]
        iphone = {u["visitor"] for u in rows if u["device"] == "iPhone/iPad"}
        # 6.6.6.6 appeared first in the older file, 1.1.1.1 second.
        self.assertEqual(iphone, {1, 2})

    def test_no_log_yet_is_an_empty_report_not_an_error(self):
        report = stats.access_log_report(os.path.join(self.tmp.name, "nope*"), 0)
        self.assertEqual(report["logFiles"], 0)
        self.assertEqual(report["uploads"], [])
        self.assertEqual(report["totals"]["requests"], 0)

    def test_cache_serves_the_same_report_within_max_age(self):
        first = stats.access_log_report(self.pattern, max_age_s=60)
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 999, "9.9.9.9", IPHONE, "POST",
                         "/api/parse-receipt/fff", 202) + "\n")
        self.assertIs(stats.access_log_report(self.pattern, max_age_s=60), first)
        self.assertEqual(len(stats.access_log_report(self.pattern, max_age_s=0)["uploads"]), 5)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        write_logs(self.tmp.name)
        stats._cache.update(at=0.0, pattern=None, report=None)
        from app import create_app
        from app.config import Config

        Config.ACCESS_LOG_GLOB = os.path.join(self.tmp.name, "split2win*.log*")
        Config.STATS_CACHE_S = 0
        self.client = create_app().test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_curl_gets_json(self):
        r = self.client.get("/stats", headers={"Accept": "*/*"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(len(r.get_json()["uploads"]), 4)

    def test_browser_gets_a_table(self):
        r = self.client.get(
            "/stats", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/html")
        body = r.get_data(as_text=True)
        self.assertIn("<table>", body)
        self.assertIn("4 receipts uploaded", body)
        self.assertIn("not a usable receipt", body)
        self.assertIn("progress check", body)
        self.assertIn("0 unknown", body)
        self.assertNotIn("1.1.1.1", body)


if __name__ == "__main__":
    unittest.main()
