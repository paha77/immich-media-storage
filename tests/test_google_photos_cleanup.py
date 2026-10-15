import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "cleanup", Path(__file__).parents[1] / "google-photos-cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(cleanup, "STATE_DIR", root),
            patch.object(cleanup, "STATE_FILE", root / "state.json"),
            patch.object(cleanup, "REPORT", root / "report.md"),
            patch.object(cleanup, "EVENT_LOG", root / "events.jsonl"),
        ]
        for p in self.patches:
            p.start()
        self.state = cleanup.load_state([
            {"key": key, "filename": key + ".mp4", "filenames": [key + ".mp4"],
             "paths": [key + ".mp4"], "size": 1, "immich_status": ["uploaded"]}
            for key in ("a", "b")
        ])

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def claim(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cleanup.next_item(self.state)
        return json.loads(output.getvalue())

    def record(self, key, command="result", status="deleted"):
        with contextlib.redirect_stdout(io.StringIO()):
            cleanup.record_result(self.state, Namespace(
                key=key, command=command, status=status, detail="fixture"))

    def test_claim_resumes_without_increasing_attempts(self):
        self.assertEqual(self.claim()["attempts"], 1)
        self.assertEqual(self.claim()["attempts"], 1)

    def test_intent_survives_reload_and_blocks_other_candidates(self):
        self.claim()
        self.record("a", command="intent", status=None)
        candidates = self.state["items"]
        self.state = cleanup.load_state(candidates)
        self.assertEqual(self.claim()["status"], "needs_review")
        self.assertEqual(self.state["items"][1]["status"], "pending")
        self.assertIn("Needs review", cleanup.REPORT.read_text())

    def test_error_cannot_clear_uncertain_trash_action(self):
        self.claim()
        self.record("a", command="intent", status=None)
        self.record("a", status="error")
        self.assertEqual(self.claim()["status"], "needs_review")

    def test_confirmed_result_releases_next_candidate(self):
        self.claim()
        self.record("a", command="intent", status=None)
        self.record("a")
        self.assertEqual(self.claim()["key"], "b")

    def test_explicit_manual_retry_releases_review_and_increments_attempt(self):
        self.claim()
        self.record("a", command="intent", status=None)
        self.record("a", command="retry", status=None)
        self.assertEqual(self.claim()["attempts"], 2)

    def test_retry_is_rejected_without_review(self):
        self.claim()
        with self.assertRaises(RuntimeError):
            self.record("a", command="retry", status=None)

    def test_unclaimed_or_unknown_result_rejected(self):
        for key in ("a", "unknown"):
            with self.assertRaises(RuntimeError):
                self.record(key)


if __name__ == "__main__":
    unittest.main()
