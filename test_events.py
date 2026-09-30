"""Run from this directory: python3 -m unittest"""
import json
import time
import unittest

# First: points the config at the fixtures before any module reads it.
from fakes import FIXTURES, ZUUL, NOW, DAY, GREEN_CI, approval, patch_set, message, change, poll_result, inline, review
import events
import gerrit


class IsActionableTest(unittest.TestCase):
    def test_zuul_failure_is_actionable(self):
        # Given
        failed = ZUUL["failed"]
        # When
        actionable = events.is_actionable(failed)
        # Then
        self.assertTrue(actionable)

    def test_zuul_merge_failed_is_actionable(self):
        # Given
        merge_failed = ZUUL["merge_failed"]
        # When
        actionable = events.is_actionable(merge_failed)
        # Then
        self.assertTrue(actionable)

    def test_zuul_success_and_start_are_not_actionable(self):
        # Given
        noise = [ZUUL["succeeded"], ZUUL["starting"]]
        # When
        actionable = [events.is_actionable(m) for m in noise]
        # Then
        self.assertEqual([False, False], actionable)

    def test_zuul_minus_one_outside_the_header_is_ignored(self):
        # Given
        text = "Patch Set 3: Build+1\n\nBuild Succeeded.\n- app-1 https://zuul/build/abc-1 : SUCCESS"
        # When
        actionable = events.is_actionable(message("zuul", text))
        # Then
        self.assertFalse(actionable)

    def test_zuul_vote_removal_is_not_a_failure(self):
        # Given
        text = "Patch Set 2: -Integration\n\nStarting check-integration jobs."
        # When
        actionable = events.is_actionable(message("zuul", text))
        # Then
        self.assertFalse(actionable)

    def test_my_own_messages_are_ignored_and_humans_are_not(self):
        # Given
        mine, theirs = message(gerrit.USER, "Done"), message("reviewer", "Why?")
        # When
        result = (events.is_actionable(mine), events.is_actionable(theirs))
        # Then
        self.assertEqual((False, True), result)

    def test_bare_positive_votes_are_ignored(self):
        # Given
        votes = [message("reviewer", "Patch Set 3: Code-Review+1"), message("reviewer", "Patch Set 3: Code-Review+2\n")]
        # When
        actionable = [events.is_actionable(m) for m in votes]
        # Then
        self.assertEqual([False, False], actionable)

    def test_positive_vote_with_comments_or_negative_vote_is_actionable(self):
        # Given
        texts = ["Patch Set 3: Code-Review+1\n\n(1 comment)", "Patch Set 3: Code-Review-1"]
        # When
        actionable = [events.is_actionable(message("reviewer", t)) for t in texts]
        # Then
        self.assertEqual([True, True], actionable)


class AwaitingThreadsTest(unittest.TestCase):
    def test_unresolved_threads_whose_last_word_is_not_mine(self):
        # Given
        answered = [inline("a1", "reviewer", "nit", 1), inline("a2", gerrit.USER, "Done", 2, "a1", unresolved=False)]
        open_ = [inline("b1", "reviewer", "why?", 3), inline("b2", gerrit.USER, "because", 4, "b1"),
                 inline("b3", "reviewer", "ok but", 5, "b2")]
        resolved_by_them = [inline("c1", "reviewer", "fyi", 6, unresolved=False)]
        comments = {"A.kt": answered + resolved_by_them, "B.kt": open_}
        # When
        threads = events.awaiting_threads(comments)
        # Then
        self.assertEqual([("B.kt", 3, "Reviewer", "b3")],
                         [(t["file"], t["line"], t["author"], t["reply_to"]) for t in threads])
        self.assertEqual(["reviewer: why?", f"{gerrit.USER}: because", "reviewer: ok but"],
                         threads[0]["messages"])

    def test_a_bot_having_the_last_word_awaits_nobody(self):
        # Given
        comments = {"A.kt": [inline("a1", "autosubmit-bot", "auto", 1)], "B.kt": [inline("b1", "", "system", 2)]}
        # When
        threads = events.awaiting_threads(comments)
        # Then
        self.assertEqual([], threads)

    def test_waiting_on_them_when_i_spoke_last(self):
        # Given
        comments = {"A.kt": [inline("a1", "reviewer", "why?", 1), inline("a2", gerrit.USER, "?", 2, "a1")]}
        # When
        threads = events.awaiting_threads(comments)
        # Then
        self.assertEqual([], threads)


