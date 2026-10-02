"""Run from this directory: python3 -m unittest"""
import concurrent.futures
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import ci  # noqa: E402
import known_failures  # noqa: E402
ZUUL = json.loads((FIXTURES / "zuul_messages.json").read_text())


def diagnose_ci(event, flaky=None, known=None):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return [future.result() for future in ci.submit_diagnosis(pool, event, {}, flaky, known)]


class CiDiagnosisTest(unittest.TestCase):
    def test_failed_jobs_skip_success_and_canceled(self):
        # Given
        text = ZUUL["failed"]["message"]
        # When
        jobs = ci.failed_jobs(text)
        # Then
        self.assertTrue(jobs)
        self.assertTrue(all(j["result"] not in ("SUCCESS", "CANCELED") for j in jobs))
        self.assertTrue(all(j["uuid"] and j["url"].startswith("https://") for j in jobs))

    def test_gradle_failure_extracts_the_block_without_prefixes(self):
        # Given
        log = (FIXTURES / "job-output-unit-test.txt").read_text()
        # When
        failure = ci.gradle_failure(log)
        # Then
        self.assertTrue(failure.startswith("Execution failed for task"))
        self.assertIn("Unresolved reference 'InboxFilter'.", failure)
        self.assertNotIn("* Try:", failure)
        self.assertNotIn("| main |", failure)

    def test_gradle_failure_keeps_every_block_of_a_continued_build(self):
        # Given
        log = "\n".join(f"2026-09-29 10:00:00.1 | main | {line}" for line in [
            "FAILURE: Build completed with 2 failures.", "1: Task failed with an exception.", "* What went wrong:",
            "Execution failed for task ':app:compileKotlin'.", "* Try:", "> Run with --stacktrace",
            "2: Task failed with an exception.", "* What went wrong:", "Execution failed for task ':app:test'.",
            "> There were failing tests.", "* Try:", "> Run with --scan", "BUILD FAILED in 1m"])
        # When
        failure = ci.gradle_failure(log)
        # Then
        self.assertEqual("Execution failed for task ':app:compileKotlin'.\n\n"
                         "Execution failed for task ':app:test'.\n> There were failing tests.", failure)

    def test_gradle_failure_absent(self):
        # Given
        log = "2026-09-28 10:56:59.095837 | main | BUILD SUCCESSFUL"
        # When
        failure = ci.gradle_failure(log)
        # Then
        self.assertEqual("", failure)

    def test_log_tail_stops_before_the_failed_task_and_the_post_run(self):
        # Given
        log = "\n".join([f"2026-09-29 10:00:0{i}.1 | main | step {i}" for i in range(3)]
                        + ["2026-09-29 10:00:03.1 | main |", "2026-09-29 10:00:04.1 | main | npm ERR! code E401",
                           "2026-09-29 10:00:05.1 | main | ERROR", "2026-09-29 10:00:06.1 | main | {",
                           "2026-09-29 10:00:07.1 | PLAY RECAP", "2026-09-29 10:00:08.1 | post-run noise"])
        # When
        tail = ci.log_tail(log)
        # Then
        self.assertEqual("step 0\nstep 1\nstep 2\nnpm ERR! code E401", tail)

    def test_log_tail_keeps_the_last_lines_without_markers(self):
        # Given
        log = "\n".join(f"line {i}" for i in range(100))
        # When
        tail = ci.log_tail(log)
        # Then
        self.assertEqual(ci.TAIL_LINES, len(tail.splitlines()))
        self.assertTrue(tail.endswith("line 99"))

    def test_a_missing_gradle_block_falls_back_to_the_log_tail(self):
        # Given
        event = {"change": 1, "message": "Patch Set 1: Verified-1\n\n- app-e2e https://z/build/abc : FAILURE"}
        build = {"log_url": "https://logs/x/", "artifacts": []}
        answers = {"/build/": json.dumps(build), "job-output.txt": "2026-09-29 10:00:00.1 | main | emulator died",
                   "zuul-file-comments.json": "{}", "/builds?": "[]"}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        # When
        with mock.patch.object(ci, "http_get", side_effect=get):
            entry = diagnose_ci(event)[0]
        # Then
        self.assertEqual(("", "emulator died"), (entry["failure"], entry["log_tail"]))

    def test_known_failures_snippet_matches_diagnose_jobs_fingerprint(self):
        # Given: a Gradle failure and a plain log, so a recorded fix's fingerprint matches its next occurrence.
        for log in [(FIXTURES / "job-output-unit-test.txt").read_text(),
                    "2026-09-29 10:00:00.1 | main | emulator died"]:
            event = {"change": 1, "message": "Patch Set 1: Verified-1\n\n- app-e2e https://z/build/abc : FAILURE"}
            build = {"log_url": "https://logs/x/", "artifacts": []}
            answers = {"/build/": json.dumps(build), "job-output.txt": log,
                       "zuul-file-comments.json": "{}", "/builds?": "[]"}
            get = lambda url, answers=answers: next(body for marker, body in answers.items() if marker in url)
            # When
            with mock.patch.object(ci, "http_get", side_effect=get):
                entry = diagnose_ci(event)[0]
                snippet = known_failures.snippet_of("https://logs/x")
            # Then
            self.assertEqual(ci.fingerprint(entry.get("failure") or entry.get("log_tail", "")),
                             ci.fingerprint(snippet))

    def test_fingerprints_ignore_ids_hashes_and_numbers(self):
        # Given
        one = "Timeout after 300s waiting for emulator-5554\nbuild 3f9a2c1d8e failed"
        two = "timeout after 120s waiting for emulator-5556\nBuild 77aa00bb11 failed"
        # When
        prints = [ci.fingerprint(text) for text in (one, two)]
        # Then
        self.assertEqual(prints[0], prints[1])
        self.assertEqual(["build <hex> failed", "timeout after #s waiting for emulator-#"], prints[0])

    def test_similar_failures_rank_look_alikes_and_drop_strangers(self):
        # Given
        snippet = "adb: device offline\nemulator-5554 disconnected\ninstrumentation run failed"
        known = {"9:2:e2e": {"change": 9, "patch_set": 2, "job": "e2e", "cause": "emulator OOM", "fix": "bump heap",
                             "recorded_at": 1, "fingerprint": ci.fingerprint(snippet + "\nretrying")},
                 "8:1:e2e": {"change": 8, "patch_set": 1, "job": "e2e", "cause": "c", "fix": "f", "recorded_at": 1,
                             "fingerprint": ci.fingerprint("adb: device offline\nnpm ERR! code E401\nlogin failed")},
                 "7:1:e2e": {"change": 7, "patch_set": 1, "job": "e2e", "cause": "c", "fix": "f", "recorded_at": 1,
                             "fingerprint": ci.fingerprint(snippet)}}
        # When
        found = ci.similar_failures(known, snippet)
        # Then
        self.assertEqual([(7, 1.0), (9, 0.75)], [(m["change"], m["similarity"]) for m in found])
        self.assertEqual("bump heap", found[1]["fix"])

    def test_only_unknown_failures_get_look_alikes(self):
        # Given
        event = {"change": 1, "message": "Patch Set 1: Verified-1\n\n- app-e2e https://z/build/abc : FAILURE"}
        build = {"log_url": "https://logs/x/", "artifacts": []}
        known = {"k": {"change": 9, "fingerprint": ci.fingerprint("emulator died\n> There were failing tests.")}}
        logs = ["2026-09-29 10:00:00.1 | main | emulator died\n2026-09-29 10:00:00.1 | main | > There were failing tests.",
                "2026-09-29 10:00:00.1 | main | * What went wrong:\n> There were failing tests.\n* Try:"]
        found = []
        for log in logs:
            answers = {"/build/": json.dumps(build), "job-output.txt": log, "zuul-file-comments.json": "{}",
                       "/builds?": "[]"}
            get = lambda url, answers=answers: next(body for marker, body in answers.items() if marker in url)
            # When
            with mock.patch.object(ci, "http_get", side_effect=get):
                found.append(diagnose_ci(event, known=known)[0])
        # Then
        self.assertEqual([("unknown", [9]), ("unit_tests", None)],
                         [(e["category"], [m["change"] for m in e["resembles"]] if "resembles" in e else None)
                          for e in found])

    def test_lint_errors_keep_only_error_level(self):
        # Given
        location = {"physicalLocation": {"artifactLocation": {"uri": "A.kt"}, "region": {"startLine": 3}}}
        sarif = {"runs": [{"results": [
            {"ruleId": "EndOfLifeRequired", "level": "error", "locations": [location], "message": {"text": "m"}},
            {"ruleId": "DeprecatedCall", "locations": [location], "message": {"text": "w"}}]}]}
        # When
        errors = ci.lint_errors(sarif)
        # Then
        self.assertEqual([{"rule": "EndOfLifeRequired", "file": "A.kt", "line": 3, "message": "m"}], errors)

    def test_lint_errors_fall_back_to_the_rule_default_level(self):
        # Given
        location = {"physicalLocation": {"artifactLocation": {"uri": "A.kt"}, "region": {"startLine": 84}}}
        rules = [{"id": "PriceFormat", "defaultConfiguration": {"level": "error"}},
                 {"id": "Named", "defaultConfiguration": {"level": "note"}}]
        sarif = {"runs": [{"tool": {"driver": {"rules": rules}}, "results": [
            {"ruleId": "PriceFormat", "locations": [location], "message": {"text": "m"}},
            {"ruleId": "Named", "locations": [location], "message": {"text": "n"}},
            {"ruleId": "PriceFormat", "level": "warning", "locations": [location], "message": {"text": "w"}}]}]}
        # When
        errors = ci.lint_errors(sarif)
        # Then
        self.assertEqual([("PriceFormat", "m")], [(e["rule"], e["message"]) for e in errors])

    def test_lint_errors_keep_locationless_results(self):
        # Given
        sarif = {"runs": [{"results": [
            {"ruleId": "UnusedResources", "level": "error", "locations": [], "message": {"text": "m"}}]}]}
        # When
        errors = ci.lint_errors(sarif)
        # Then
        self.assertEqual([{"rule": "UnusedResources", "file": None, "line": None, "message": "m"}], errors)

    def test_categories(self):
        # Given
        cases = {
            "screenshots": ("app-unit-test", ZUUL["screenshots"]["message"], "POST_FAILURE", "", ()),
            "dependency_guard": ("app-dependency-guard", "", "FAILURE", "", ()),
            "lint": ("app-lint", "", "FAILURE", "", [{"rule": "JUnitAssertionsUsage"}]),
            "compile": ("app-build", "", "FAILURE", "> Kotlin compiler: UNRESOLVED_REFERENCE", ()),
            "unit_tests": ("app-unit-test", "", "FAILURE", "> There were failing tests. See", ()),
            "infra": ("app-unit-test", "", "TIMED_OUT", "", ()),
            "unknown": ("app-e2e", "", "FAILURE", "something else", ()),
        }
        # When
        found = {expected: ci.categorize(*args) for expected, args in cases.items()}
        # Then
        self.assertEqual({k: k for k in cases}, found)

    def test_screenshot_regression_only_tags_the_job_that_regressed(self):
        # Given
        verdict = ZUUL["screenshots"]["message"]
        # When
        categories = [ci.categorize(job, verdict, "FAILURE", "> Kotlin compiler: X", ())
                      for job in ("app-unit-test", "app-build")]
        # Then
        self.assertEqual(["screenshots", "compile"], categories)

    def test_file_comments_keep_errors_and_untagged_findings(self):
        # Given
        payload = {"A.kt": [
            {"message": "**🔴 Misleading**\n\n**Fix:** Replace with 200_00\n\n---\n> `PriceFormat` • `Error` • `C`\n> d",
             "line": 84},
            {"message": "**🔵 Named**\n\n---\n> `MissingNamedParameters` • `Note` • `Productivity`", "line": 26},
            {"message": "detekt: too long", "line": 3}]}
        # When
        found = ci.file_comments(payload)
        # Then
        self.assertEqual([("PriceFormat", 84, "**🔴 Misleading**\n\n**Fix:** Replace with 200_00"), (None, 3, "detekt: too long")],
                         [(c["rule"], c["line"], c["message"]) for c in found])

    def test_missing_file_comments_are_not_an_error(self):
        # Given
        missing = ci.urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b""))
        self.addCleanup(missing.close)
        # When
        with mock.patch.object(ci, "http_get", side_effect=missing):
            found = ci.fetch_file_comments("https://logs/x")
        # Then
        self.assertEqual([], found)

    def test_diagnose_ci_reads_one_build_history_for_both_signals(self):
        # Given
        event = {"change": 1, "message": ZUUL["failed"]["message"]}
        build = {"log_url": "https://logs/x/", "end_time": "2026-09-29T12:00:00", "artifacts": []}
        history = [{"job_name": "app-build", "result": "FAILURE", "end_time": "2026-09-29T11:00:00",
                    "ref": {"change": "2", "patchset": "1"}}]
        answers = {"/build/": json.dumps(build), "job-output.txt": "", "zuul-file-comments.json": "{}",
                   "/builds?": json.dumps(history)}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        # When
        with mock.patch.object(ci, "http_get", side_effect=get) as http:
            diagnosis = diagnose_ci(event)
        # Then
        self.assertEqual(([2], 1), (diagnosis[0]["others_failing"], diagnosis[0]["job_history"]["builds"]))
        self.assertEqual(1, sum("/builds?" in c.args[0] for c in http.call_args_list))

    def test_the_job_name_is_url_encoded(self):
        # Given
        event = {"change": 1, "message": "Patch Set 1: Verified-1\n\n- app&x https://z/build/abc : FAILURE"}
        build = {"log_url": "https://logs/x/", "artifacts": []}
        answers = {"/build/": json.dumps(build), "job-output.txt": "", "zuul-file-comments.json": "{}",
                   "/builds?": "[]"}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        # When
        with mock.patch.object(ci, "http_get", side_effect=get) as http:
            diagnose_ci(event)
        # Then
        self.assertIn("/builds?job_name=app%26x&limit=", http.call_args_list[-1].args[0])

    def test_others_failing_excludes_self_old_and_successes(self):
        # Given
        around = ci.iso_to_epoch("2026-09-29T12:00:00")
        builds = [
            {"job_name": "j", "result": "FAILURE", "end_time": "2026-09-29T11:00:00", "ref": {"change": "2"}},
            {"job_name": "j", "result": "FAILURE", "end_time": "2026-09-29T12:30:00", "ref": {"change": 3}},
            {"job_name": "j", "result": "FAILURE", "end_time": "2026-09-29T11:59:00", "ref": {"change": "1"}},
            {"job_name": "j", "result": "FAILURE", "end_time": "2026-09-28T12:00:00", "ref": {"change": "4"}},
            {"job_name": "j", "result": "SUCCESS", "end_time": "2026-09-29T12:00:00", "ref": {"change": "5"}},
            {"job_name": "j", "result": "FAILURE", "end_time": None, "ref": {"change": "6"}},
        ]
        # When
        others = ci.others_failing(builds, "j", 1, around)
        # Then
        self.assertEqual([2, 3], others)

    def test_job_history_counts_failures_and_reruns_that_went_green(self):
        # Given
        def build(change_, ps, result, end):
            return {"result": result, "end_time": f"2026-09-29T{end}:00:00", "ref": {"change": change_, "patchset": ps}}
        builds = [build(1, "1", "SUCCESS", "12"), build(1, "1", "FAILURE", "10"),
                  build(2, "3", "FAILURE", "11"), build(2, "3", "FAILURE", "13"),
                  build(3, "1", "FAILURE", "09"), build(4, "1", "SUCCESS", "09"),
                  build(5, "1", "ABORTED", "09")]
        # When
        history = ci.job_history(builds)
        # Then
        self.assertEqual({"builds": 6, "failure_rate": 0.67, "retried": 2, "retried_green": 1}, history)

    def test_job_history_without_builds(self):
        # Given
        builds = []
        # When
        history = ci.job_history(builds)
        # Then
        self.assertEqual({"builds": 0, "failure_rate": None, "retried": 0, "retried_green": 0}, history)

    def test_diagnose_ci_survives_network_errors(self):
        # Given
        event = {"change": 1, "message": ZUUL["failed"]["message"]}
        # When
        with mock.patch.object(ci, "http_get", side_effect=OSError("offline")):
            diagnosis = diagnose_ci(event)
        # Then
        self.assertTrue(diagnosis)
        self.assertTrue(all(d["diagnosis_error"] == "offline" and d["category"] for d in diagnosis))

    def test_an_unexplained_failure_of_a_job_that_flaked_here_is_flaky(self):
        # Given
        event = {"change": 1, "message": ZUUL["failed"]["message"]}
        often, once = {"app-build": {"week": 1, "month": 2, "last": 1}}, {"app-build": {"week": 1, "month": 1, "last": 1}}
        # When
        with mock.patch.object(ci, "http_get", side_effect=OSError("offline")):
            found = [diagnose_ci(event, flaky=flaky)[0] for flaky in (often, once)]
        # Then
        self.assertEqual(["flaky", "unknown"], [d["category"] for d in found])
        self.assertEqual(often["app-build"], found[0]["flaky_here"])


