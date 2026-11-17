from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawl_hadith import api, crawl, db
from crawl_hadith.api import ApiError

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        self.conn = db.connect(self.db_path)
        db.ensure_schema(self.conn)
        db.seed_ids(self.conn, [105542])
        self.conn.commit()

        self.byid = load("byid_105542.json")
        self.fallback = load("byid_fallback_105542.json")
        self.translations = load("translations_105542.json")
        self.explanations = load("explanations_105542.json")
        self.rejal_v2 = load("rejal_v2_105542.json")
        self.rejal_v1 = load("rejal_v1_105542.json")

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def full_bundle(self) -> crawl.Bundle:
        return crawl.Bundle(
            105542,
            {
                "main": crawl.StageResult("done", self.byid["data"][0]),
                "translate": crawl.StageResult("done", self.translations),
                "explanation": crawl.StageResult("done", self.explanations),
                "rejal": crawl.StageResult("done", self.rejal_v2),
            },
        )

    def test_ingest_and_idempotent(self) -> None:
        crawl.save_bundle(self.conn, self.full_bundle())
        counts = db.counts(self.conn)
        self.assertEqual(counts["hadiths"], 1)
        self.assertEqual(counts["translations"], len(self.translations["data"]))
        self.assertEqual(counts["explanations"], len(self.explanations["data"]))
        self.assertEqual(counts["hadith_group_members"], 6)
        self.assertEqual(counts["hadith_toc"], 3)

        row = self.conn.execute(
            "SELECT text_plain, source_id FROM hadiths WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(row["source_id"], 1348)
        self.assertIn("عَفَاف", row["text_plain"])

        status = self.conn.execute(
            "SELECT * FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["main_state"], "done")
        self.assertEqual(status["translate_state"], "done")
        self.assertEqual(status["explanation_state"], "done")
        self.assertEqual(status["rejal_state"], "done")

        segments = [
            segment
            for wrapper in self.rejal_v2["data"]["sanadList"]
            for segment in wrapper["sanad"]
        ]
        expected_ravi_links = sum(len(segment.get("raviList") or []) for segment in segments)
        expected_ravis = {
            ravi["raviId"]
            for segment in segments
            for ravi in segment.get("raviList") or []
        } | {ravi["raviId"] for ravi in self.rejal_v2["data"]["raviList"]}
        self.assertEqual(counts["hadith_sanad"], len(segments))
        self.assertEqual(counts["hadith_sanad_ravi"], expected_ravi_links)
        self.assertEqual(counts["ravis"], len(expected_ravis))

        # Re-ingesting the same response must not duplicate anything.
        crawl.save_bundle(self.conn, self.full_bundle())
        self.assertEqual(db.counts(self.conn), counts)

    def test_fallback_then_enrich(self) -> None:
        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105542,
                {"main": crawl.StageResult("fallback", self.fallback["data"])},
            ),
        )
        status = self.conn.execute(
            "SELECT main_state FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["main_state"], "fallback")
        row = self.conn.execute(
            "SELECT source_id, toc_text, text_plain FROM hadiths WHERE hadith_id = 105542"
        ).fetchone()
        self.assertIsNone(row["source_id"])
        self.assertTrue(row["toc_text"])
        self.assertTrue(row["text_plain"])

        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105542,
                {"main": crawl.StageResult("done", self.byid["data"][0])},
            ),
        )
        status = self.conn.execute(
            "SELECT main_state FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["main_state"], "done")
        row = self.conn.execute(
            "SELECT source_id FROM hadiths WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(row["source_id"], 1348)
        self.assertEqual(db.counts(self.conn)["hadith_group_members"], 6)
        self.assertEqual(db.counts(self.conn)["hadith_toc"], 3)

    def test_enrich_refetches_skipped_text_lists(self) -> None:
        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105542,
                {
                    "main": crawl.StageResult("fallback", self.fallback["data"]),
                    "translate": crawl.StageResult("skipped"),
                    "explanation": crawl.StageResult("skipped"),
                },
            ),
        )
        row = db.status_rows(self.conn, [105542])[0]
        self.assertEqual(row["translate_state"], "skipped")
        self.assertEqual(row["explanation_state"], "skipped")

        byid = self.byid
        translations = self.translations
        explanations = self.explanations
        rejal = self.rejal_v2

        class FakeClient:
            def hadith_detail(self, hadith_id):
                return byid["data"][0]

            def translations(self, hadith_id):
                return translations

            def explanations(self, hadith_id):
                return explanations

            def rejal(self, hadith_id):
                return rejal

        bundle = crawl.fetch_bundle(FakeClient(), row, enrich=True)
        self.assertEqual(bundle.stages["main"].state, "done")
        self.assertEqual(bundle.stages["translate"].state, "done")
        self.assertEqual(bundle.stages["explanation"].state, "done")
        crawl.save_bundle(self.conn, bundle)
        self.assertEqual(db.counts(self.conn)["translations"], len(translations["data"]))
        self.assertEqual(db.counts(self.conn)["explanations"], len(explanations["data"]))

    def test_failed_stage_preserves_data_and_resumes(self) -> None:
        crawl.save_bundle(self.conn, self.full_bundle())
        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105542,
                {"translate": crawl.StageResult("failed", None, "temporary failure")},
            ),
        )
        status = self.conn.execute(
            "SELECT translate_state, last_error FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["translate_state"], "failed")
        self.assertIn("temporary failure", status["last_error"])
        self.assertEqual(db.counts(self.conn)["translations"], len(self.translations["data"]))
        self.assertEqual(db.select_pending_ids(self.conn), [105542])

        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105542,
                {"translate": crawl.StageResult("done", self.translations)},
            ),
        )
        status = self.conn.execute(
            "SELECT translate_state FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["translate_state"], "done")
        self.assertEqual(db.select_pending_ids(self.conn), [])

    def test_fetch_bundle_uses_fallbacks(self) -> None:
        fallback_data = self.fallback["data"]
        rejal_v1 = self.rejal_v1

        class FakeClient:
            def hadith_detail(self, hadith_id):
                raise ApiError("primary down")

            def hadith_detail_fallback(self, hadith_id):
                return fallback_data

            def translations(self, hadith_id):
                return {"isSuccess": True, "data": []}

            def explanations(self, hadith_id):
                return {"isSuccess": True, "data": []}

            def rejal(self, hadith_id):
                raise ApiError("v2 down")

            def rejal_fallback(self, hadith_id):
                return rejal_v1

        row = {
            "hadith_id": 105542,
            "main_state": "pending",
            "translate_state": "pending",
            "explanation_state": "pending",
            "rejal_state": "pending",
            "translate_count": None,
            "explanation_count": None,
        }
        bundle = crawl.fetch_bundle(FakeClient(), row)
        self.assertEqual(bundle.stages["main"].state, "fallback")
        self.assertEqual(bundle.stages["rejal"].state, "fallback")
        self.assertEqual(bundle.stages["translate"].state, "skipped")
        self.assertEqual(bundle.stages["explanation"].state, "skipped")

        crawl.save_bundle(self.conn, bundle)
        status = self.conn.execute(
            "SELECT * FROM crawl_status WHERE hadith_id = 105542"
        ).fetchone()
        self.assertEqual(status["main_state"], "fallback")
        self.assertEqual(status["rejal_state"], "fallback")
        self.assertGreater(db.counts(self.conn)["hadith_sanad"], 0)

    def test_client_retries_application_errors(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return self.payload

        responses = [
            FakeResponse(
                json.dumps(
                    {
                        "isSuccess": False,
                        "statusCode": 500,
                        "message": "Response code 403",
                    }
                ).encode()
            ),
            FakeResponse(json.dumps({"isSuccess": True, "data": [1]}).encode()),
        ]
        client = api.Client(retries=3, backoff=0)
        with (
            mock.patch.object(api.urllib.request, "urlopen", side_effect=responses) as mocked,
            mock.patch.object(api.time, "sleep"),
        ):
            payload = client._request("/test")
        self.assertEqual(payload["data"], [1])
        self.assertEqual(mocked.call_count, 2)

        not_found = FakeResponse(
            json.dumps({"isSuccess": True, "statusCode": 404, "data": None}).encode()
        )
        with (
            mock.patch.object(api.urllib.request, "urlopen", return_value=not_found) as mocked,
            mock.patch.object(api.time, "sleep"),
        ):
            payload = client._request("/test")
        self.assertIsNone(payload["data"])
        self.assertEqual(mocked.call_count, 1)

    def test_same_toc_view(self) -> None:
        crawl.save_bundle(self.conn, self.full_bundle())
        other = load("byid_105540.json")
        crawl.save_bundle(
            self.conn,
            crawl.Bundle(
                105540,
                {"main": crawl.StageResult("done", other["data"][0])},
            ),
        )
        rows = self.conn.execute(
            """
            SELECT same_toc_hadith_id
            FROM v_same_toc_hadith
            WHERE hadith_id = 105540
            """
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [105542])

    def test_full_seed_resumes_after_failure(self) -> None:
        self.conn.execute("DELETE FROM crawl_status")
        self.conn.execute(
            "INSERT INTO sources(id, title) VALUES (1, 'test source')"
        )
        self.conn.commit()

        calls: list[int] = []
        fail_once = {"page": 2, "failed": False}

        class FakeSeedClient:
            def source_hadith_ids(self, source_id, page, page_size=50):
                calls.append(page)
                if page == fail_once["page"] and not fail_once["failed"]:
                    fail_once["failed"] = True
                    raise ApiError("temporary seed failure")
                return {1: [101, 102], 2: [103], 3: []}.get(page, [])

        failed = crawl.full_seed(self.conn, FakeSeedClient())
        self.assertEqual(failed, 1)
        seed = self.conn.execute(
            "SELECT status, next_page FROM source_seed WHERE source_id = 1"
        ).fetchone()
        self.assertEqual(seed["status"], "failed")
        self.assertEqual(seed["next_page"], 2)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT hadith_id FROM crawl_status")],
            [101, 102],
        )

        failed = crawl.full_seed(self.conn, FakeSeedClient())
        self.assertEqual(failed, 0)
        seed = self.conn.execute(
            "SELECT status, next_page FROM source_seed WHERE source_id = 1"
        ).fetchone()
        self.assertEqual(seed["status"], "done")
        self.assertEqual(seed["next_page"], 3)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT hadith_id FROM crawl_status")],
            [101, 102, 103],
        )
        self.assertEqual(calls, [1, 2, 2, 3])


if __name__ == "__main__":
    unittest.main()