class ReadyToSubmitTest(unittest.TestCase):
    def test_plus_two_with_green_ci_is_ready(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        # When
        ready = events.is_ready_to_submit(ps)
        # Then
        self.assertTrue(ready)

    def test_a_minus_one_next_to_the_plus_two_blocks(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), approval("Code-Review", -1, "other"), *GREEN_CI)
        # When
        ready = events.is_ready_to_submit(ps)
        # Then
        self.assertFalse(ready)

    def test_a_ci_plus_two_counts_as_green(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *[approval(label, 2, "zuul") for label in events.CI_LABELS])
        # When
        ready = events.is_ready_to_submit(ps)
        # Then
        self.assertTrue(ready)

    def test_missing_ci_label_blocks(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI[:2])
        # When
        ready = events.is_ready_to_submit(ps)
        # Then
        self.assertFalse(ready)


class CiStateTest(unittest.TestCase):
    def test_merge_failed_is_a_stale_base(self):
        # Given
        ps = patch_set(1, *[approval(label, -1, "zuul") for label in events.CI_LABELS])
        merge_failed = {**ZUUL["merge_failed"], "message": "Patch Set 1: Build-1\n\nMerge Failed.\n\nThis change"}
        c = change(current=ps, comments=[merge_failed])
        # When
        state = events.ci_state(c, ps, events.votes_of(ps))
        # Then
        self.assertEqual("stale_base", state)

    def test_merge_failed_on_an_older_patch_set_does_not_count(self):
        # Given
        ps = patch_set(2, approval("Build", -1, "zuul"))
        old = message("zuul", "Patch Set 1: Build-1\n\nMerge Failed.", timestamp=1)
        real = message("zuul", "Patch Set 2: Build-1\n\nBuild Failed (check-build pipeline).", timestamp=2)
        c = change(current=ps, comments=[old, real])
        # When
        state = events.ci_state(c, ps, events.votes_of(ps))
        # Then
        self.assertEqual("failed", state)

    def test_green_and_pending(self):
        # Given
        green, pending = patch_set(1, *GREEN_CI), patch_set(1, GREEN_CI[0])
        # When
        states = [events.ci_state(change(current=ps), ps, events.votes_of(ps)) for ps in (green, pending)]
        # Then
        self.assertEqual(["passed", "running"], states)

    def test_a_ci_plus_two_is_passed_not_running(self):
        # Given
        ps = patch_set(1, *[approval(label, 2, "zuul") for label in events.CI_LABELS])
        # When
        state = events.ci_state(change(current=ps), ps, events.votes_of(ps))
        # Then
        self.assertEqual("passed", state)


