"""Zuul failure diagnosis: what broke, where, and whether it looks flaky. Network-bound, only for fresh failures."""
import calendar
import concurrent.futures
import gzip
import itertools
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import CONFIG

ZUUL_API = CONFIG["zuul_api"]
INFRA_RESULTS = {"POST_FAILURE", "TIMED_OUT", "NODE_FAILURE", "RETRY_LIMIT", "ERROR", "DISK_FULL"}
FLAKY_WINDOW_S = 3 * 3600
PERIODIC_BUILD = CONFIG["periodic_build"]
SCREENSHOT_MARKER = CONFIG["screenshot_regression_marker"]
JOB_HISTORY_LIMIT = 200
BASE_HISTORY_LIMIT = 20
CI_USER = CONFIG["ci_user"]
# Local flakes (failed, then green on the same patch set) over 30 days that make a failure look flaky.
FLAKY_MIN = 2
WEEK_S = 7 * 86400
MONTH_S = 30 * 86400
NOT_FAILED = ("SUCCESS", "CANCELED", "SKIPPED")
RECHECK = re.compile(r"recheck(?:-[\w-]+)?")


ZUUL_JOB_LINE = re.compile(r"^- (\S+) (https://\S+/build/(\w+)) : (\w+)", re.MULTILINE)
LOG_PREFIX = re.compile(r"^\S+ \S+ \| \w+ \| ?")
VERDICT_PATCH_SET = re.compile(r"^Patch Set (\d+):")


def failed_jobs(message):
    return [{"job": job, "url": url, "uuid": uuid, "result": result}
            for job, url, uuid, result in ZUUL_JOB_LINE.findall(message) if result not in NOT_FAILED]


def flaky_runs(change):
    """(key, record) per job that failed, then went green on the same patch set: nothing was pushed in between."""
    verdicts = {}
    for message in sorted(change.get("comments", []), key=lambda m: m["timestamp"]):
        match = VERDICT_PATCH_SET.match(message.get("message", ""))
        if match and message["reviewer"].get("username") == CI_USER:
            verdicts.setdefault(int(match.group(1)), []).append(message)
    for patch_set, messages in verdicts.items():
        failed = {}
        for message in messages:
            for job, _, _, result in ZUUL_JOB_LINE.findall(message["message"]):
                if result == "SUCCESS" and job in failed:
                    failed_at, failed_result = failed.pop(job)
                    yield (f"{change['number']}:{patch_set}:{job}:{message['timestamp']}",
                           {"job": job, "change": change["number"], "patch_set": patch_set, "result": failed_result,
                            "failed_at": failed_at, "passed_at": message["timestamp"]})
                elif result not in NOT_FAILED:
                    failed[job] = (message["timestamp"], result)


def flaky_counts(records, jobs, now=None):
    """{job: {week, month, last}} for the given jobs that flaked here in the last 30 days."""
    now = now or time.time()
    counts = {}
    for job in jobs:
        stamps = [r["passed_at"] for r in records.values() if r["job"] == job and now - r["passed_at"] < MONTH_S]
        if stamps:
            counts[job] = {"week": sum(now - s < WEEK_S for s in stamps), "month": len(stamps), "last": max(stamps)}
    return counts


def gradle_failure(log):
    """Gradle's `* What went wrong:` block(s), without zuul's timestamp prefixes."""
    lines = [LOG_PREFIX.sub("", line) for line in log.splitlines()]
    if "* What went wrong:" not in lines:
        return ""
    block = []
    for line in lines[lines.index("* What went wrong:") + 1:]:
        if line.startswith(("* Try:", "BUILD FAILED")):
            break
        block.append(line)
    return "\n".join(block).strip()[:3000]


TAIL_LINES = 40