def verdict(ps, timestamp, **jobs):
    lines = "\n".join(f"- {job} https://zuul/build/{job}{timestamp} : {result}" for job, result in jobs.items())
    return {"reviewer": {"username": "zuul"}, "timestamp": timestamp, "message": f"Patch Set {ps}: Build-1\n\n{lines}"}


class FlakyRunsTest(unittest.TestCase):
    def test_a_job_red_then_green_on_the_same_patch_set_flaked(self):
        # Given
        comments = [verdict(1, 20, unit="SUCCESS", lint="SUCCESS"), verdict(1, 10, unit="FAILURE", lint="SUCCESS")]
        # When
        found = dict(ci.flaky_runs({"number": 7, "comments": comments}))
        # Then
        self.assertEqual({"7:1:unit:20": {"job": "unit", "change": 7, "patch_set": 1, "result": "FAILURE",
                                          "failed_at": 10, "passed_at": 20}}, found)

    def test_green_on_a_new_patch_set_was_a_fix_not_a_flake(self):
        # Given
        comments = [verdict(1, 10, unit="FAILURE"), verdict(2, 20, unit="SUCCESS"),
                    {"reviewer": {"username": "someone"}, "timestamp": 30, "message": "Patch Set 2:\n\n- unit x : SUCCESS"}]
        # When
        found = list(ci.flaky_runs({"number": 7, "comments": comments}))
        # Then
        self.assertEqual([], found)

    def test_counts_over_the_week_and_the_month(self):
        # Given
        now = 100 * 86400
        records = {str(i): {"job": job, "passed_at": now - days * 86400}
                   for i, (job, days) in enumerate([("unit", 1), ("unit", 10), ("unit", 40), ("lint", 1)])}
        # When
        counts = ci.flaky_counts(records, ["unit", "e2e"], now)
        # Then
        self.assertEqual({"unit": {"week": 1, "month": 2, "last": now - 86400}}, counts)


