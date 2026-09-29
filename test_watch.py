"""Run from this directory: python3 -m unittest"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")

import ci  # noqa: E402
import watch  # noqa: E402
ZUUL = json.loads((FIXTURES / "zuul_messages.json").read_text())


def approval(label, value, by="someone"):
    return {"type": label, "value": str(value), "by": {"username": by}}


NOW = time.time()
DAY = "2026-09-29"
CHANGE_ID = "I" + "abc0" * 10


def patch_set(number=1, *approvals, parents=("base",), created=NOW):
    return {"number": number, "revision": f"rev{number}", "ref": f"refs/changes/01/1/{number}",
            "parents": list(parents), "approvals": list(approvals), "createdOn": created}


def message(author, text, timestamp=1):
    return {"reviewer": {"username": author, "name": author.title()}, "message": text, "timestamp": timestamp}


def change(number=1, current=None, comments=(), **extra):
    return {"number": number, "id": f"I{number}", "subject": f"subject {number}", "branch": "main",
            "url": f"https://review/{number}", "status": "NEW", "currentPatchSet": current or patch_set(),
            "comments": list(comments), **extra}


GREEN_CI = [approval(label, 1, "zuul") for label in watch.CI_LABELS]


def poll_result(changes=(), reviews=(), conflicts=None, parents=None, stale=None, threads=None):
    return {"changes": list(changes), "reviews": list(reviews), "conflicts": conflicts or {},
            "parents": parents or {}, "stale_parents": stale or {}, "threads": threads or {}}


class IsActionableTest(unittest.TestCase):
    def test_zuul_failure_is_actionable(self):
        # Given
        failed = ZUUL["failed"]
        # When
        actionable = watch.is_actionable(failed)
        # Then
        self.assertTrue(actionable)

    def test_zuul_merge_failed_is_actionable(self):
        # Given
        merge_failed = ZUUL["merge_failed"]
        # When
        actionable = watch.is_actionable(merge_failed)
        # Then
        self.assertTrue(actionable)

    def test_zuul_success_and_start_are_not_actionable(self):
        # Given
        noise = [ZUUL["succeeded"], ZUUL["starting"]]
        # When
        actionable = [watch.is_actionable(m) for m in noise]
        # Then
        self.assertEqual([False, False], actionable)

    def test_zuul_minus_one_outside_the_header_is_ignored(self):
        # Given
        text = "Patch Set 3: Build+1\n\nBuild Succeeded.\n- app-1 https://zuul/build/abc-1 : SUCCESS"
        # When
        actionable = watch.is_actionable(message("zuul", text))
        # Then
        self.assertFalse(actionable)

    def test_zuul_vote_removal_is_not_a_failure(self):
        # Given
        text = "Patch Set 2: -Integration\n\nStarting check-integration jobs."
        # When
        actionable = watch.is_actionable(message("zuul", text))
        # Then
        self.assertFalse(actionable)

    def test_my_own_messages_are_ignored_and_humans_are_not(self):
        # Given
        mine, theirs = message(watch.USER, "Done"), message("reviewer", "Why?")
        # When
        result = (watch.is_actionable(mine), watch.is_actionable(theirs))
        # Then
        self.assertEqual((False, True), result)

    def test_bare_positive_votes_are_ignored(self):
        # Given
        votes = [message("reviewer", "Patch Set 3: Code-Review+1"), message("reviewer", "Patch Set 3: Code-Review+2\n")]
        # When
        actionable = [watch.is_actionable(m) for m in votes]
        # Then
        self.assertEqual([False, False], actionable)

    def test_positive_vote_with_comments_or_negative_vote_is_actionable(self):
        # Given
        texts = ["Patch Set 3: Code-Review+1\n\n(1 comment)", "Patch Set 3: Code-Review-1"]
        # When
        actionable = [watch.is_actionable(message("reviewer", t)) for t in texts]
        # Then
        self.assertEqual([True, True], actionable)


def inline(id_, author, text, updated, reply_to=None, unresolved=True, line=3, ps=1):
    """A comment as REST `/changes/<n>/comments` returns it."""
    return {"id": id_, "in_reply_to": reply_to, "unresolved": unresolved, "patch_set": ps, "line": line,
            "updated": f"2026-09-29 10:00:{updated:02d}.000000000", "message": text,
            "author": {"username": author, "name": author.title()}}


class AwaitingThreadsTest(unittest.TestCase):
    def test_unresolved_threads_whose_last_word_is_not_mine(self):
        # Given
        answered = [inline("a1", "reviewer", "nit", 1), inline("a2", watch.USER, "Done", 2, "a1", unresolved=False)]
        open_ = [inline("b1", "reviewer", "why?", 3), inline("b2", watch.USER, "because", 4, "b1"),
                 inline("b3", "reviewer", "ok but", 5, "b2")]
        resolved_by_them = [inline("c1", "reviewer", "fyi", 6, unresolved=False)]
        comments = {"A.kt": answered + resolved_by_them, "B.kt": open_}
        # When
        threads = watch.awaiting_threads(comments)
        # Then
        self.assertEqual([("B.kt", 3, "Reviewer", "b3")],
                         [(t["file"], t["line"], t["author"], t["reply_to"]) for t in threads])
        self.assertEqual(["reviewer: why?", f"{watch.USER}: because", "reviewer: ok but"],
                         threads[0]["messages"])

    def test_a_bot_having_the_last_word_awaits_nobody(self):
        # Given
        comments = {"A.kt": [inline("a1", "autosubmit-bot", "auto", 1)], "B.kt": [inline("b1", "", "system", 2)]}
        # When
        threads = watch.awaiting_threads(comments)
        # Then
        self.assertEqual([], threads)

    def test_waiting_on_them_when_i_spoke_last(self):
        # Given
        comments = {"A.kt": [inline("a1", "reviewer", "why?", 1), inline("a2", watch.USER, "?", 2, "a1")]}
        # When
        threads = watch.awaiting_threads(comments)
        # Then
        self.assertEqual([], threads)


class OpenThreadsTest(unittest.TestCase):
    def setUp(self):
        watch._threads_memo = {}

    def test_rest_is_only_called_when_the_change_moves(self):
        # Given
        comments = {"A.kt": [inline("a1", "reviewer", "why?", 1)]}
        with mock.patch.object(watch, "rest_get", return_value=comments) as rest:
            # When
            first = watch.open_threads([change(1, lastUpdated=10)])
            second = watch.open_threads([change(1, lastUpdated=10)])
            watch.open_threads([change(1, lastUpdated=11)])
        # Then
        self.assertEqual(first, second)
        self.assertEqual(1, len(first[1]))
        self.assertEqual(2, rest.call_count)

    def test_an_expired_rest_token_does_not_fail_the_poll(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        with mock.patch.object(watch, "fetch_changes", return_value=[change(current=ps)]), \
                mock.patch.object(watch, "parent_statuses", return_value={}), \
                mock.patch.object(watch, "merge_conflicts", return_value={}), \
                mock.patch.object(watch, "stale_parents", return_value={}), \
                mock.patch.object(watch, "fetch_reviews", return_value=[]), \
                mock.patch.object(watch, "fetch_attention", return_value=[]), \
                mock.patch.object(watch, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            # When
            result = watch.poll()
        # Then
        self.assertEqual("HTTP Error 401: Unauthorized", result["threads_error"])
        self.assertEqual([], [e["kind"] for _, e in watch.events(result)])

    def test_unknown_threads_leave_human_messages_without_threads(self):
        # Given
        c = change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])
        result = {**poll_result([c]), "threads_error": "offline"}
        # When
        found = [e for _, e in watch.events(result)]
        # Then
        self.assertEqual([None], [e["threads_awaiting_me"] for e in found])

    def test_changes_without_threads_are_memoized_too(self):
        # Given
        with mock.patch.object(watch, "rest_get", return_value={}) as rest:
            # When
            watch.open_threads([change(1, lastUpdated=10)])
            watch.open_threads([change(1, lastUpdated=10)])
        # Then
        rest.assert_called_once_with("/changes/1/comments")


class ReadyToSubmitTest(unittest.TestCase):
    def test_plus_two_with_green_ci_is_ready(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        # When
        ready = watch.is_ready_to_submit(ps)
        # Then
        self.assertTrue(ready)

    def test_a_minus_one_next_to_the_plus_two_blocks(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), approval("Code-Review", -1, "other"), *GREEN_CI)
        # When
        ready = watch.is_ready_to_submit(ps)
        # Then
        self.assertFalse(ready)

    def test_missing_ci_label_blocks(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI[:2])
        # When
        ready = watch.is_ready_to_submit(ps)
        # Then
        self.assertFalse(ready)


class CiStateTest(unittest.TestCase):
    def test_merge_failed_is_a_stale_base(self):
        # Given
        ps = patch_set(1, *[approval(label, -1, "zuul") for label in watch.CI_LABELS])
        merge_failed = {**ZUUL["merge_failed"], "message": "Patch Set 1: Build-1\n\nMerge Failed.\n\nThis change"}
        c = change(current=ps, comments=[merge_failed])
        # When
        state = watch.ci_state(c, ps, watch.votes_of(ps))
        # Then
        self.assertEqual("stale_base", state)

    def test_merge_failed_on_an_older_patch_set_does_not_count(self):
        # Given
        ps = patch_set(2, approval("Build", -1, "zuul"))
        old = message("zuul", "Patch Set 1: Build-1\n\nMerge Failed.", timestamp=1)
        real = message("zuul", "Patch Set 2: Build-1\n\nBuild Failed (check-build pipeline).", timestamp=2)
        c = change(current=ps, comments=[old, real])
        # When
        state = watch.ci_state(c, ps, watch.votes_of(ps))
        # Then
        self.assertEqual("failed", state)

    def test_green_and_pending(self):
        # Given
        green, pending = patch_set(1, *GREEN_CI), patch_set(1, GREEN_CI[0])
        # When
        states = [watch.ci_state(change(current=ps), ps, watch.votes_of(ps)) for ps in (green, pending)]
        # Then
        self.assertEqual(["passed", "running"], states)


class EventsTest(unittest.TestCase):
    def test_messages_carry_the_author_username(self):
        # Given
        result = poll_result([change(comments=[ZUUL["failed"], message("reviewer", "nit")])])
        # When
        found = [e for _, e in watch.events(result)]
        # Then
        self.assertEqual(["zuul", "reviewer"], [e["author_username"] for e in found])

    def test_ready_to_submit_waits_for_an_open_parent(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        blocked = poll_result([change(current=ps)], parents={1: 99})
        free = poll_result([change(current=ps)])
        # When
        kinds = [[e["kind"] for _, e in watch.events(r)] for r in (blocked, free)]
        # Then
        self.assertEqual([[], ["ready_to_submit"]], kinds)

    def test_parent_merged_replaces_merge_conflict(self):
        # Given
        stale = {1: {"parent": 99, "old_parent_sha": "abc"}}
        result = poll_result([change()], conflicts={1: ["A.kt"]}, stale=stale)
        # When
        found = dict(watch.events(result))
        # Then
        self.assertEqual(["1:parent_merged:1"], list(found))
        self.assertEqual((99, "abc", ["A.kt"]),
                         (found["1:parent_merged:1"]["parent"], found["1:parent_merged:1"]["old_parent_sha"],
                          found["1:parent_merged:1"]["files"]))

    def test_conflict_without_merged_parent(self):
        # Given
        result = poll_result([change()], conflicts={1: ["A.kt"]})
        # When
        keys = list(dict(watch.events(result)))
        # Then
        self.assertEqual(["1:conflict:1"], keys)

    def test_zuul_verdict_on_a_replaced_patch_set_is_dropped(self):
        # Given
        late = message("zuul", "Patch Set 1: Build-1\n\nBuild Failed.", timestamp=5)
        result = poll_result([change(current=patch_set(2), comments=[late])])
        # When
        found = list(watch.events(result))
        # Then
        self.assertEqual([], found)

    def test_human_messages_carry_the_threads_awaiting_me(self):
        # Given
        c = change(comments=[message("reviewer", "Patch Set 1:\n\n(1 comment)"), ZUUL["failed"]])
        threads = {1: [{"file": "A.kt", "reply_to": "a1"}]}
        # When
        found = [e for _, e in watch.events(poll_result([c], threads=threads))]
        # Then
        self.assertEqual([1, None], [len(e["threads_awaiting_me"]) if "threads_awaiting_me" in e else None
                                     for e in found])

    def test_ci_silent_for_two_hours_is_stuck(self):
        # Given
        starting = message("zuul", "Patch Set 1:\n\nStarting check-integration jobs.", timestamp=NOW - 3 * 3600)
        stuck = change(current=patch_set(1, GREEN_CI[0], created=NOW - 4 * 3600), comments=[starting])
        rechecked = change(2, current=patch_set(1, created=NOW - 4 * 3600),
                           comments=[message("zuul", "Patch Set 1:\n\nStarting check-build jobs.", timestamp=NOW - 60)])
        green = change(3, current=patch_set(1, *GREEN_CI, created=NOW - 4 * 3600))
        # When
        keys = list(dict(watch.events(poll_result([stuck, rechecked, green]), NOW)))
        # Then
        self.assertEqual(["1:ci_stuck:1"], keys)

    def test_unresolved_threads_block_ready_to_submit(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        result = poll_result([change(current=ps)], threads={1: [{"file": "A.kt", "reply_to": "a1"}]})
        # When
        kinds_found = [e["kind"] for _, e in watch.events(result)]
        # Then
        self.assertEqual([], kinds_found)

    def test_zuul_failure_counts_my_earlier_rechecks_on_the_patch_set(self):
        # Given
        comments = [message(watch.USER, "Patch Set 1:\n\nrecheck-integration", timestamp=5),
                    message(watch.USER, "Patch Set 1:\n\nrecheck is not needed", timestamp=6),
                    message("zuul", "Patch Set 1: Integration-1\n\nBuild Failed.", timestamp=10),
                    message(watch.USER, "Patch Set 1:\n\nrecheck", timestamp=15)]
        # When
        found = [e for _, e in watch.events(poll_result([change(comments=comments)])) if e["kind"] == "message"]
        # Then
        self.assertEqual([1], [e["rechecks"] for e in found])

    def test_patch_set_unreviewed_for_two_working_days(self):
        # Given
        friday, tuesday = local(2026, 9, 25, 17), local(2026, 9, 29, 10)
        reviewers = [{"username": watch.USER}, {"username": "zuul"}, {"username": "r", "name": "Rev"}]
        waiting = change(current=patch_set(1, *GREEN_CI, created=friday), allReviewers=reviewers,
                         comments=[message("zuul", "Patch Set 1: Build+1", timestamp=friday + 60)])
        answered = change(2, current=patch_set(1, *GREEN_CI, created=friday),
                          comments=[message("r", "Patch Set 1:\n\nwhy?", timestamp=friday + 60)])
        voted = change(3, current=patch_set(1, approval("Code-Review", 1, "r"), *GREEN_CI, created=friday))
        wip = change(4, current=patch_set(1, *GREEN_CI, created=friday), wip=True)
        red = change(5, current=patch_set(1, approval("Build", -1, "zuul"), created=friday))
        # When
        found = {k: e for k, e in watch.events(poll_result([waiting, answered, voted, wip, red]), tuesday)
                 if e["kind"] == "waiting_for_review"}
        # Then
        self.assertEqual(["1:unreviewed:1:2026-09-29"], list(found))
        self.assertEqual((2, ["Rev"]), (found["1:unreviewed:1:2026-09-29"]["working_days"],
                                        found["1:unreviewed:1:2026-09-29"]["reviewers"]))

    def test_one_working_day_is_not_waiting_yet(self):
        # Given
        c = change(current=patch_set(1, *GREEN_CI, created=local(2026, 9, 28, 17)))
        # When
        found = list(watch.events(poll_result([c]), local(2026, 9, 29, 18)))
        # Then
        self.assertEqual([], found)

    def test_ready_to_submit_comes_back_each_working_day(self):
        # Given
        ps = patch_set(1, {**approval("Code-Review", 2), "grantedOn": 123}, *GREEN_CI)
        result = poll_result([change(current=ps)])
        tuesday, wednesday = local(2026, 9, 29, 10), local(2026, 9, 30, 10)
        # When
        keys = [list(dict(watch.events(result, now))) for now in (tuesday, wednesday)]
        # Then
        self.assertEqual([["1:ready:1:2026-09-29"], ["1:ready:1:2026-09-30"]], keys)
        self.assertEqual(123, dict(watch.events(result, tuesday))["1:ready:1:2026-09-29"]["ready_since"])


def local(year, month, day, hour):
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


class WorkingHoursTest(unittest.TestCase):
    def test_work_day_flips_in_the_morning_and_skips_weekends(self):
        # Given
        moments = {"tuesday 08:00": local(2026, 9, 29, 8), "tuesday 10:00": local(2026, 9, 29, 10),
                   "saturday": local(2026, 10, 3, 15), "monday 08:00": local(2026, 10, 5, 8),
                   "monday 09:00": local(2026, 10, 5, 9)}
        # When
        days = {name: watch.work_day(now) for name, now in moments.items()}
        # Then
        self.assertEqual({"tuesday 08:00": "2026-09-28", "tuesday 10:00": "2026-09-29", "saturday": "2026-10-02",
                          "monday 08:00": "2026-10-02", "monday 09:00": "2026-10-05"}, days)

    def test_quiet_at_night_and_on_weekends(self):
        # Given
        moments = [local(2026, 9, 29, 10), local(2026, 9, 29, 19), local(2026, 9, 29, 7), local(2026, 10, 4, 11)]
        # When
        quiet = [watch.is_quiet(now) for now in moments]
        # Then
        self.assertEqual([False, True, True, True], quiet)


class PendingEventsTest(unittest.TestCase):
    def test_failing_ci_is_reported_with_its_verdict(self):
        # Given
        ps = patch_set(1, approval("Build", -1, "zuul"))
        verdict = message("zuul", "Patch Set 1: Build-1\n\nBuild Failed.", timestamp=2)
        # When
        found = list(watch.pending_events(poll_result([change(current=ps, comments=[verdict])])))
        # Then
        self.assertEqual([("pending", "failed", verdict["message"])],
                         [(e["kind"], e["ci"], e.get("ci_verdict")) for e in found])

    def test_quiet_change_and_past_messages_are_not_replayed(self):
        # Given
        quiet = change(comments=[message("reviewer", "Patch Set 1:\n\nold remark")], current=patch_set(1, *GREEN_CI))
        # When
        found = list(watch.pending_events(poll_result([quiet])))
        # Then
        self.assertEqual([], found)

    def test_state_events_are_kept_and_reviews_i_joined_are_not_requests(self):
        # Given
        joined = review(patch_sets=[patch_set(1, approval("Code-Review", 1, watch.USER))])
        fresh = review(number=2)
        result = poll_result([change()], reviews=[joined, fresh], conflicts={1: ["A.kt"]})
        # When
        found = [(e["kind"], e["change"]) for e in watch.pending_events(result)]
        # Then
        self.assertEqual([("merge_conflict", 1), ("review_requested", 2)], found)


class RememberTest(unittest.TestCase):
    def test_vanished_changes_are_kept_for_a_while_then_forgotten(self):
        # Given
        old = NOW - watch.SEEN_RETENTION_S - 1
        seen = {"1:10:reviewer": old, "2:10:reviewer": NOW - 3600, "3:10:reviewer": old, "cleanup:live:sha": old}
        result = poll_result([change(1)])
        current = {"cleanup:live:sha": {}, "1:20:reviewer": {}}
        # When
        kept = watch.remember(seen, ["1:20:reviewer"], result, current, NOW)
        # Then
        self.assertEqual({"1:10:reviewer": NOW, "1:20:reviewer": NOW, "2:10:reviewer": NOW - 3600,
                          "cleanup:live:sha": NOW}, kept)

    def test_a_restored_change_does_not_replay(self):
        # Given
        c = change(2, comments=[message("reviewer", "old remark", timestamp=10)])
        seen = watch.remember({}, dict(watch.events(poll_result([c]))), poll_result([c]), {}, NOW - 86400)
        seen = watch.remember(seen, (), poll_result([]), {}, NOW - 3600)
        # When
        fresh = dict(watch.events(poll_result([c]))).keys() - seen.keys()
        # Then
        self.assertEqual(set(), fresh)


def review(current=1, patch_sets=None, comments=(), reviewers=1, **extra):
    owner = {"username": "owner", "name": "Owner"}
    people = [{"username": watch.USER}, {"username": watch.CI_USER}, owner]
    people += [{"username": f"r{i}"} for i in range(reviewers - 1)]
    sets = patch_sets or [patch_set(n) for n in range(1, current + 1)]
    return change(current=sets[-1], comments=comments, owner=owner, allReviewers=people, patchSets=sets, **extra)


def kinds(c):
    return [e["kind"] for _, e in watch.review_events(c, DAY)]


class ReviewEventsTest(unittest.TestCase):
    def test_recorded_change_where_my_plus_two_is_current(self):
        # Given
        recorded = json.loads((FIXTURES / "review_12345.json").read_text())
        # When
        found = kinds(recorded)
        # Then
        self.assertEqual(["review_requested"], found)

    def test_group_additions_are_not_review_requests(self):
        # Given
        crowded = review(reviewers=watch.MAX_REVIEWERS + 1)
        # When
        found = kinds(crowded)
        # Then
        self.assertEqual([], found)

    def test_wip_changes_are_not_review_requests(self):
        # Given
        wip = review(wip=True)
        # When
        found = kinds(wip)
        # Then
        self.assertEqual([], found)

    def test_new_patch_set_after_my_minus_one(self):
        # Given
        sets = [patch_set(1, approval("Code-Review", -1, watch.USER)), patch_set(2)]
        # When
        found = dict(watch.review_events(review(patch_sets=sets), DAY))
        # Then
        event = found["1:review_ps:2"]
        self.assertEqual((1, {"patch_set": 1, "value": -1}), (event["since_patch_set"], event["my_last_vote"]))
        self.assertEqual(("refs/changes/01/1/1", "refs/changes/01/1/2"), (event["since_ref"], event["current_ref"]))

    def test_untouched_request_comes_back_each_working_day_joined_one_does_not(self):
        # Given
        untouched = review()
        joined = review(number=2, patch_sets=[patch_set(1, approval("Code-Review", 1, watch.USER))])
        # When
        keys = [k for c in (untouched, joined) for day in (DAY, "2026-09-30")
                for k, e in watch.review_events(c, day) if e["kind"] == "review_requested"]
        # Then
        self.assertEqual(["1:review_requested:2026-09-29", "1:review_requested:2026-09-30",
                          "2:review_requested", "2:review_requested"], keys)

    def test_copied_vote_on_the_current_patch_set_means_nothing_to_do(self):
        # Given
        sets = [patch_set(1, approval("Code-Review", 2, watch.USER)),
                patch_set(2, approval("Code-Review", 2, watch.USER))]
        # When
        found = kinds(review(patch_sets=sets))
        # Then
        self.assertNotIn("review_new_patch_set", found)

    def test_comment_without_vote_counts_as_participation(self):
        # Given
        comments = [message(watch.USER, "Patch Set 1:\n\n(1 comment)", timestamp=10)]
        # When
        found = kinds(review(current=2, comments=comments))
        # Then
        self.assertIn("review_new_patch_set", found)

    def test_untouched_change_has_no_new_patch_set_event(self):
        # Given
        untouched = review(current=3)
        # When
        found = kinds(untouched)
        # Then
        self.assertEqual(["review_requested"], found)

    def test_owner_reply_after_my_comment(self):
        # Given
        comments = [message("owner", "Patch Set 1:\n\n(1 comment)", 5),
                    message(watch.USER, "Patch Set 1:\n\n(2 comments)", 10),
                    message("owner", "Uploaded patch set 2.", 20),
                    message("owner", "Patch Set 2: Patch Set 1 was rebased", 25),
                    message("other", "Patch Set 2:\n\nLGTM", 28),
                    message("owner", "Patch Set 2:\n\n(2 comments)", 30)]
        # When
        found = dict(watch.review_events(review(current=2, comments=comments), DAY))
        # Then
        self.assertEqual(["1:review_reply:30"], [k for k, e in found.items() if e["kind"] == "review_reply"])

    def test_another_reviewer_reply_counts_when_gerrit_wants_my_attention(self):
        # Given
        comments = [message(watch.USER, "Patch Set 1:\n\n(1 comment)", 10),
                    message("other", "Patch Set 1:\n\n(1 comment)", 20),
                    message("zuul", "Patch Set 1: Build+1", 25)]
        result = poll_result(reviews=[review(comments=comments)])
        # When
        replies = [[k for k, e in watch.events({**result, "attention": attention}) if e["kind"] == "review_reply"]
                   for attention in ([], [1])]
        # Then
        self.assertEqual([[], ["1:review_reply:20"]], replies)


class BotsTest(unittest.TestCase):
    def test_bot_messages_are_not_actionable(self):
        # Given
        messages = [message("autosubmit-bot", "Hashtag added: auto-submit"),
                    {"reviewer": {"name": "Gerrit Code Review"}, "message": "Abandoned\n\nAuto-Abandoned"}]
        # When
        actionable = [watch.is_actionable(m) for m in messages]
        # Then
        self.assertEqual([False, False], actionable)

    def test_bot_activity_does_not_count_as_a_review(self):
        # Given
        friday, tuesday = local(2026, 9, 25, 17), local(2026, 9, 29, 10)
        c = change(current=patch_set(1, *GREEN_CI, created=friday),
                   allReviewers=[{"username": "autosubmit-bot", "name": "autosubmit-bot"}, {"username": "r", "name": "Rev"}],
                   comments=[message("autosubmit-bot", "Hashtag added: auto-submit", timestamp=friday + 60)])
        # When
        found = [e for _, e in watch.events(poll_result([c]), tuesday) if e["kind"] == "waiting_for_review"]
        # Then
        self.assertEqual([["Rev"]], [e["reviewers"] for e in found])


class EnrichTest(unittest.TestCase):
    def test_stuck_ci_gets_the_zuul_queue_and_requests_the_prereview_path(self):
        # Given
        stuck = {"kind": "ci_stuck", "change": 1, "patch_set": 2}
        requested = {"kind": "review_requested", "change": 3, "patch_set": 4, "participated": False}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # When
        with mock.patch.object(ci, "zuul_queue", return_value=[]), \
                mock.patch.object(watch, "CACHE", pathlib.Path(tmp.name)):
            watch.enrich([stuck, requested])
        # Then
        self.assertEqual([], stuck["zuul_queue"])
        self.assertEqual({"path": f"{tmp.name}/prereview/3-4.md", "done": False}, requested["prereview"])

    def test_a_job_red_on_several_changes_fetches_its_history_once(self):
        # Given
        events = [{"kind": "message", "change": n, "branch": "main", "author_username": "zuul",
                   "message": ZUUL["failed"]["message"]} for n in (1, 2)]
        build = {"log_url": "https://logs/x/", "end_time": "2026-09-29T12:00:00", "artifacts": []}
        answers = {"/build/": json.dumps(build), "job-output.txt": "", "zuul-file-comments.json": "{}",
                   "/builds?": "[]"}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        jobs = {j["job"] for j in ci.failed_jobs(ZUUL["failed"]["message"])}
        # When
        with mock.patch.object(ci, "http_get", side_effect=get) as http, \
                mock.patch.object(ci, "ZUUL_API", "https://zuul/api"), \
                mock.patch.object(ci, "PERIODIC_BUILD", None):
            watch.enrich(events)
        # Then
        self.assertEqual(len(jobs), sum("/builds?" in c.args[0] for c in http.call_args_list))

    def test_only_ci_failures_and_new_patch_sets_get_extras(self):
        # Given
        failure = {"kind": "message", "change": 1, "branch": "main", "author_username": "zuul", "message": "m"}
        human = {"kind": "message", "change": 1, "branch": "main", "author_username": "reviewer", "message": "m"}
        patch = {"kind": "review_new_patch_set", "change": 2, "since_ref": "a", "current_ref": "b"}
        # When
        with mock.patch.object(ci, "diagnose_ci", return_value=["d"]), \
                mock.patch.object(ci, "base_build", return_value={"result": "SUCCESS"}), \
                mock.patch.object(watch, "interdiff", return_value={"stat": "s"}):
            watch.enrich([failure, human, patch])
        # Then
        self.assertEqual((["d"], {"result": "SUCCESS"}), (failure["ci_diagnosis"], failure["base_build"]))
        self.assertNotIn("ci_diagnosis", human)
        self.assertEqual({"stat": "s"}, patch["interdiff"])


class SettleTest(unittest.TestCase):
    def test_events_a_minute_apart_wake_the_session_once(self):
        # Given
        first = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        second = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10),
                                               message("reviewer", "Patch Set 1: Code-Review-1", 20)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = []
        with mock.patch.object(watch, "STATE", pathlib.Path(tmp.name) / "seen.json"), \
                mock.patch.object(watch, "CACHE", pathlib.Path(tmp.name)), \
                mock.patch.object(watch, "write_session_lock"), \
                mock.patch.object(watch, "daemon_poll", side_effect=[first, second]), \
                mock.patch.object(watch, "cleanup_candidates", return_value=[]), \
                mock.patch.object(watch.time, "sleep") as sleep, \
                mock.patch("sys.argv", ["watch.py"]), \
                mock.patch("builtins.print", side_effect=out.append):
            # When
            watch.main()
        # Then
        sleep.assert_called_once_with(watch.SETTLE_S)
        self.assertEqual(["1:10:reviewer", "1:20:reviewer"], sorted(json.loads((pathlib.Path(tmp.name) / "seen.json").read_text())))
        self.assertEqual(2, len(json.loads(out[0])["events"]))

    def test_a_crash_while_enriching_leaves_the_events_unseen(self):
        # Given
        result = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = pathlib.Path(tmp.name) / "seen.json"
        with mock.patch.object(watch, "STATE", state), \
                mock.patch.object(watch, "write_session_lock"), \
                mock.patch.object(watch, "daemon_poll", return_value=result), \
                mock.patch.object(watch, "cleanup_candidates", return_value=[]), \
                mock.patch.object(watch, "enrich", side_effect=RuntimeError("boom")), \
                mock.patch.object(watch.time, "sleep"), \
                mock.patch("sys.argv", ["watch.py"]):
            # When
            with self.assertRaises(RuntimeError):
                watch.main()
        # Then
        self.assertFalse(state.exists())


class SwiftbarTest(unittest.TestCase):
    def test_a_hung_swiftbar_does_not_kill_the_daemon(self):
        # Given
        hung = subprocess.TimeoutExpired("open", 10)
        # When
        with mock.patch.object(watch.subprocess, "run", side_effect=hung) as run:
            watch.swiftbar("refreshplugin", name="gerrit")
        # Then
        run.assert_called_once()


class GitRepoTest(unittest.TestCase):
    """A throwaway repo standing in for the checkout: REPO is resolved at call time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.commit("base")
        patcher = mock.patch.object(watch, "REPO", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, subject, change_id=None):
        message = f"{subject}\n\nChange-Id: {change_id}" if change_id else subject
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


