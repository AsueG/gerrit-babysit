"""Run from this directory: python3 -m unittest"""
import unittest
from unittest import mock

from memo import LatestMemo, PollMemo


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

    def test_a_failed_poll_keeps_what_was_known(self):
        # Given
        memo = PollMemo()
        with memo.poll():
            memo.get("a", str.upper, "a")
            memo.get("b", str.upper, "b")
        # When
        with self.assertRaises(OSError), memo.poll():
            memo.get("a", str.upper, "a")
            raise OSError("REST down")
        # Then
        self.assertEqual({"a": "A", "b": "B"}, memo.kept)


class LatestMemoTest(unittest.TestCase):
    def test_only_a_new_key_computes_again(self):
        # Given
        memo = LatestMemo()
        compute = mock.Mock(side_effect=str.upper)
        # When
        found = [memo.get(key, compute, key) for key in ("a", "a", "b", "a")]
        # Then
        self.assertEqual(["A", "A", "B", "A"], found)
        self.assertEqual(3, compute.call_count)

    def test_a_failed_compute_keeps_nothing(self):
        # Given
        memo = LatestMemo()
        memo.get("a", str.upper, "a")
        # When
        with self.assertRaises(OSError):
            memo.get("b", mock.Mock(side_effect=OSError("down")))
        # Then
        self.assertEqual("B", memo.get("b", str.upper, "b"))


if __name__ == "__main__":
    unittest.main()