def log_tail(log):
    """Without a Gradle block: the lines leading up to the first failed zuul task (its bare `ERROR` line), else to the
    first PLAY RECAP, else the end of the log. The post-run playbooks that follow are the same for every failure."""
    lines = log.splitlines()
    end = next((i for i, line in enumerate(lines) if "PLAY RECAP" in line
                or (LOG_PREFIX.match(line) and LOG_PREFIX.sub("", line).strip() == "ERROR")), len(lines))
    kept = [stripped for line in lines[:end] if (stripped := LOG_PREFIX.sub("", line).rstrip()).strip()]
    return "\n".join(kept[-TAIL_LINES:])[-3000:]


# Build ids, hashes, durations, ports and line numbers differ between two runs of the same breakage.
FINGERPRINT_NOISE = [(re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
                     (re.compile(r"\b[0-9a-f]{7,64}\b"), "<hex>"),
                     (re.compile(r"\d+"), "#")]
# Below this Jaccard index two fingerprints share too little for the older fix to be worth a look.
SIMILAR_MIN = 0.5
SIMILAR_MAX = 3


def fingerprint(snippet):
    """Sorted, normalized, distinct lines of a failure snippet."""
    lines = set()
    for line in snippet.lower().splitlines():
        line = LOG_PREFIX.sub("", line)
        for pattern, placeholder in FINGERPRINT_NOISE:
            line = pattern.sub(placeholder, line)
        line = " ".join(line.split())
        if len(line) > 3:
            lines.add(line)
    return sorted(lines)


def similar_failures(known, snippet):
    """Failures already diagnosed by hand whose fingerprint looks like this snippet's, closest first. A lead, not a
    verdict: a brand-new failure can share boilerplate lines with an old one."""
    mine = set(fingerprint(snippet))
    if not mine:
        return []
    matches = []
    for record in known.values():
        theirs = set(record.get("fingerprint", ()))
        score = len(mine & theirs) / len(mine | theirs) if theirs else 0
        if score >= SIMILAR_MIN:
            matches.append({"similarity": round(score, 2),
                            **{k: record.get(k) for k in ("change", "patch_set", "job", "cause", "fix", "recorded_at")}})
    return sorted(matches, key=lambda m: -m["similarity"])[:SIMILAR_MAX]


def lint_errors(sarif):
    errors = []
    for run in sarif.get("runs", []):
        # Android Lint leaves `level` off its results: the severity is the rule's default configuration.
        defaults = {rule["id"]: rule.get("defaultConfiguration", {}).get("level")
                    for rule in run.get("tool", {}).get("driver", {}).get("rules", [])}
        errors += [{"rule": r["ruleId"],
                    "file": r["locations"][0]["physicalLocation"]["artifactLocation"].get("uri"),
                    "line": r["locations"][0]["physicalLocation"].get("region", {}).get("startLine"),
                    "message": r["message"]["text"][:300]}
                   for r in run.get("results", []) if (r.get("level") or defaults.get(r["ruleId"])) == "error"]
    return errors


def has_screenshot_regression(job, message):
    """The verdict lists every job; only the regression line's report link names the job that regressed."""
    return bool(SCREENSHOT_MARKER) and any(f"/{job}/" in line for line in message.splitlines()
                                           if SCREENSHOT_MARKER in line)


FILE_COMMENT_TAGS = re.compile(r"^> `(\w+)` • `(\w+)`", re.MULTILINE)


def file_comments(payload):
    """zuul-file-comments.json (the robot comments zuul posts inline, with their **Fix:** hint), errors only."""
    found = []
    for file, comments in payload.items():
        for comment in comments:
            text = comment.get("message", "")
            tags = FILE_COMMENT_TAGS.search(text)
            rule, level = tags.groups() if tags else (None, None)
            # Untagged comments come from other analyzers whose severity we can't read: keep them.
            if level not in (None, "Error", "Fatal"):
                continue
            found.append({"file": file, "line": comment.get("line"), "rule": rule,
                          "message": text.split("\n---\n", 1)[0].strip()[:500]})
    return found[:30]


def fetch_file_comments(log_url):
    try:
        return file_comments(json.loads(http_get(f"{log_url}/zuul-file-comments.json")))
    except urllib.error.HTTPError as error:
        # Jobs without inline findings publish no file at all.
        if error.code == 404:
            return []
        raise


def categorize(job, message, result, failure="", errors=()):
    if has_screenshot_regression(job, message):
        return "screenshots"
    if "dependency-guard" in job:
        return "dependency_guard"
    if errors:
        return "lint"
    if "Kotlin compiler" in failure or re.search(r"compile\w*Kotlin", failure):
        return "compile"
    if "failing tests" in failure or re.search(r"\d+ tests? completed, \d+ failed", failure):
        return "unit_tests"
    if result in INFRA_RESULTS:
        return "infra"
    return "unknown"


def others_failing(builds, job, change, around):
    """Other changes this job failed on around the same time: several of them hint at a flaky job or a red base."""
    return sorted({int(b["ref"]["change"]) for b in builds
                   if b.get("job_name", job) == job and b.get("result") == "FAILURE"
                   and b.get("ref", {}).get("change") and str(b["ref"]["change"]) != str(change)
                   and b.get("end_time") and abs(around - iso_to_epoch(b["end_time"])) < FLAKY_WINDOW_S})


def job_history(builds):
    """The job's recent track record: a high failure rate, or failures that went green when rerun, point to flakiness."""
    finished = sorted((b for b in builds if b.get("result") in ("SUCCESS", "FAILURE")),
                      key=lambda b: b.get("end_time") or "")
    runs = {}
    for build in finished:
        ref = build.get("ref", {})
        runs.setdefault((ref.get("change"), ref.get("patchset")), []).append(build["result"])
    retried = [results for results in runs.values() if "FAILURE" in results[:-1]]
    failures = sum(b["result"] == "FAILURE" for b in finished)
    return {"builds": len(finished), "failure_rate": round(failures / len(finished), 2) if finished else None,
            "retried": len(retried), "retried_green": sum(results[-1] == "SUCCESS" for results in retried)}


def iso_to_epoch(stamp):
    """Zuul timestamps are UTC without a suffix."""
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))