class StaleParentsTest(GitRepoTest):
    def test_parent_landed_under_another_sha(self):
        # Given
        self.git("checkout", "-q", "-b", "stack")
        old_parent = self.commit("parent")
        self.git("checkout", "-q", "main")
        self.commit("parent, rebased on submit")
        self.git("update-ref", f"{watch.FETCH_NAMESPACE}/main", "main")
        c = change(current=patch_set(1, parents=(old_parent,)))
        # When
        stale = watch.stale_parents([c], {1: (99, "MERGED")})
        # Then
        self.assertEqual({1: {"parent": 99, "old_parent_sha": old_parent}}, stale)

    def test_parent_landed_as_is_or_still_open(self):
        # Given
        parent = self.commit("parent")
        self.git("update-ref", f"{watch.FETCH_NAMESPACE}/main", "main")
        c = change(current=patch_set(1, parents=(parent,)))
        # When
        results = [watch.stale_parents([c], {1: (99, status)}) for status in ("MERGED", "NEW")]
        # Then
        self.assertEqual([{}, {}], results)


class MergeConflictsTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.repo))

    def conflicting_change(self, number, branch):
        (self.repo / "A.kt").write_text("base\n")
        self.git("add", "A.kt")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-f", branch)
        (self.repo / "A.kt").write_text("theirs\n")
        self.git("commit", "-q", "-am", "theirs")
        self.git("checkout", "-q", "--detach", branch)
        (self.repo / "A.kt").write_text("mine\n")
        self.git("commit", "-q", "-am", "mine")
        ref = f"refs/changes/0{number}/{number}/1"
        self.git("update-ref", ref, "HEAD")
        self.git("checkout", "-q", "main")
        self.git("branch", "-f", branch, "main")
        return change(number, current={**patch_set(1), "revision": self.git("rev-parse", ref), "ref": ref},
                      branch=branch)

    def test_a_vanished_branch_does_not_hide_other_conflicts(self):
        # Given
        live = self.conflicting_change(1, "release")
        gone = change(2, current={**patch_set(1), "revision": "0" * 40, "ref": "refs/changes/02/2/1"},
                      branch="deleted")
        # When
        conflicts = watch.merge_conflicts([live, gone])
        # Then
        self.assertEqual({1: ["A.kt"]}, conflicts)

    def test_fetch_failing_for_every_ref_raises(self):
        # Given
        self.git("remote", "set-url", "origin", str(self.repo / "missing"))
        c = change(1, current={**patch_set(1), "revision": "0" * 40})
        # When / Then
        with self.assertRaises(subprocess.CalledProcessError):
            watch.merge_conflicts([c])


