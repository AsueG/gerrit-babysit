"""Run from this directory: python3 -m unittest"""
import json
import os
import pathlib
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")

import ci  # noqa: E402
ZUUL = json.loads((FIXTURES / "zuul_messages.json").read_text())


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

    def test_gradle_failure_absent(self):
        # Given
        log = "2026-09-28 10:56:59.095837 | main | BUILD SUCCESSFUL"
        # When
        failure = ci.gradle_failure(log)
        # Then
        self.assertEqual("", failure)

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
        missing = ci.urllib.error.HTTPError("u", 404, "Not Found", {}, None)
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
            diagnosis = ci.diagnose_ci(event)
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
            ci.diagnose_ci(event)
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
            diagnosis = ci.diagnose_ci(event)
        # Then
        self.assertTrue(diagnosis)
        self.assertTrue(all(d["diagnosis_error"] == "offline" and d["category"] for d in diagnosis))


class BaseBuildTest(unittest.TestCase):
    def test_latest_periodic_build_of_the_branch(self):
        # Given
        build = {"result": "FAILURE", "end_time": "2026-09-29T12:11:28", "log_url": "https://logs/x/", "uuid": "u"}
        # When
        with mock.patch.object(ci, "http_get", return_value=json.dumps([build])) as get:
            found = ci.base_build("main")
        # Then
        self.assertEqual({"result": "FAILURE", "end_time": "2026-09-29T12:11:28", "log_url": "https://logs/x/"}, found)
        self.assertIn("pipeline=periodic", get.call_args.args[0])
        self.assertIn("branch=main", get.call_args.args[0])

    def test_no_build_or_offline(self):
        # Given
        answers = [mock.patch.object(ci, "http_get", return_value="[]"),
                   mock.patch.object(ci, "http_get", side_effect=OSError("offline"))]
        # When
        found = []
        for answer in answers:
            with answer:
                found.append(ci.base_build("main"))
        # Then
        self.assertEqual([None, {"error": "offline"}], found)


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
