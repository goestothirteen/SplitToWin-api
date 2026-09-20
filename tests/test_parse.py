"""Upload, poll, and the three ways a parse ends.

No network: a fake provider stands in for the model, so these run anywhere.
Run: python -m unittest -v
"""

import io
import time
import unittest

from PIL import Image

from app.services import jobs, receipt_parser
from app.services.providers.base import ProviderError

GOOD = {
    "is_receipt": True,
    "reject_reason": "",
    "currency": "SGD",
    "items": [
        {"name": "Salted egg chicken rice", "quantity": 4, "line_total": 37.20,
         "category": "item"},
        {"name": "Seafood flat rice noodles", "quantity": 1, "line_total": 9.50,
         "category": "item"},
    ],
    "subtotal": 46.70,
    "total": 46.70,
    "warnings": [],
}


def a_photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (900, 1600), "white").save(out, format="JPEG")
    return out.getvalue()


class FakeProvider:
    """Stands in for a provider module. Same duck type: NAME + parse()."""

    def __init__(self, name, payload=None, error=None, items_seen=0, delay=0.0,
                 payloads=None):
        self.NAME = name
        self.payload = payload
        # When given, one payload per call: the first read, then the second look.
        self.payloads = list(payloads or [])
        self.error = error
        self.items_seen = items_seen
        self.delay = delay
        self.calls = 0

    def parse(self, image_bytes, mime_type, timeout_s, report=None,
              prompt=None, deliberate=False):
        self.calls += 1
        self.seen_timeout = timeout_s
        self.last_prompt = prompt
        self.last_deliberate = deliberate
        if self.payloads:
            self.payload = self.payloads.pop(0)
        if report and self.items_seen:
            for n in range(1, self.items_seen + 1):
                report(items=n)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.payload


