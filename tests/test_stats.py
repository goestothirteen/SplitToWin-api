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


def write_logs(dirpath):
    live = [
        line(T0, "1.1.1.1", IPHONE, "GET", "/"),
        line(T0 + 5, "1.1.1.1", IPHONE, "GET", "/assets/index-abc.js"),
        line(T0 + 30, "1.1.1.1", IPHONE, "POST", "/api/parse-receipt", 200, 12.0, 512_000),
        line(T0 + 90, "1.1.1.1", IPHONE, "POST", "/api/parse-receipt", 200, 8.0),
        line(T0 + 100, "1.1.1.1", IPHONE, "GET", "/split"),
        line(T0 + 200, "2.2.2.2", ANDROID, "GET", "/"),  # looked, left: no upload
        line(T0 + 300, "3.3.3.3", ANDROID, "GET", "/pay/_"),  # link opened
        line(T0 + 400, "4.4.4.4", "curl/8.0", "GET", "/api/healthz"),  # bot
        line(T0 + 500, "5.5.5.5", IPHONE, "GET", "/api/stats"),  # not a view
        "this is not json",
    ]
    older = [
        line(T0 - 86400, "6.6.6.6", IPHONE, "GET", "/"),
        line(T0 - 86300, "6.6.6.6", IPHONE, "POST", "/api/parse-receipt", 429),
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

    def test_one_row_per_upload(self):
        report = stats.access_log_report(self.pattern, max_age_s=0)
        self.assertEqual(report["logFiles"], 2)
        self.assertEqual(
            report["totals"],
            {
                "requests": 11,
                "visitors": 5,  # 1.1.1.1, 2.2.2.2, 3.3.3.3, 5.5.5.5, 6.6.6.6
                "uploads": 3,
                "receiptsParsed": 2,
                "failedParses": 1,
                "pageViews": 4,  # two "/", "/split", and the older "/"
                "payLinkOpens": 1,
                "botHits": 1,
            },
        )
        uploads = report["uploads"]
        self.assertEqual(len(uploads), 3)
        # Newest first, and the rate-limited one from yesterday is last.
        self.assertEqual([u["at"][:16] for u in uploads][-1], "2025-09-06T16:01")
        newest, second, oldest = uploads

        self.assertEqual(newest["at"], "2025-09-07T16:01:30+08:00")
        self.assertEqual(newest["visitor"], 2)  # 6.6.6.6 appeared first
        self.assertEqual(newest["device"], "iPhone/iPad")
        self.assertTrue(newest["ok"])
        self.assertEqual(newest["outcome"], "parsed")
        self.assertEqual(newest["seconds"], 8.0)
        self.assertIsNone(newest["photoKB"])  # no size recorded on that line

        self.assertEqual(second["seconds"], 12.0)
        self.assertEqual(second["photoKB"], 500)

        self.assertFalse(oldest["ok"])
        self.assertEqual(oldest["outcome"], "rate limited")
        self.assertEqual(oldest["status"], 429)
        self.assertEqual(oldest["visitor"], 1)

        # The whole point of it being an open URL: no addresses leave.
        dumped = json.dumps(report)
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "5.5.5.5", "6.6.6.6"):
            self.assertNotIn(ip, dumped)

    def test_a_returning_phone_keeps_its_visitor_number(self):
        """Two uploads from one address are one visitor; a different address
        on the same kind of phone is a different one."""
        report = stats.access_log_report(self.pattern, max_age_s=0)
        newest, second, oldest = report["uploads"]
        self.assertEqual(newest["visitor"], second["visitor"])  # both 1.1.1.1
        self.assertNotEqual(newest["visitor"], oldest["visitor"])  # 6.6.6.6

    def test_an_unmapped_status_shows_the_code_rather_than_guessing(self):
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 600, "7.7.7.7", ANDROID, "POST", "/api/parse-receipt", 418) + "\n")
        report = stats.access_log_report(self.pattern, max_age_s=0)
        self.assertEqual(report["uploads"][0]["outcome"], "error 418")

    def test_no_log_yet_is_an_empty_report_not_an_error(self):
        report = stats.access_log_report(os.path.join(self.tmp.name, "nope*"), 0)
        self.assertEqual(report["logFiles"], 0)
        self.assertEqual(report["uploads"], [])
        self.assertEqual(report["totals"]["requests"], 0)

    def test_cache_serves_the_same_report_within_max_age(self):
        first = stats.access_log_report(self.pattern, max_age_s=60)
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 999, "9.9.9.9", IPHONE, "POST", "/api/parse-receipt") + "\n")
        self.assertIs(stats.access_log_report(self.pattern, max_age_s=60), first)
        self.assertEqual(len(stats.access_log_report(self.pattern, max_age_s=0)["uploads"]), 4)


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
        self.assertEqual(len(r.get_json()["uploads"]), 3)

    def test_browser_gets_a_table(self):
        r = self.client.get(
            "/stats", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/html")
        body = r.get_data(as_text=True)
        self.assertIn("<table>", body)
        self.assertIn("3 receipts uploaded", body)
        self.assertIn("rate limited", body)
        self.assertIn("500 KB", body)  # under a MB stays in KB
        self.assertNotIn("1.1.1.1", body)


if __name__ == "__main__":
    unittest.main()