def periodic(result, end_time, log_url="https://logs/x/"):
    return {"result": result, "end_time": end_time, "log_url": log_url, "uuid": "u"}


class BaseHealthTest(unittest.TestCase):
    def test_a_red_streak_starts_at_its_first_failure_after_the_last_green(self):
        # Given
        builds = [periodic("FAILURE", "2026-09-29T12:00:00"), periodic("CANCELED", "2026-09-29T11:00:00"),
                  periodic("FAILURE", "2026-09-29T10:00:00"), periodic("SUCCESS", "2026-09-29T09:00:00"),
                  periodic("FAILURE", "2026-09-29T08:00:00")]
        # When
        with mock.patch.object(ci, "http_get", return_value=json.dumps(builds)) as get:
            found = ci.base_health("main")
        # Then
        self.assertEqual({"result": "FAILURE", "end_time": "2026-09-29T12:00:00", "log_url": "https://logs/x/",
                          "red_since": "2026-09-29T10:00:00", "failures": 2, "last_green": "2026-09-29T09:00:00"},
                         found)
        self.assertIn("pipeline=periodic", get.call_args.args[0])
        self.assertIn("branch=main", get.call_args.args[0])
        self.assertIn(f"limit={ci.BASE_HISTORY_LIMIT}", get.call_args.args[0])

    def test_a_streak_longer_than_the_window_has_no_last_green(self):
        # Given
        builds = [periodic("FAILURE", "2026-09-29T10:00:00"), periodic("FAILURE", "2026-09-29T12:00:00")]
        # When
        found = ci.red_streak(builds)
        # Then
        self.assertEqual(("2026-09-29T10:00:00", 2, None), (found["red_since"], found["failures"], found["last_green"]))
        self.assertEqual(5 * 3600, ci.red_for_s(found, ci.iso_to_epoch("2026-09-29T15:00:00")))

    def test_a_green_base_carries_no_streak(self):
        # Given
        builds = [periodic("SUCCESS", "2026-09-29T12:00:00"), periodic("FAILURE", "2026-09-29T10:00:00")]
        # When
        found = ci.red_streak(builds)
        # Then
        self.assertEqual({"result": "SUCCESS", "end_time": "2026-09-29T12:00:00", "log_url": "https://logs/x/"}, found)
        self.assertFalse(ci.is_red(found))

    def test_no_build_or_offline(self):
        # Given
        answers = [mock.patch.object(ci, "http_get", return_value="[]"),
                   mock.patch.object(ci, "http_get", side_effect=OSError("offline"))]
        # When
        found = []
        for answer in answers:
            with answer:
                found.append(ci.base_health("main"))
        # Then
        self.assertEqual([None, {"error": "offline"}], found)
        self.assertFalse(ci.is_red(found[1]))