class InterdiffTest(GitRepoTest):
    def publish(self, patch_set, files):
        """Writes `files` on a commit off `main` and publishes it as refs/changes/01/1/<patch_set>."""
        self.git("checkout", "-q", "--detach", "main")
        for name, text in files.items():
            (self.repo / name).write_text(text)
        self.git("add", *files)
        self.git("commit", "-q", "-m", f"ps{patch_set}")
        self.git("update-ref", f"refs/changes/01/1/{patch_set}", "HEAD")
        self.git("checkout", "-q", "main")

    def event(self):
        return {"change": 1, "since_ref": "refs/changes/01/1/1", "current_ref": "refs/changes/01/1/2"}

    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.repo))

    def test_rebased_patch_set_only_shows_the_change_files(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        (self.repo / "Upstream.kt").write_text("x\n")
        self.git("add", "Upstream.kt")
        self.git("commit", "-q", "-m", "upstream")
        self.publish(2, {"A.kt": "two\n"})
        # When
        found = watch.interdiff(self.event())
        # Then
        self.assertTrue(found["rebased"])
        self.assertIn("A.kt", found["stat"])
        self.assertNotIn("Upstream.kt", found["stat"])
        self.assertEqual("", self.git("for-each-ref", f"{watch.FETCH_NAMESPACE}/review"))

    def test_same_base(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        self.publish(2, {"A.kt": "one\n", "B.kt": "new\n"})
        # When
        found = watch.interdiff(self.event())
        # Then
        self.assertFalse(found["rebased"])
        self.assertIn("B.kt", found["stat"])

    def test_a_git_timeout_is_reported_and_the_refs_are_removed(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        self.publish(2, {"A.kt": "two\n"})
        real = watch.git_run

        def git_run(*args, **kwargs):
            if args[0] == "diff":
                raise subprocess.TimeoutExpired("git diff", 60)
            return real(*args, **kwargs)
        # When
        with mock.patch.object(watch, "git_run", side_effect=git_run):
            found = watch.interdiff(self.event())
        # Then
        self.assertIn("timed out", found["error"])
        self.assertEqual("", self.git("for-each-ref", f"{watch.FETCH_NAMESPACE}/review"))


class ParentStatusesTest(unittest.TestCase):
    def test_mine_are_open_and_others_are_queried(self):
        # Given
        changes = [change(1, dependsOn=[{"number": 2}]), change(2, dependsOn=[{"number": 50}]), change(3)]
        # When
        with mock.patch.object(watch, "gerrit_query", return_value=[{"number": 50, "status": "MERGED"}]) as query:
            statuses = watch.parent_statuses(changes)
        # Then
        self.assertEqual({1: (2, "NEW"), 2: (50, "MERGED")}, statuses)
        query.assert_called_once_with("change:50")


class CleanupCandidatesTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        watch._cleanup_memo.clear()

    def run_cleanup(self, merged_revisions, open_ids=frozenset()):
        rows = [{"id": CHANGE_ID, "number": 7, "subject": "s", "patchSets": [{"revision": r} for r in merged_revisions]}]
        with mock.patch.object(watch, "gerrit_query", return_value=rows) as self.query:
            return [e for _, e in watch.cleanup_candidates(open_ids)]

    def test_unchanged_branches_are_not_queried_twice(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        first = self.run_cleanup([pushed])
        # When
        second = self.run_cleanup([pushed])
        # Then
        self.assertEqual(first, second)
        self.query.assert_not_called()

    def test_branches_of_open_changes_are_skipped(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed], open_ids=frozenset({CHANGE_ID}))
        # Then
        self.assertEqual([], found)
        self.query.assert_not_called()

    def test_branch_at_a_merged_patch_set_is_a_candidate(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([("feature", 7)], [(e["branch"], e["change"]) for e in found])

    def test_local_amend_after_merge_is_kept(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", f"feat v2\n\nChange-Id: {CHANGE_ID}")
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)

    def test_a_dirty_worktree_is_held_back_until_clean_without_a_new_query(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        worktree = pathlib.Path(self.tmp.name + "-wt")
        self.git("worktree", "add", "-q", str(worktree), "feature")
        self.addCleanup(subprocess.run, ["rm", "-rf", str(worktree)])
        (worktree / "scratch.txt").write_text("wip\n")
        dirty = self.run_cleanup([pushed])
        (worktree / "scratch.txt").unlink()
        # When
        clean = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], dirty)
        self.assertEqual(["feature"], [e["branch"] for e in clean])
        self.query.assert_not_called()

    def test_checked_out_and_protected_branches_are_excluded(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)

    def test_a_malformed_change_id_never_reaches_the_query(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", "I1' OR owner:someone")
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)
        self.query.assert_not_called()


class WorktreesByChangeIdTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        watch._worktrees_memo.clear()

    def test_recomputed_only_when_a_head_moves(self):
        # Given
        self.commit("one", CHANGE_ID)
        first = watch.worktrees_by_change_id()
        with mock.patch.object(watch, "git", wraps=watch.git) as git:
            unchanged = watch.worktrees_by_change_id()
        other = "I" + "def1" * 10
        self.commit("two", other)
        # When
        moved = watch.worktrees_by_change_id()
        # Then
        self.assertEqual(first, unchanged)
        self.assertEqual(1, git.call_count)
        self.assertEqual({CHANGE_ID, other}, set(moved))


class RestGetTest(unittest.TestCase):
    def setUp(self):
        watch._rest_auth = None
        self.addCleanup(setattr, watch, "_rest_auth", None)

    @staticmethod
    def response(body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = f")]}}'\n{json.dumps(body)}".encode()
        return response

    def test_a_rotated_token_is_reread_once(self):
        # Given
        unauthorized = watch.urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())
        credentials = mock.Mock(side_effect=[("me", "old"), ("me", "new")])
        # When
        with mock.patch.object(watch, "http_credentials", credentials), \
                mock.patch.object(watch.urllib.request, "urlopen",
                                  side_effect=[unauthorized, self.response({"ok": 1})]) as urlopen:
            found = watch.rest_get("/changes/1/comments")
        # Then
        self.assertEqual({"ok": 1}, found)
        self.assertEqual(2, credentials.call_count)
        self.assertIn("me:new".encode(), [watch.base64.b64decode(c.args[0].get_header("Authorization")[6:])
                                           for c in urlopen.call_args_list])

    def test_a_token_still_refused_after_rereading_raises(self):
        # Given
        unauthorized = watch.urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())
        self.addCleanup(unauthorized.close)
        # When / Then
        with mock.patch.object(watch, "http_credentials", return_value=("me", "bad")), \
                mock.patch.object(watch.urllib.request, "urlopen", side_effect=unauthorized) as urlopen, \
                self.assertRaises(watch.urllib.error.HTTPError):
            watch.rest_get("/changes/1/comments")
        self.assertEqual(2, urlopen.call_count)


class DaemonTest(unittest.TestCase):
    class Stop(Exception):
        pass

    def run_daemon(self, rounds, **patches):
        sleeps = mock.Mock(side_effect=[None] * (rounds - 1) + [self.Stop()])
        stderr = io.StringIO()
        with mock.patch.object(watch.time, "sleep", sleeps), \
                mock.patch.object(watch, "swiftbar"), \
                mock.patch.object(watch, "write_daemon_poll"), \
                mock.patch("sys.stderr", stderr), \
                contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(watch, name, value))
            with self.assertRaises(self.Stop):
                watch.daemon(60)
        return stderr.getvalue()

    def test_a_bug_after_the_poll_is_logged_once_and_the_daemon_keeps_going(self):
        # Given
        status = mock.Mock()
        # When
        log = self.run_daemon(3, poll=mock.Mock(return_value={}), write_status=status,
                              daemon_round=mock.Mock(side_effect=KeyError("number")))
        # Then
        self.assertEqual(1, log.count("Traceback"))
        self.assertEqual([mock.call(error="KeyError: 'number'")] * 3, status.call_args_list)

    def test_a_repeated_poll_failure_is_logged_once(self):
        # Given
        down = OSError("Could not resolve hostname")
        # When
        log = self.run_daemon(3, poll=mock.Mock(side_effect=[down, down, {}]), write_status=mock.Mock(),
                              daemon_round=mock.Mock())
        # Then
        self.assertEqual(1, log.count("poll failed"))
        self.assertIn("recovered", log)


class NotificationsTest(unittest.TestCase):
    def event(self, number):
        return {"kind": "merge_conflict", "change": number, "files": ["a/A.kt"], "url": f"https://review/{number}"}

    def test_a_few_events_notify_one_by_one_and_open_their_change(self):
        # Given
        fresh = [self.event(n) for n in (1, 2, 3)] + [{"kind": "cleanup_candidate"}]
        # When
        found = watch.notifications(fresh)
        # Then
        self.assertEqual([("1 has conflicts", "A.kt", "https://review/1"), ("2 has conflicts", "A.kt", "https://review/2"),
                          ("3 has conflicts", "A.kt", "https://review/3")], found)

    def test_a_burst_becomes_a_single_summary_opening_the_dashboard(self):
        # Given
        fresh = [self.event(n) for n in (1, 2, 3, 4)]
        # When
        found = watch.notifications(fresh)
        # Then
        self.assertEqual([("4 Gerrit events", "1 has conflicts · 2 has conflicts · 3 has conflicts · 4 has conflicts",
                           "https://gerrit.example.com/dashboard/self")], found)

    def test_waiting_for_review(self):
        # Given
        event = {"kind": "waiting_for_review", "change": 1, "working_days": 3, "subject": "s"}
        # When
        found = watch.notification(event)
        # Then
        self.assertEqual(("1 unreviewed for 3 working days", "s"), found)


class WithIntKeysTest(unittest.TestCase):
    def test_old_daemon_payload_gets_defaults(self):
        # Given
        payload = json.loads(json.dumps({"changes": [], "conflicts": {"1": ["A.kt"]}, "parents": {"2": 3}}))
        # When
        result = watch.with_int_keys(payload)
        # Then
        self.assertEqual(({1: ["A.kt"]}, {2: 3}, {}, [], []),
                         (result["conflicts"], result["parents"], result["stale_parents"], result["reviews"],
                          result["attention"]))


if __name__ == "__main__":
    unittest.main()
