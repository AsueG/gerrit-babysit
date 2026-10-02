"""Run from this directory: python3 -m unittest"""
import os
import unittest
from unittest import mock

import procs

# A realistic `ps -axo pid=,ppid=,command=` capture: pid/ppid right-padded, a command with
# spaces and arguments, a line too short to have a command, and a trailing blank line.
PS_OUTPUT = (
    "    1     0 /sbin/launchd\n"
    "  412     1 /usr/bin/python3 /path/watch.py --interval 60\n"
    "  413   412 /bin/zsh -c exec ps -axo pid=,ppid=,command=\n"
    "  414   413\n"
    "\n"
)


class ProcessesTest(unittest.TestCase):
    def test_parses_pid_ppid_and_a_command_with_spaces(self):
        # When
        with mock.patch.object(procs.subprocess, "run", return_value=mock.Mock(stdout=PS_OUTPUT)):
            table = procs.processes()
        # Then
        self.assertEqual(
            {1: (0, "/sbin/launchd"),
             412: (1, "/usr/bin/python3 /path/watch.py --interval 60"),
             413: (412, "/bin/zsh -c exec ps -axo pid=,ppid=,command=")},
            table)

    def test_a_line_without_a_command_is_dropped_rather_than_crashing(self):
        # Given: pid 414's command was empty, so its line only has two fields.
        with mock.patch.object(procs.subprocess, "run", return_value=mock.Mock(stdout=PS_OUTPUT)):
            table = procs.processes()
        # Then
        self.assertNotIn(414, table)

    def test_the_trailing_blank_line_is_dropped_rather_than_crashing(self):
        # Given: `ps` output ends with a newline, and this capture has one blank line besides.
        with mock.patch.object(procs.subprocess, "run", return_value=mock.Mock(stdout=PS_OUTPUT)):
            table = procs.processes()
        # Then
        self.assertEqual(3, len(table))

    def test_the_column_is_asked_for_last_so_ps_does_not_truncate_it(self):
        # When
        with mock.patch.object(procs.subprocess, "run", return_value=mock.Mock(stdout="")) as run:
            procs.processes("args")
        # Then
        self.assertEqual(["ps", "-axo", "pid=,ppid=,args="], run.call_args[0][0])


class AncestorsTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(procs.subprocess, "run", return_value=mock.Mock(stdout=PS_OUTPUT)):
            self.table = procs.processes()

    def test_walks_up_to_but_not_past_launchd(self):
        # When
        chain = list(procs.ancestors(413, self.table))
        # Then
        self.assertEqual([413, 412], chain)

    def test_an_unknown_pid_yields_nothing(self):
        # When
        chain = list(procs.ancestors(999, self.table))
        # Then
        self.assertEqual([], chain)

    def test_descends_from_matches_a_direct_ancestor_but_not_the_reverse(self):
        # When / Then
        self.assertTrue(procs.descends_from(413, 412, self.table))
        self.assertFalse(procs.descends_from(412, 413, self.table))


class RealPsTest(unittest.TestCase):
    def test_this_process_is_in_the_real_process_tree_with_its_real_ppid(self):
        # When
        table = procs.processes()
        # Then
        self.assertIn(os.getpid(), table)
        self.assertEqual(os.getppid(), table[os.getpid()][0])


if __name__ == "__main__":
    unittest.main()