class ZuulQueueTest(unittest.TestCase):
    def test_queued_patch_set_lists_waiting_and_running_jobs(self):
        # Given
        item = {"enqueue_time": 1790687541256, "remaining_time": 343846, "jobs": [
            {"name": "lint", "pipeline": "check-quality", "start_time": 1.0, "result": None},
            {"name": "lava", "pipeline": "check-quality", "start_time": 1.0, "result": "SUCCESS"},
            {"name": "unit", "pipeline": "check-quality", "start_time": None, "result": None}]}
        # When
        with mock.patch.object(ci, "http_get", return_value=json.dumps([item])) as get:
            queue = ci.zuul_queue(1, 2)
        # Then
        self.assertEqual([{"pipeline": "check-quality", "enqueued_at": 1790687541, "remaining_s": 343,
                           "jobs_waiting": ["unit"], "jobs_running": ["lint"]}], queue)
        self.assertTrue(get.call_args.args[0].endswith("/status/change/1,2"))

    def test_unknown_to_zuul_or_offline(self):
        # Given
        answers = [mock.patch.object(ci, "http_get", return_value="[]"),
                   mock.patch.object(ci, "http_get", side_effect=OSError("offline"))]
        # When
        found = []
        for answer in answers:
            with answer:
                found.append(ci.zuul_queue(1, 2))
        # Then
        self.assertEqual([[], {"error": "offline"}], found)


if __name__ == "__main__":
    unittest.main()
