"""Run from this directory: python3 -m unittest"""
import datetime
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import config  # noqa: E402
import snooze  # noqa: E402


def local(year, month, day, hour=0):
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


class SnoozeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(snooze, "SNOOZE", pathlib.Path(tmp.name) / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_date_snooze_holds_until_that_morning(self):
        # Given
        until = snooze.parse_date("2026-10-05")
        snooze.set_snooze(1, until=until)
        # When
        found = [bool(snooze.active({1: 3}, now)) for now in (until - 60, until)]
        # Then
        self.assertEqual([True, False], found)
        self.assertEqual(local(2026, 10, 5, config.CONFIG["work_hours"][0]), until)

    def test_a_patch_set_snooze_holds_until_a_newer_one(self):
        # Given
        snooze.set_snooze(1, patch_set=3)
        # When
        found = [bool(snooze.active({1: ps})) for ps in (3, "3", 4)]
        # Then
        self.assertEqual([True, True, False], found)

    def test_a_change_gone_from_the_query_is_not_snoozed(self):
        # Given
        snooze.set_snooze(1, patch_set=3)
        # When
        found = snooze.active({2: 1})
        # Then
        self.assertEqual({}, found)

    def test_tomorrow_on_a_friday_is_monday_morning(self):
        # Given
        friday = local(2026, 10, 2, 15)
        # When
        until = snooze.in_days(1, friday)
        # Then
        self.assertEqual(datetime.date(2026, 10, 5), datetime.date.fromtimestamp(until))

    def test_wake_and_expired_entries_are_pruned(self):
        # Given
        snooze.set_snooze(1, until=time.time() - 1)
        snooze.set_snooze(2, patch_set=1)
        snooze.set_snooze(3, until=time.time() + 3600)
        snooze.set_snooze(4, base_green=True)
        # When
        snooze.wake(3)
        # Then
        self.assertEqual({2: {"patch_set": 1}, 4: {"base_green": True}}, snooze.load())

    def test_a_base_green_snooze_holds_only_on_a_red_base(self):
        # Given
        snooze.set_snooze(1, base_green=True)
        # When
        found = [bool(snooze.active({1: 3}, base_red=red)) for red in ({1}, set())]
        # Then
        self.assertEqual([True, False], found)

    def test_forget_drops_only_the_given_changes(self):
        # Given
        snooze.set_snooze(1, patch_set=1)
        snooze.set_snooze(2, patch_set=4)
        # When
        snooze.forget({1, 9})
        # Then
        self.assertEqual({2: {"patch_set": 4}}, snooze.load())


if __name__ == "__main__":
    unittest.main()