class ParseTestBase(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.client = create_app().test_client()
        self.chain = []
        self._real_chain = receipt_parser.providers.resolve_chain
        receipt_parser.providers.resolve_chain = lambda *a, **k: self.chain
        # A fresh store per test, so one test's jobs can't be polled by another.
        self._real_store = jobs.store
        jobs.store = jobs.JobStore()
        import app.routes as routes

        routes.jobs = jobs.store

    def tearDown(self):
        receipt_parser.providers.resolve_chain = self._real_chain
        jobs.store = self._real_store
        import app.routes as routes

        routes.jobs = self._real_store

    def upload(self, photo=None):
        response = self.client.post(
            "/parse-receipt",
            data={"image": (io.BytesIO(photo or a_photo()), "receipt.jpg")},
            content_type="multipart/form-data",
        )
        return response

    def settle(self, job_id, timeout_s=10):
        """Poll like the phone does, until the job stops moving."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            body = self.client.get(f"/parse-receipt/{job_id}").get_json()
            if body["status"] in ("done", "failed"):
                return body
            time.sleep(0.02)
        self.fail(f"job {job_id} never finished: {body}")


class UploadTest(ParseTestBase):
    def test_upload_answers_immediately_with_a_job_to_poll(self):
        self.chain = [FakeProvider("fake", GOOD, delay=0.2)]
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["jobId"])
        self.assertIn(body["status"], ("queued", "running"))
        self.assertNotIn("receipt", body)

        final = self.settle(body["jobId"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(len(final["receipt"]["items"]), 2)
        self.assertEqual(final["receipt"]["provider"], "fake")
        self.assertEqual(final["receipt"]["warnings"], [])

    def test_progress_reports_lines_as_they_are_read(self):
        self.chain = [FakeProvider("fake", GOOD, items_seen=7, delay=0.4)]
        job_id = self.upload().get_json()["jobId"]

        seen_counts = set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            body = self.client.get(f"/parse-receipt/{job_id}").get_json()
            seen_counts.add(body["itemsFound"])
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        self.assertIn(7, seen_counts, "the live line count never reached the phone")
        self.assertGreaterEqual(
            self.client.get(f"/parse-receipt/{job_id}").get_json()["elapsedSeconds"], 0
        )

    def test_the_same_job_id_twice_does_not_read_the_receipt_twice(self):
        provider = FakeProvider("fake", GOOD, delay=0.3)
        self.chain = [provider]
        first = self.client.post(
            "/parse-receipt",
            data={"image": (io.BytesIO(a_photo()), "r.jpg"), "jobId": "abc123"},
            content_type="multipart/form-data",
        )
        second = self.client.post(
            "/parse-receipt",
            data={"image": (io.BytesIO(a_photo()), "r.jpg"), "jobId": "abc123"},
            content_type="multipart/form-data",
        )
        self.assertEqual(first.get_json()["jobId"], second.get_json()["jobId"])
        self.settle("abc123")
        self.assertEqual(provider.calls, 1)

    def test_polling_a_job_nobody_has_heard_of_is_a_clean_404(self):
        response = self.client.get("/parse-receipt/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "job_unknown")

    def test_an_empty_upload_is_refused_before_a_job_is_made(self):
        response = self.client.post(
            "/parse-receipt",
            data={"image": (io.BytesIO(b""), "r.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "empty")


class RejectionTest(ParseTestBase):
    def test_a_photo_that_is_not_a_receipt_is_refused_in_the_models_own_words(self):
        self.chain = [
            FakeProvider(
                "fake",
                {
                    "is_receipt": False,
                    "reject_reason": "That's the menu, not the bill — photograph the printed receipt.",
                    "currency": "",
                    "items": [],
                    "subtotal": 0,
                    "total": 0,
                    "warnings": [],
                },
            )
        ]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["code"], "not_a_receipt")
        self.assertIn("menu", final["error"])

    def test_a_rejection_does_not_waste_the_backup_reader_on_the_same_photo(self):
        backup = FakeProvider("backup", GOOD)
        self.chain = [
            FakeProvider(
                "fake",
                {"is_receipt": False, "reject_reason": "That's a plate of food.",
                 "currency": "", "items": [], "subtotal": 0, "total": 0, "warnings": []},
            ),
            backup,
        ]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(backup.calls, 0)

    def test_a_receipt_it_could_not_read_says_so_rather_than_returning_nothing(self):
        empty = dict(GOOD, items=[])
        self.chain = [FakeProvider("fake", empty)]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["code"], "no_items")
        self.assertIn("straighter photo", final["error"])


class WarningTest(ParseTestBase):
    def test_what_the_model_could_not_read_reaches_the_receipt(self):
        self.chain = [
            FakeProvider("fake", dict(GOOD, warnings=["The last line is cut off."]))
        ]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["receipt"]["warnings"], ["The last line is cut off."])

    def test_a_line_with_no_price_is_named_even_when_the_model_stays_quiet(self):
        payload = dict(
            GOOD,
            items=GOOD["items"] + [
                {"name": "Hokkien mee", "quantity": 1, "line_total": 0, "category": "item"}
            ],
        )
        self.chain = [FakeProvider("fake", payload)]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertIn("Hokkien mee", final["receipt"]["warnings"][0])


class FailoverTest(ParseTestBase):
    def test_the_backup_reader_gets_a_full_budget_not_the_leftovers(self):
        slow = FakeProvider(
            "slow", error=ProviderError("busy", transient=True), delay=0.05
        )
        backup = FakeProvider("backup", GOOD)
        self.chain = [slow, backup]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["receipt"]["provider"], "backup")
        # The old shared deadline handed the fallback whatever was left, which
        # in production was a second or two. It now starts fresh.
        from app.config import Config

        self.assertGreater(backup.seen_timeout, Config.PROVIDER_BUDGET_S - 10)

    def test_a_failure_with_no_backup_reaches_the_phone_as_a_readable_error(self):
        self.chain = [
            FakeProvider("fake", error=ProviderError("The reader is busy.", transient=False))
        ]
        final = self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error"], "The reader is busy.")


class SecondLookTest(ParseTestBase):
    """A bill that doesn't add up gets looked at again before it is accepted."""

    # The real case this came from: a receipt printing packaging and add-egg
    # lines whose prices are already inside the dish above them. Counting
    # them charges 61.20 for a 54.20 dinner.
    OVERCOUNTED = dict(
        GOOD,
        items=[
            {"name": "Salted egg chicken rice", "quantity": 4, "line_total": 37.20,
             "category": "item"},
            {"name": "Packaging", "quantity": 4, "line_total": 2.00, "category": "item"},
            {"name": "Add egg", "quantity": 4, "line_total": 4.00, "category": "item"},
            {"name": "Seafood hor fun", "quantity": 1, "line_total": 9.50,
             "category": "item"},
            {"name": "Hokkien mee", "quantity": 1, "line_total": 7.50, "category": "item"},
        ],
        total=54.20,
    )
    CORRECTED = dict(
        GOOD,
        items=[
            {"name": "Salted egg chicken rice", "quantity": 4, "line_total": 37.20,
             "category": "item"},
            {"name": "Seafood hor fun", "quantity": 1, "line_total": 9.50,
             "category": "item"},
            {"name": "Hokkien mee", "quantity": 1, "line_total": 7.50, "category": "item"},
        ],
        total=54.20,
    )

    def test_lines_that_do_not_add_up_are_read_again_and_corrected(self):
        provider = FakeProvider("fake", payloads=[self.OVERCOUNTED, self.CORRECTED])
        self.chain = [provider]
        final = self.settle(self.upload().get_json()["jobId"])

        self.assertEqual(final["status"], "done")
        self.assertEqual(provider.calls, 2)
        self.assertTrue(provider.last_deliberate, "the second look should think")
        self.assertIn("54.20", provider.last_prompt or "")
        receipt = final["receipt"]
        self.assertEqual(len(receipt["items"]), 3)
        self.assertIsNone(receipt["discrepancy"])
        self.assertEqual(receipt["warnings"], [])

    def test_a_second_look_that_is_no_better_leaves_the_first_read_alone(self):
        provider = FakeProvider("fake", payloads=[self.OVERCOUNTED, self.OVERCOUNTED])
        self.chain = [provider]
        final = self.settle(self.upload().get_json()["jobId"])

        self.assertEqual(final["status"], "done")
        self.assertEqual(provider.calls, 2)
        # The original stands, discrepancy and all, so the panel still warns.
        self.assertEqual(len(final["receipt"]["items"]), 5)
        self.assertAlmostEqual(final["receipt"]["discrepancy"], -6.0, places=2)

    def test_a_receipt_that_adds_up_is_never_read_twice(self):
        provider = FakeProvider("fake", GOOD)
        self.chain = [provider]
        self.settle(self.upload().get_json()["jobId"])
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
