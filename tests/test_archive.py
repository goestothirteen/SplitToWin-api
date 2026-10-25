"""The receipt archive: photo, total and names kept per upload, and how
/stats shows them only to the key holder. Run: python -m unittest -v"""

import io
import json
import os
import tempfile
import time
import unittest

from PIL import Image

from app.config import Config
from app.services import archive as archive_mod
from app.services import jobs, receipt_parser, stats
from tests.test_parse import GOOD, FakeProvider, a_photo

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


def log_line(ts, method, uri, status):
    return json.dumps(
        {
            "ts": ts,
            "request": {
                "client_ip": "1.1.1.1",
                "method": method,
                "uri": uri,
                "headers": {"User-Agent": [IPHONE]},
            },
            "status": status,
            "duration": 0.1,
        }
    )


class ArchiveStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = archive_mod.Archive(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_photo_is_kept_small_and_the_row_opened(self):
        self.store.record_upload("job1", a_photo())
        self.assertTrue(self.store.has_photo("job1"))
        with Image.open(self.store.photo_path("job1")) as img:
            self.assertLessEqual(max(img.size), archive_mod.PHOTO_EDGE)
        rec = self.store.by_job()["job1"]
        self.assertEqual(rec["people"], [])
        self.assertTrue(rec["photo"])
        self.assertIsNone(rec["total"])

    def test_a_repeat_upload_does_not_reset_the_row(self):
        self.store.record_upload("job1", a_photo())
        self.store.record_people("job1", ["Mark", "Amy"])
        self.store.record_upload("job1", a_photo())  # the phone retrying
        self.assertEqual(self.store.by_job()["job1"]["people"], ["Mark", "Amy"])

    def test_names_are_cleaned_but_duplicates_survive(self):
        self.store.record_upload("job1", a_photo())
        ok = self.store.record_people(
            "job1", ["  Mark ", "Mark", "", 42, {"name": "Jo"}, "x" * 100]
        )
        self.assertTrue(ok)
        people = self.store.by_job()["job1"]["people"]
        self.assertEqual(people[:2], ["Mark", "Mark"])
        self.assertEqual(len(people), 3)
        self.assertEqual(len(people[2]), archive_mod.MAX_NAME_LEN)

    def test_names_for_an_unknown_upload_are_refused(self):
        self.assertFalse(self.store.record_people("never", ["Mark"]))
        self.assertFalse(self.store.record_people("../etc/passwd", ["Mark"]))
        self.assertEqual(self.store.by_job(), {})

    def test_something_that_is_not_an_image_still_gets_a_row(self):
        self.store.record_upload("job1", b"definitely not a jpeg")
        rec = self.store.by_job()["job1"]
        self.assertFalse(rec["photo"])
        self.assertFalse(self.store.has_photo("job1"))

    def test_switched_off_is_a_quiet_no_op(self):
        off = archive_mod.Archive("")
        off.record_upload("job1", a_photo())
        self.assertFalse(off.record_people("job1", ["Mark"]))
        self.assertEqual(off.by_job(), {})


class StatsMergeTest(unittest.TestCase):
    REPORT = {
        "generatedAt": "2026-10-26T12:00:00+08:00",
        "logFiles": 1,
        "totals": {
            "requests": 2, "visitors": 2, "uploads": 2, "receiptsParsed": 2,
            "failedParses": 0, "outcomeUnknown": 0, "progressChecks": 0,
            "pageViews": 0, "payLinkOpens": 0, "botHits": 0,
        },
        "uploads": [
            {"at": "2026-10-26T11:00:00+08:00", "job": "aaa", "visitor": 1,
             "device": "iPhone/iPad", "status": 202, "seconds": 9.0,
             "photoKB": 2048, "outcome": "parsed", "ok": True},
            {"at": "2026-10-25T11:00:00+08:00", "job": None, "visitor": 2,
             "device": "Android", "status": 200, "seconds": 12.0,
             "photoKB": 900, "outcome": "parsed", "ok": True},
        ],
    }
    RECORDS = {
        "aaa": {"at": "2026-10-26T11:00:00+08:00", "people": ["Mark", "Amy"],
                "currency": "SGD", "total": 46.7, "items": 2, "photo": True},
        "zzz": {"at": "2026-10-26T13:00:00+08:00", "people": [],
                "currency": "", "total": None, "items": None, "photo": True},
    }

    def merge(self, records=None):
        return stats.with_archive(
            self.REPORT, self.RECORDS if records is None else records,
            photo_url=lambda job: "/api/stats/photo/" + job,
        )

    def test_rows_carry_names_total_and_photo(self):
        out = self.merge()
        matched, older = out["uploads"]
        self.assertEqual(matched["people"], ["Mark", "Amy"])
        self.assertEqual(matched["total"], 46.7)
        self.assertEqual(matched["currency"], "SGD")
        self.assertEqual(matched["photo"], "/api/stats/photo/aaa")
        # An upload from before the archive existed: present, honest, empty.
        self.assertIsNone(older["people"])
        self.assertIsNone(older["photo"])
        self.assertEqual(out["archive"]["receipts"], 2)
        self.assertEqual(out["archive"]["withNames"], 1)
        self.assertEqual(out["archive"]["matchedToLog"], 1)
        # The cached report handed in was not written to.
        self.assertNotIn("archive", self.REPORT)
        self.assertNotIn("people", self.REPORT["uploads"][0])

    def test_html_shows_names_total_and_a_photo_link(self):
        shown = stats.render_html(self.merge())
        self.assertIn("split with", shown)
        self.assertIn("Mark, Amy", shown)
        self.assertIn("SGD 46.70", shown)
        self.assertIn('href="stats/photo/aaa"', shown)
        self.assertIn("2 on file, 1 with names", shown)

    def test_names_are_escaped_in_the_table(self):
        records = {"aaa": dict(self.RECORDS["aaa"], people=["<script>alert(1)</script>"])}
        out = self.merge(records)
        html = stats.render_html(out)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class EndToEndTest(unittest.TestCase):
    """Upload, parse, send names, read it all back off /stats."""

    def setUp(self):
        import app.routes as routes
        from app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.client = create_app().test_client()
        self.routes = routes

        self._saved = (routes.archive, routes.jobs, jobs.store,
                       receipt_parser.providers.resolve_chain,
                       Config.ACCESS_LOG_GLOB)
        routes.archive = archive_mod.Archive(os.path.join(self.tmp.name, "data"))
        jobs.store = routes.jobs = jobs.JobStore()
        receipt_parser.providers.resolve_chain = lambda *a, **k: [FakeProvider("fake", GOOD)]
        self.log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.log_dir)
        Config.ACCESS_LOG_GLOB = os.path.join(self.log_dir, "split2win*.log*")
        stats._cache.update(at=0.0, pattern=None, report=None)

    def tearDown(self):
        (self.routes.archive, self.routes.jobs, jobs.store,
         receipt_parser.providers.resolve_chain,
         Config.ACCESS_LOG_GLOB) = self._saved
        stats._cache.update(at=0.0, pattern=None, report=None)
        self.tmp.cleanup()

    def parse_one(self, job_id="job1"):
        response = self.client.post(
            "/parse-receipt/" + job_id,
            data={"image": (io.BytesIO(a_photo()), "receipt.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = self.client.get("/parse-receipt/" + job_id).get_json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        self.assertEqual(body["status"], "done")
        # What Caddy would have written for that upload.
        now = time.time()
        with open(os.path.join(self.log_dir, "split2win.log"), "w") as f:
            f.write(log_line(now - 20, "POST", "/api/parse-receipt/" + job_id, 202) + "\n")
            f.write(log_line(now - 10, "GET", "/api/parse-receipt/" + job_id, 200) + "\n")
        stats._cache.update(at=0.0, pattern=None, report=None)

    def stats_json(self):
        return self.client.get("/stats", headers={"Accept": "application/json"}).get_json()

    def test_names_total_and_photo_reach_stats(self):
        self.parse_one()
        sent = self.client.post("/parse-receipt/job1/people", json={"names": ["Mark", "Amy", "Jo"]})
        self.assertEqual(sent.status_code, 204)

        out = self.stats_json()
        self.assertEqual(out["archive"]["receipts"], 1)
        row = out["uploads"][0]
        self.assertEqual(row["people"], ["Mark", "Amy", "Jo"])
        self.assertEqual(row["total"], 46.7)
        self.assertEqual(row["currency"], "SGD")
        self.assertEqual(row["photo"], "/api/stats/photo/job1")

        html = self.client.get("/stats", headers={"Accept": "text/html"}).get_data(as_text=True)
        self.assertIn("Mark, Amy, Jo", html)

        photo = self.client.get("/stats/photo/job1")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/jpeg")
        photo.close()  # send_file holds the file open until the body is read
        self.assertEqual(self.client.get("/stats/photo/nope").status_code, 404)
        self.assertEqual(self.client.get("/stats/photo/..%2Freceipts.sqlite").status_code, 404)

    def test_the_latest_list_replaces_the_last(self):
        self.parse_one()
        self.client.post("/parse-receipt/job1/people", json={"names": ["Mark", "Amy"]})
        self.client.post("/parse-receipt/job1/people", json={"names": ["Mark"]})
        self.assertEqual(self.stats_json()["uploads"][0]["people"], ["Mark"])

    def test_names_need_a_real_upload_and_a_real_list(self):
        self.parse_one()
        self.assertEqual(
            self.client.post("/parse-receipt/ghost/people", json={"names": ["Mark"]}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/parse-receipt/job1/people", json={"names": "Mark"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/parse-receipt/job1/people", data="nope").status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
