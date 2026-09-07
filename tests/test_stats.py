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


def line(ts, ip, ua, method, uri, status=200, duration=0.01):
    return json.dumps(
        {
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
    )


def write_logs(dirpath):
    live = [
        line(T0, "1.1.1.1", IPHONE, "GET", "/"),
        line(T0 + 5, "1.1.1.1", IPHONE, "GET", "/assets/index-abc.js"),
        line(T0 + 30, "1.1.1.1", IPHONE, "POST", "/api/parse-receipt", 200, 12.0),
        line(T0 + 90, "1.1.1.1", IPHONE, "POST", "/api/parse-receipt", 200, 8.0),
        line(T0 + 100, "1.1.1.1", IPHONE, "GET", "/split"),
        line(T0 + 200, "2.2.2.2", ANDROID, "GET", "/"),  # looked, left: no row
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

    def test_one_row_per_person_who_parsed(self):
        report = stats.access_log_report(self.pattern, max_age_s=0)
        self.assertEqual(report["logFiles"], 2)
        self.assertEqual(
            report["totals"],
            {
                "requests": 11,
                "visitors": 5,  # 1.1.1.1, 2.2.2.2, 3.3.3.3, 5.5.5.5, 6.6.6.6
                "receiptsParsed": 2,
                "payLinkOpens": 1,
                "botHits": 1,
            },
        )
        people = report["people"]
        self.assertEqual([p["visitor"] for p in people], [2, 1])  # newest first
        newest, oldest = people
        self.assertEqual(newest["device"], "iPhone/iPad")
        self.assertEqual(newest["pageViews"], 2)  # "/" and "/split", not the asset
        self.assertEqual(newest["receipts"], 2)
        self.assertEqual(newest["failedParses"], 0)
        self.assertEqual(newest["avgParseSeconds"], 10.0)
        self.assertEqual(newest["firstSeen"], "2025-09-07T16:00:00+08:00")
        self.assertEqual(newest["lastSeen"], "2025-09-07T16:01:40+08:00")
        self.assertEqual(oldest["receipts"], 0)
        self.assertEqual(oldest["failedParses"], 1)
        self.assertIsNone(oldest["avgParseSeconds"])
        # The whole point of it being an open URL: no addresses leave.
        dumped = json.dumps(report)
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3", "5.5.5.5", "6.6.6.6"):
            self.assertNotIn(ip, dumped)

    def test_no_log_yet_is_an_empty_report_not_an_error(self):
        report = stats.access_log_report(os.path.join(self.tmp.name, "nope*"), 0)
        self.assertEqual(report["logFiles"], 0)
        self.assertEqual(report["people"], [])
        self.assertEqual(report["totals"]["requests"], 0)

    def test_cache_serves_the_same_report_within_max_age(self):
        first = stats.access_log_report(self.pattern, max_age_s=60)
        with open(os.path.join(self.tmp.name, "split2win.log"), "a") as f:
            f.write(line(T0 + 999, "9.9.9.9", IPHONE, "POST", "/api/parse-receipt") + "\n")
        self.assertIs(stats.access_log_report(self.pattern, max_age_s=60), first)
        self.assertEqual(len(stats.access_log_report(self.pattern, max_age_s=0)["people"]), 3)


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
        self.assertEqual(len(r.get_json()["people"]), 2)

    def test_browser_gets_a_table(self):
        r = self.client.get(
            "/stats", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/html")
        body = r.get_data(as_text=True)
        self.assertIn("<table>", body)
        self.assertIn("2 receipts parsed", body)
        self.assertNotIn("1.1.1.1", body)


if __name__ == "__main__":
    unittest.main()