def http_get(url):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
    return body.decode(errors="replace")


def diagnose_ci(event, histories=None, flaky=None, known=None):
    """Network-bound: only for fresh zuul failures, so the idle loop stays free. The failed jobs are diagnosed side by
    side, each one being a handful of downloads.

    `histories` is shared across the events of one wake-up, see job_builds(). `flaky` ({job: counts}) is the local
    flake memory, see flaky_counts(). `known` is the memory of failures diagnosed by hand, see similar_failures()."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return [future.result() for future in
                submit_diagnosis(pool, event, {} if histories is None else histories, flaky, known)]


def submit_diagnosis(pool, event, histories, flaky=None, known=None):
    """One future per failed job, in the verdict's order: several events can share one pool.

    No deadlock on a shared pool: a job only waits in job_builds() for a download already running."""
    return [pool.submit(diagnose_job, job, event, histories, flaky or {}, known or {})
            for job in failed_jobs(event["message"])]


_histories_lock = threading.Lock()


def job_builds(job, histories):
    """The job's last builds; `histories` ({job: future}) is shared by threads, so a job red on several changes is
    fetched once while the others wait for it. One call covers both signals: 200 builds of a job span well over the
    ±3 h flaky window."""
    with _histories_lock:
        owner = job not in histories
        if owner:
            histories[job] = concurrent.futures.Future()
    if owner:
        try:
            histories[job].set_result(json.loads(
                http_get(f"{ZUUL_API}/builds?" + urllib.parse.urlencode({"job_name": job, "limit": JOB_HISTORY_LIMIT}))))
        except BaseException as error:
            histories[job].set_exception(error)
            raise
    return histories[job].result()


def diagnose_job(job, event, histories, flaky, known):
    entry = {"job": job["job"], "result": job["result"], "url": job["url"]}
    try:
        build = json.loads(http_get(f"{ZUUL_API}/build/{job['uuid']}"))
        log_url = build["log_url"].rstrip("/")
        entry["log_url"] = log_url
        log = http_get(f"{log_url}/job-output.txt")
        entry["failure"] = gradle_failure(log)
        if not entry["failure"]:
            entry["log_tail"] = log_tail(log)
        sarif = next((a["url"] for a in build.get("artifacts", []) if a["name"].endswith("lint.sarif")), None)
        errors = lint_errors(json.loads(http_get(sarif))) if sarif else []
        if errors:
            entry["lint_errors"] = errors
        comments = fetch_file_comments(log_url)
        if comments:
            entry["file_comments"] = comments
        history = job_builds(job["job"], histories)
        around = iso_to_epoch(build["end_time"]) if build.get("end_time") else time.time()
        entry["others_failing"] = others_failing(history, job["job"], event["change"], around)
        entry["job_history"] = job_history(history)
    except (OSError, ValueError, KeyError) as error:
        entry["diagnosis_error"] = str(error)
    entry["category"] = categorize(job["job"], event["message"], job["result"], entry.get("failure", ""),
                                   entry.get("lint_errors") or entry.get("file_comments") or ())
    if entry["category"] == "unknown" and known:
        resembles = similar_failures(known, entry.get("failure") or entry.get("log_tail", ""))
        if resembles:
            entry["resembles"] = resembles
    if job["job"] in flaky:
        entry["flaky_here"] = flaky[job["job"]]
        # A known cause (compile, lint…) wins: only an unexplained failure is put down to flakiness.
        if entry["category"] in ("infra", "unknown") and flaky[job["job"]]["month"] >= FLAKY_MIN:
            entry["category"] = "flaky"
    return entry


def base_health(branch):
    """The target branch's latest periodic builds: red means the base itself is broken, so a recheck can't help."""
    query = urllib.parse.urlencode({"pipeline": PERIODIC_BUILD["pipeline"], "job_name": PERIODIC_BUILD["job"],
                                    "branch": branch, "limit": BASE_HISTORY_LIMIT})
    try:
        return red_streak(json.loads(http_get(f"{ZUUL_API}/builds?{query}")))
    except (OSError, ValueError) as error:
        return {"error": str(error)}


def red_streak(builds):
    """The latest verdict; when red, `red_since` = end of the streak's first failure and `last_green` = end of the
    green build before it, None past the history window (the streak is then at least `failures` long)."""
    finished = sorted((b for b in builds if b.get("result") in ("SUCCESS", "FAILURE") and b.get("end_time")),
                      key=lambda b: b["end_time"], reverse=True)
    if not finished:
        return None
    health = {key: finished[0].get(key) for key in ("result", "end_time", "log_url")}
    if health["result"] == "FAILURE":
        streak = list(itertools.takewhile(lambda b: b["result"] == "FAILURE", finished))
        green = finished[len(streak)] if len(streak) < len(finished) else None
        health |= {"red_since": streak[-1]["end_time"], "failures": len(streak),
                   "last_green": green["end_time"] if green else None}
    return health


def is_red(health):
    return bool(health) and health.get("result") == "FAILURE"


def red_for_s(health, now):
    return max(0, now - iso_to_epoch(health["red_since"])) if health.get("red_since") else 0


def zuul_queue(change, patch_set):
    """Where zuul has this patch set: [] means it lost track of it (recheck), otherwise it is only queued."""
    try:
        items = json.loads(http_get(f"{ZUUL_API}/status/change/{change},{patch_set}"))
    except (OSError, ValueError) as error:
        return {"error": str(error)}
    queue = []
    for item in items:
        jobs = item.get("jobs", [])
        remaining = item.get("remaining_time")
        queue.append({
            "pipeline": next((j["pipeline"] for j in jobs if j.get("pipeline")), None),
            "enqueued_at": (item.get("enqueue_time") or 0) // 1000,
            "remaining_s": remaining // 1000 if remaining is not None else None,
            "jobs_waiting": [j["name"] for j in jobs if j.get("start_time") is None],
            "jobs_running": [j["name"] for j in jobs if j.get("start_time") is not None and j.get("result") is None],
        })
    return queue
