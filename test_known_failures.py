"""Run from this directory: python3 -m unittest"""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import fakes  # noqa: F401  (points the config at the fixtures before any module reads it)
import ci
import known_failures

LOG = "2026-09-29 10:00:00.1 | main | adb: device offline\n2026-09-29 10:00:01.1 | main | ERROR"


class KnownFailuresTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(known_failures, "KNOWN", pathlib.Path(tmp.name) / "known_failures.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_recorded_fix_is_found_again_on_a_look_alike_failure(self):
        # Given
        with mock.patch.object(ci, "http_get", return_value=LOG) as get:
            known_failures.record(9, 2, "app-e2e", "https://logs/x/", "emulator OOM", "bump the heap", now=100)
        # When
        found = ci.similar_failures(known_failures.load(), ci.log_tail(LOG.replace("10:00", "11:30")))
        # Then
        self.assertEqual("https://logs/x/job-output.txt", get.call_args.args[0])
        self.assertEqual([{"similarity": 1.0, "change": 9, "patch_set": 2, "job": "app-e2e", "cause": "emulator OOM",
                           "fix": "bump the heap", "recorded_at": 100}], found)

    def test_old_records_are_pruned_and_the_same_failure_is_overwritten(self):
        # Given
        with mock.patch.object(ci, "http_get", return_value=LOG):
            known_failures.record(1, 1, "j", "https://logs/a", "old", "f", now=1)
            known_failures.record(2, 1, "j", "https://logs/b", "first", "f", now=known_failures.RETENTION_S + 1)
            # When
            known_failures.record(2, 1, "j", "https://logs/b", "second", "f", now=known_failures.RETENTION_S + 2)
        # Then
        self.assertEqual({"2:1:j": "second"}, {k: r["cause"] for k, r in known_failures.load().items()})

    def test_an_empty_log_is_not_recorded(self):
        # Given
        with mock.patch.object(ci, "http_get", return_value=""):
            # When / Then
            with self.assertRaises(ValueError):
                known_failures.record(1, 1, "j", "https://logs/a", "c", "f")
        self.assertEqual({}, known_failures.load())

    def test_forget(self):
        # Given
        with mock.patch.object(ci, "http_get", return_value=LOG):
            known_failures.record(1, 1, "j", "https://logs/a", "c", "f")
        # When
        forgotten = [known_failures.forget(1, 1, "j"), known_failures.forget(1, 1, "j")]
        # Then
        self.assertEqual([True, False], forgotten)
        self.assertEqual({}, json.loads(known_failures.KNOWN.read_text()))


if __name__ == "__main__":
    unittest.main()
