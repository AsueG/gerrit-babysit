"""Run from this directory: python3 -m unittest"""
import unittest
from unittest import mock

from memo import PollMemo


class PollMemoTest(unittest.TestCase):
    def test_only_the_keys_of_the_last_poll_survive(self):
        # Given
        memo = PollMemo()
        with memo.poll():
            memo.get("old", str.upper, "old")
            memo.get("kept", str.upper, "kept")
        # When
        compute = mock.Mock(return_value="NEW")
        with memo.poll():
            found = [memo.get("kept", compute), memo.get("new", compute)]
        # Then
        self.assertEqual((["KEPT", "NEW"], 1, {"kept": "KEPT", "new": "NEW"}), (found, compute.call_count, memo.kept))

    def test_an_unknown_value_is_asked_again_on_the_next_poll(self):
        # Given
        memo = PollMemo()
        compute = mock.Mock(side_effect=[None, "known"])
        with memo.poll():
            memo.get("key", compute)
        # When
        with memo.poll():
            found = memo.get("key", compute)
        # Then
        self.assertEqual(("known", 2), (found, compute.call_count))


if __name__ == "__main__":
    unittest.main()