class EventsTest(unittest.TestCase):
    def test_messages_carry_the_author_username(self):
        # Given
        result = poll_result([change(comments=[ZUUL["failed"], message("reviewer", "nit")])])
        # When
        found = [e for _, e in events.events(result)]
        # Then
        self.assertEqual(["zuul", "reviewer"], [e["author_username"] for e in found])

    def test_ready_to_submit_waits_for_an_open_parent(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        blocked = poll_result([change(current=ps)], parents={1: 99})
        free = poll_result([change(current=ps)])
        # When
        kinds = [[e["kind"] for _, e in events.events(r)] for r in (blocked, free)]
        # Then
        self.assertEqual([[], ["ready_to_submit"]], kinds)

    def test_parent_merged_replaces_merge_conflict(self):
        # Given
        stale = {1: {"parent": 99, "old_parent_sha": "abc"}}
        result = poll_result([change()], conflicts={1: ["A.kt"]}, stale=stale)
        # When
        found = dict(events.events(result))
        # Then
        self.assertEqual(["1:parent_merged:1"], list(found))
        self.assertEqual((99, "abc", ["A.kt"]),
                         (found["1:parent_merged:1"]["parent"], found["1:parent_merged:1"]["old_parent_sha"],
                          found["1:parent_merged:1"]["files"]))

    def test_conflict_without_merged_parent(self):
        # Given
        result = poll_result([change()], conflicts={1: ["A.kt"]})
        # When
        keys = list(dict(events.events(result)))
        # Then
        self.assertEqual(["1:conflict:1"], keys)

    def test_zuul_verdict_on_a_replaced_patch_set_is_dropped(self):
        # Given
        late = message("zuul", "Patch Set 1: Build-1\n\nBuild Failed.", timestamp=5)
        result = poll_result([change(current=patch_set(2), comments=[late])])
        # When
        found = list(events.events(result))
        # Then
        self.assertEqual([], found)

    def test_human_messages_carry_the_threads_awaiting_me(self):
        # Given
        c = change(comments=[message("reviewer", "Patch Set 1:\n\n(1 comment)"), ZUUL["failed"]])
        threads = {1: [{"file": "A.kt", "reply_to": "a1"}]}
        # When
        found = [e for _, e in events.events(poll_result([c], threads=threads))]
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
        keys = list(dict(events.events(poll_result([stuck, rechecked, green]), NOW)))
        # Then
        self.assertEqual(["1:ci_stuck:1"], keys)

    def test_unresolved_threads_block_ready_to_submit(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        result = poll_result([change(current=ps)], threads={1: [{"file": "A.kt", "reply_to": "a1"}]})
        # When
        kinds_found = [e["kind"] for _, e in events.events(result)]
        # Then
        self.assertEqual([], kinds_found)

    def test_zuul_failure_counts_my_earlier_rechecks_on_the_patch_set(self):
        # Given
        comments = [message(gerrit.USER, "Patch Set 1:\n\nrecheck-integration", timestamp=5),
                    message(gerrit.USER, "Patch Set 1:\n\nrecheck is not needed", timestamp=6),
                    message("zuul", "Patch Set 1: Integration-1\n\nBuild Failed.", timestamp=10),
                    message(gerrit.USER, "Patch Set 1:\n\nrecheck", timestamp=15)]
        # When
        found = [e for _, e in events.events(poll_result([change(comments=comments)])) if e["kind"] == "message"]
        # Then
        self.assertEqual([1], [e["rechecks"] for e in found])

    def test_patch_set_unreviewed_for_two_working_days(self):
        # Given
        friday, tuesday = local(2026, 9, 25, 17), local(2026, 9, 29, 10)
        reviewers = [{"username": gerrit.USER}, {"username": "zuul"}, {"username": "r", "name": "Rev"}]
        waiting = change(current=patch_set(1, *GREEN_CI, created=friday), allReviewers=reviewers,
                         comments=[message("zuul", "Patch Set 1: Build+1", timestamp=friday + 60)])
        answered = change(2, current=patch_set(1, *GREEN_CI, created=friday),
                          comments=[message("r", "Patch Set 1:\n\nwhy?", timestamp=friday + 60)])
        voted = change(3, current=patch_set(1, approval("Code-Review", 1, "r"), *GREEN_CI, created=friday))
        wip = change(4, current=patch_set(1, *GREEN_CI, created=friday), wip=True)
        red = change(5, current=patch_set(1, approval("Build", -1, "zuul"), created=friday))
        # When
        found = {k: e for k, e in events.events(poll_result([waiting, answered, voted, wip, red]), tuesday)
                 if e["kind"] == "waiting_for_review"}
        # Then
        self.assertEqual(["1:unreviewed:1:2026-09-29"], list(found))
        self.assertEqual((2, ["Rev"]), (found["1:unreviewed:1:2026-09-29"]["working_days"],
                                        found["1:unreviewed:1:2026-09-29"]["reviewers"]))

    def test_one_working_day_is_not_waiting_yet(self):
        # Given
        c = change(current=patch_set(1, *GREEN_CI, created=local(2026, 9, 28, 17)))
        # When
        found = list(events.events(poll_result([c]), local(2026, 9, 29, 18)))
        # Then
        self.assertEqual([], found)

    def test_ready_to_submit_comes_back_each_working_day(self):
        # Given
        ps = patch_set(1, {**approval("Code-Review", 2), "grantedOn": 123}, *GREEN_CI)
        result = poll_result([change(current=ps)])
        tuesday, wednesday = local(2026, 9, 29, 10), local(2026, 9, 30, 10)
        # When
        keys = [list(dict(events.events(result, now))) for now in (tuesday, wednesday)]
        # Then
        self.assertEqual([["1:ready:1:2026-09-29"], ["1:ready:1:2026-09-30"]], keys)
        self.assertEqual(123, dict(events.events(result, tuesday))["1:ready:1:2026-09-29"]["ready_since"])


def local(year, month, day, hour):
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


class WorkingHoursTest(unittest.TestCase):
    def test_work_day_flips_in_the_morning_and_skips_weekends(self):
        # Given
        moments = {"tuesday 08:00": local(2026, 9, 29, 8), "tuesday 10:00": local(2026, 9, 29, 10),
                   "saturday": local(2026, 10, 3, 15), "monday 08:00": local(2026, 10, 5, 8),
                   "monday 09:00": local(2026, 10, 5, 9)}
        # When
        days = {name: events.work_day(now) for name, now in moments.items()}
        # Then
        self.assertEqual({"tuesday 08:00": "2026-09-28", "tuesday 10:00": "2026-09-29", "saturday": "2026-10-02",
                          "monday 08:00": "2026-10-02", "monday 09:00": "2026-10-05"}, days)

    def test_quiet_at_night_and_on_weekends(self):
        # Given
        moments = [local(2026, 9, 29, 10), local(2026, 9, 29, 19), local(2026, 9, 29, 7), local(2026, 10, 4, 11)]
        # When
        quiet = [events.is_quiet(now) for now in moments]
        # Then
        self.assertEqual([False, True, True, True], quiet)


class PendingEventsTest(unittest.TestCase):
    def test_failing_ci_is_reported_with_its_verdict(self):
        # Given
        ps = patch_set(1, approval("Build", -1, "zuul"))
        verdict = message("zuul", "Patch Set 1: Build-1\n\nBuild Failed.", timestamp=2)
        # When
        found = list(events.pending_events(poll_result([change(current=ps, comments=[verdict])])))
        # Then
        self.assertEqual([("pending", "failed", verdict["message"])],
                         [(e["kind"], e["ci"], e.get("ci_verdict")) for e in found])

    def test_quiet_change_and_past_messages_are_not_replayed(self):
        # Given
        quiet = change(comments=[message("reviewer", "Patch Set 1:\n\nold remark")], current=patch_set(1, *GREEN_CI))
        # When
        found = list(events.pending_events(poll_result([quiet])))
        # Then
        self.assertEqual([], found)

    def test_state_events_are_kept_and_reviews_i_joined_are_not_requests(self):
        # Given
        joined = review(patch_sets=[patch_set(1, approval("Code-Review", 1, gerrit.USER))])
        fresh = review(number=2)
        result = poll_result([change()], reviews=[joined, fresh], conflicts={1: ["A.kt"]})
        # When
        found = [(e["kind"], e["change"]) for e in events.pending_events(result)]
        # Then
        self.assertEqual([("merge_conflict", 1), ("review_requested", 2)], found)


def kinds(c):
    return [e["kind"] for _, e in events.review_events(c, DAY)]


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
        crowded = review(reviewers=events.MAX_REVIEWERS + 1)
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
        sets = [patch_set(1, approval("Code-Review", -1, gerrit.USER)), patch_set(2)]
        # When
        found = dict(events.review_events(review(patch_sets=sets), DAY))
        # Then
        event = found["1:review_ps:2"]
        self.assertEqual((1, {"patch_set": 1, "value": -1}), (event["since_patch_set"], event["my_last_vote"]))
        self.assertEqual(("refs/changes/01/1/1", "refs/changes/01/1/2"), (event["since_ref"], event["current_ref"]))

    def test_untouched_request_comes_back_each_working_day_joined_one_does_not(self):
        # Given
        untouched = review()
        joined = review(number=2, patch_sets=[patch_set(1, approval("Code-Review", 1, gerrit.USER))])
        # When
        keys = [k for c in (untouched, joined) for day in (DAY, "2026-09-30")
                for k, e in events.review_events(c, day) if e["kind"] == "review_requested"]
        # Then
        self.assertEqual(["1:review_requested:2026-09-29", "1:review_requested:2026-09-30",
                          "2:review_requested", "2:review_requested"], keys)

    def test_copied_vote_on_the_current_patch_set_means_nothing_to_do(self):
        # Given
        sets = [patch_set(1, approval("Code-Review", 2, gerrit.USER)),
                patch_set(2, approval("Code-Review", 2, gerrit.USER))]
        # When
        found = kinds(review(patch_sets=sets))
        # Then
        self.assertNotIn("review_new_patch_set", found)

    def test_comment_without_vote_counts_as_participation(self):
        # Given
        comments = [message(gerrit.USER, "Patch Set 1:\n\n(1 comment)", timestamp=10)]
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
                    message(gerrit.USER, "Patch Set 1:\n\n(2 comments)", 10),
                    message("owner", "Uploaded patch set 2.", 20),
                    message("owner", "Patch Set 2: Patch Set 1 was rebased", 25),
                    message("other", "Patch Set 2:\n\nLGTM", 28),
                    message("owner", "Patch Set 2:\n\n(2 comments)", 30)]
        # When
        found = dict(events.review_events(review(current=2, comments=comments), DAY))
        # Then
        self.assertEqual(["1:review_reply:30"], [k for k, e in found.items() if e["kind"] == "review_reply"])

    def test_another_reviewer_reply_counts_when_gerrit_wants_my_attention(self):
        # Given
        comments = [message(gerrit.USER, "Patch Set 1:\n\n(1 comment)", 10),
                    message("other", "Patch Set 1:\n\n(1 comment)", 20),
                    message("zuul", "Patch Set 1: Build+1", 25)]
        result = poll_result(reviews=[review(comments=comments)])
        # When
        replies = [[k for k, e in events.events({**result, "attention": attention}) if e["kind"] == "review_reply"]
                   for attention in ([], [1])]
        # Then
        self.assertEqual([[], ["1:review_reply:20"]], replies)


class BotsTest(unittest.TestCase):
    def test_bot_messages_are_not_actionable(self):
        # Given
        messages = [message("autosubmit-bot", "Hashtag added: auto-submit"),
                    {"reviewer": {"name": "Gerrit Code Review"}, "message": "Abandoned\n\nAuto-Abandoned"}]
        # When
        actionable = [events.is_actionable(m) for m in messages]
        # Then
        self.assertEqual([False, False], actionable)

    def test_bot_activity_does_not_count_as_a_review(self):
        # Given
        friday, tuesday = local(2026, 9, 25, 17), local(2026, 9, 29, 10)
        c = change(current=patch_set(1, *GREEN_CI, created=friday),
                   allReviewers=[{"username": "autosubmit-bot", "name": "autosubmit-bot"}, {"username": "r", "name": "Rev"}],
                   comments=[message("autosubmit-bot", "Hashtag added: auto-submit", timestamp=friday + 60)])
        # When
        found = [e for _, e in events.events(poll_result([c]), tuesday) if e["kind"] == "waiting_for_review"]
        # Then
        self.assertEqual([["Rev"]], [e["reviewers"] for e in found])


def holder(username, since, reason="Reviewer was added"):
    return {"username": username, "name": username.title(), "since": since, "reason": reason}


class AttentionSetTest(unittest.TestCase):
    friday, tuesday = local(2026, 9, 25, 17), local(2026, 9, 29, 10)

    def waiting(self, c, holders=(), removed=()):
        result = {**poll_result([c]), "attention_sets": {1: {"holders": list(holders), "removed": list(removed)}}}
        return [e for _, e in events.events(result, self.tuesday) if e["kind"] == "waiting_for_review"]

    def test_rest_attention_set_becomes_entries(self):
        # Given
        rest = {"1000": {"account": {"_account_id": 1000, "username": "alice", "name": "Alice"},
                         "last_update": "2026-09-29 08:00:00.000000000", "reason": "Reviewer was added"}}
        # When
        found = events.attention_entries(rest)
        # Then
        self.assertEqual([holder("alice", 1790668800.0)], found)

    def test_a_reviewer_holding_it_after_a_first_round_is_waited_on(self):
        # Given
        c = change(current=patch_set(2, approval("Code-Review", 1, "bob"), *GREEN_CI, created=self.friday),
                   comments=[message("bob", "Patch Set 2: Code-Review+1", timestamp=self.friday + 60)])
        # When
        [event] = self.waiting(c, holders=[holder("alice", self.friday)])
        # Then
        self.assertEqual((2, [{"name": "Alice", "username": "alice", "working_days": 2}], []),
                         (event["working_days"], event["waiting_on"], event["dismissed"]))

    def test_nothing_is_waited_on_while_the_ball_is_mine_or_just_handed_over(self):
        # Given
        c = change(current=patch_set(1, *GREEN_CI, created=self.friday))
        # When
        found = [self.waiting(c, holders=[holder("alice", self.friday), holder(gerrit.USER, self.friday)]),
                 self.waiting(c, holders=[holder("alice", self.tuesday - 3600)])]
        # Then
        self.assertEqual([[], []], found)

    def test_a_reviewer_who_left_it_without_a_word_saw_it_and_passed(self):
        # Given
        c = change(current=patch_set(1, *GREEN_CI, created=self.friday))
        removed = [holder("alice", self.friday + 600, "removed by alice"), holder("bob", self.friday - 600)]
        # When
        [event] = self.waiting(c, removed=removed)
        # Then
        self.assertEqual(([], [{"name": "Alice", "username": "alice", "reason": "removed by alice"}]),
                         (event["waiting_on"], event["dismissed"]))

    def test_without_the_attention_set_only_the_day_count_is_left(self):
        # Given
        c = change(current=patch_set(1, *GREEN_CI, created=self.friday))
        # When
        [event] = [e for _, e in events.events(poll_result([c]), self.tuesday) if e["kind"] == "waiting_for_review"]
        # Then
        self.assertEqual((2, None, None), (event["working_days"], event["waiting_on"], event["dismissed"]))

    def test_nudges_are_grouped_by_reviewer(self):
        # Given
        alice, bob = ({"name": n.title(), "username": n, "working_days": d} for n, d in (("alice", 3), ("bob", 2)))
        waiting = [{"kind": "waiting_for_review", "change": 1, "waiting_on": [alice]},
                   {"kind": "waiting_for_review", "change": 2, "waiting_on": [{**alice, "working_days": 5}, bob]},
                   {"kind": "waiting_for_review", "change": 3, "waiting_on": None}, {"kind": "merge_conflict", "change": 4}]
        # When
        found = events.nudges(waiting)
        # Then
        self.assertEqual([{"reviewer": "Alice", "username": "alice", "changes": [1, 2], "working_days": 5},
                          {"reviewer": "Bob", "username": "bob", "changes": [2], "working_days": 2}], found)


class StatusRowsTest(unittest.TestCase):
    def test_open_threads_keep_an_approved_change_from_being_ready(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        result = {**poll_result([change(1, current=ps), change(2, current=ps)], threads={1: [{"file": "A.kt"}]}),
                  "outdated_parents": {2: {"parent": 9}}}
        # When
        rows = events.status_rows(result, {}, {"I2": "/wt/2"}, NOW)
        # Then
        self.assertEqual([(1, False, 1, None, None), (2, True, 0, 9, "/wt/2")],
                         [(r["number"], r["ready"], r["threads"], r["outdated_parent"], r["worktree"]) for r in rows])

    def test_unknown_threads_are_none_and_block_ready(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        result = {**poll_result([change(current=ps)]), "threads_error": "offline"}
        # When
        [row] = events.status_rows(result, {}, {}, NOW)
        # Then
        self.assertEqual((None, False, 2), (row["threads"], row["ready"], row["code_review"]))


class NotificationsTest(unittest.TestCase):
    def event(self, number):
        return {"kind": "merge_conflict", "change": number, "files": ["a/A.kt"], "url": f"https://review/{number}"}

    def test_a_few_events_notify_one_by_one_and_open_their_change(self):
        # Given
        fresh = [self.event(n) for n in (1, 2, 3)] + [{"kind": "cleanup_candidate"}]
        # When
        found = events.notifications(fresh)
        # Then
        self.assertEqual([("1 has conflicts", "A.kt", "https://review/1"), ("2 has conflicts", "A.kt", "https://review/2"),
                          ("3 has conflicts", "A.kt", "https://review/3")], found)

    def test_a_burst_becomes_a_single_summary_opening_the_dashboard(self):
        # Given
        fresh = [self.event(n) for n in (1, 2, 3, 4)]
        # When
        found = events.notifications(fresh)
        # Then
        self.assertEqual([("4 Gerrit events", "1 has conflicts · 2 has conflicts · 3 has conflicts · 4 has conflicts",
                           "https://gerrit.example.com/dashboard/self")], found)

    def test_waiting_for_review(self):
        # Given
        event = {"kind": "waiting_for_review", "change": 1, "working_days": 3, "subject": "s"}
        # When
        found = events.notification(event)
        # Then
        self.assertEqual(("1 unreviewed for 3 working days", "s"), found)

    def test_waiting_on_a_reviewer(self):
        # Given
        event = {"kind": "waiting_for_review", "change": 1, "working_days": 3, "subject": "s",
                 "waiting_on": [{"name": "Alice", "username": "alice", "working_days": 3}]}
        # When
        found = events.notification(event)
        # Then
        self.assertEqual(("1 waiting on Alice for 3 working days", "s"), found)

    def test_changes_waiting_on_the_same_reviewer_notify_once(self):
        # Given
        alice = [{"name": "Alice", "username": "alice", "working_days": 2}]
        fresh = [{"kind": "waiting_for_review", "change": n, "working_days": 2, "subject": "s", "waiting_on": alice,
                  "url": f"https://review/{n}"} for n in (1, 2, 3)] + [self.event(4)]
        # When
        found = events.notifications(fresh)
        # Then
        self.assertEqual([("3 CLs waiting on Alice", "1 · 2 · 3",
                           "https://gerrit.example.com/q/owner%3Aself%20status%3Aopen%20attention%3Aalice"),
                          ("4 has conflicts", "A.kt", "https://review/4")], found)


if __name__ == "__main__":
    unittest.main()
