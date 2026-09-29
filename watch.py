#!/usr/bin/env python3
"""Block until one of my open Gerrit changes gets a new actionable event, print it as JSON, exit."""
import argparse
import base64
import calendar
import datetime
import gzip
import json
import netrc
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from config import CACHE, CONFIG, REPO, gerrit_user, t

HOST = CONFIG["gerrit_host"]
PORT = str(CONFIG["ssh_port"])
USER = gerrit_user()
QUERY = "owner:self status:open"
REVIEW_QUERY = "reviewer:self status:open -owner:self"
ATTENTION_QUERY = "attention:self status:open"
# Group additions put dozens of people on a change: not a personal review request.
MAX_REVIEWERS = CONFIG["max_reviewers"]
ZUUL_API = CONFIG["zuul_api"]
INFRA_RESULTS = {"POST_FAILURE", "TIMED_OUT", "NODE_FAILURE", "RETRY_LIMIT", "ERROR", "DISK_FULL"}
FLAKY_WINDOW_S = 3 * 3600
STATE = CACHE / "seen.json"
STATUS = CACHE / "status.json"
CI_USER = CONFIG["ci_user"]
# "" = Gerrit itself (auto-abandon notices carry no username).
BOT_USERS = {CI_USER, "", *CONFIG["bot_users"]}
CI_LABELS = tuple(CONFIG["ci_labels"])
PROTECTED_BRANCHES = set(CONFIG["protected_branches"])
# Private namespace: never moves a ref another session relies on (branches, FETCH_HEAD).
FETCH_NAMESPACE = "refs/gerrit-babysit"
DAEMON_STATE = CACHE / "daemon-seen.json"
DAEMON_POLL = CACHE / "daemon-poll.json"
# A daemon loop can take interval + fetch timeout + ssh timeout before it writes again.
DAEMON_ALIVE_S = 360
SESSION = CACHE / "session.json"
MAX_THREADS = 20
REST = f"https://{HOST}/a"
PERIODIC_BUILD = CONFIG["periodic_build"]
SCREENSHOT_MARKER = CONFIG["screenshot_regression_marker"]
CI_STUCK_S = 2 * 3600
# Longer than the daemon's interval, so the session sees at least one newer poll before reporting.
SETTLE_S = 90
SEEN_RETENTION_S = 30 * 86400
WORK_HOURS = tuple(CONFIG["work_hours"])
UNREVIEWED_WORKING_DAYS = 2
# Past this many notifications in one poll (typically the morning flush), a single summary replaces them.
DIGEST_OVER = 3
JOB_HISTORY_LIMIT = 200
SWIFTBAR_PLUGIN = "gerrit"
DASHBOARD = f"https://{HOST}/dashboard/self"


def gerrit_query(query, *options):
    out = subprocess.run(
        ["ssh", "-p", PORT, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"{USER}@{HOST}",
         "gerrit", "query", "--format=JSON", *options, f"'{query}'"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [r for r in rows if r.get("type") != "stats"]


def fetch_changes():
    return gerrit_query(QUERY, "--current-patch-set", "--comments", "--dependencies", "--all-reviewers")


def fetch_attention():
    return sorted(row["number"] for row in gerrit_query(ATTENTION_QUERY))


_rest_auth = None


def http_credentials():
    """(user, HTTP password): from the Gerrit MCP server's config when set, so the token lives in one place."""
    if CONFIG["gerrit_mcp_config"]:
        hosts = json.loads(pathlib.Path(CONFIG["gerrit_mcp_config"]).expanduser().read_text())["gerrit_hosts"]
        auth = hosts[0]["authentication"]
        return auth["username"], auth["auth_token"]
    entry = netrc.netrc().authenticators(HOST)
    if not entry:
        raise KeyError(f"no ~/.netrc entry for {HOST}")
    return entry[0], entry[2]


def rest_get(path):
    global _rest_auth
    if _rest_auth is None:
        _rest_auth = base64.b64encode(":".join(http_credentials()).encode()).decode()
    request = urllib.request.Request(REST + path, headers={"Authorization": f"Basic {_rest_auth}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        # Gerrit prefixes every JSON body with )]}' against XSSI.
        return json.loads(response.read().decode().split("\n", 1)[1])


_threads_memo = {}


def open_threads(changes):
    """{change number: threads awaiting me}; the REST call only reruns when the change moves."""
    global _threads_memo
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes}
    _threads_memo = {key: _threads_memo[key] if key in _threads_memo
                     else awaiting_threads(rest_get(f"/changes/{number}/comments"))
                     for number, key in keys.items()}
    return {number: _threads_memo[key] for number, key in keys.items()}


def fetch_reviews():
    return gerrit_query(REVIEW_QUERY, "--current-patch-set", "--all-approvals", "--all-reviewers", "--comments")


def parent_statuses(changes):
    """{change number: (parent number, parent status)} — `dependsOn` lists merged parents too."""
    parent_of = {c["number"]: c["dependsOn"][0]["number"] for c in changes if c.get("dependsOn")}
    status = {c["number"]: c["status"] for c in changes}
    others = sorted(set(parent_of.values()) - status.keys())
    if others:
        query = " OR ".join(f"change:{n}" for n in others)
        status |= {row["number"]: row["status"] for row in gerrit_query(query)}
    return {number: (parent, status.get(parent)) for number, parent in parent_of.items()}


def stale_parents(changes, statuses):
    """{change number: {parent, old_parent_sha}} when the parent merged under another SHA (rebase on submit).

    Must run after merge_conflicts(), which fetches the patch sets and branch tips it relies on."""
    stale = {}
    for change in changes:
        parent, status = statuses.get(change["number"], (None, None))
        base = change.get("currentPatchSet", {}).get("parents", [None])[0]
        if status != "MERGED" or not base:
            continue
        target = f"{FETCH_NAMESPACE}/{change['branch']}"
        if git_run("merge-base", "--is-ancestor", base, target).returncode == 1:
            stale[change["number"]] = {"parent": parent, "old_parent_sha": base}
    return stale


def poll():
    changes = fetch_changes()
    statuses = parent_statuses(changes)
    conflicts = merge_conflicts(changes)
    # REST relies on the HTTP password, SSH does not: an expired token must not blind the whole watcher.
    threads, threads_error = {}, None
    try:
        threads = open_threads(changes)
    except (OSError, ValueError, KeyError) as error:
        threads_error = error_detail(error)
    return {
        "changes": changes,
        "conflicts": conflicts,
        "parents": {n: parent for n, (parent, status) in statuses.items() if status == "NEW"},
        "stale_parents": stale_parents(changes, statuses),
        "threads": threads,
        "threads_error": threads_error,
        "reviews": fetch_reviews(),
        "attention": fetch_attention(),
    }


def with_int_keys(result):
    """JSON turns the change-number keys into strings; defaults cover a daemon still running older code."""
    return {**result, "reviews": result.get("reviews", []), "attention": result.get("attention", []),
            **{key: {int(k): v for k, v in result.get(key, {}).items()}
               for key in ("conflicts", "parents", "stale_parents", "threads")}}


class DaemonError(Exception):
    pass


def error_detail(error):
    """ssh's own stderr ('Could not resolve hostname…') says far more than 'exit status 255'."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    last_line = stderr.strip().splitlines()[-1] if stderr and stderr.strip() else ""
    return last_line or str(error)


def daemon_poll():
    """The daemon's latest poll when it is alive, so a session never hits Gerrit or the fetch refs a second time."""
    if not DAEMON_POLL.exists():
        return None
    heartbeat = json.loads(DAEMON_POLL.read_text())
    if time.time() - heartbeat["attempt"] > DAEMON_ALIVE_S:
        return None
    if heartbeat["error"]:
        raise DaemonError(heartbeat["error"])
    return with_int_keys(heartbeat["poll"])


def write_daemon_poll(result=None, error=None):
    previous = json.loads(DAEMON_POLL.read_text()) if DAEMON_POLL.exists() else {}
    atomic_write(DAEMON_POLL, {"attempt": time.time(), "error": error,
                               "poll": result if error is None else previous.get("poll")})


def atomic_write(path, payload):
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(path)


def git_run(*args, cwd=None, timeout=60):
    return subprocess.run(["git", "-C", str(cwd or REPO), *args], capture_output=True, text=True, timeout=timeout)


def git(*args, cwd=None):
    return git_run(*args, cwd=cwd).stdout


_conflicts_memo = {}


def merge_conflicts(changes):
    """{change number: conflicting files} — `is:mergeable` is often disabled server-side, so merge locally."""
    candidates = [(c, c["currentPatchSet"]) for c in changes if c.get("currentPatchSet", {}).get("revision")]
    if not candidates:
        return {}
    refspecs = {f"+refs/heads/{c['branch']}:{FETCH_NAMESPACE}/{c['branch']}" for c, _ in candidates}
    refspecs |= {f"+{ps['ref']}:{FETCH_NAMESPACE}/changes/{c['number']}"
                 for c, ps in candidates if git_run("cat-file", "-e", ps["revision"]).returncode != 0}
    subprocess.run(["git", "-C", str(REPO), "fetch", "--quiet", "--no-write-fetch-head", "origin", *sorted(refspecs)],
                   capture_output=True, text=True, timeout=180, check=True)

    conflicts = {}
    for change, patch_set in candidates:
        target = git("rev-parse", f"{FETCH_NAMESPACE}/{change['branch']}").strip()
        key = (target, patch_set["revision"])
        if key not in _conflicts_memo:
            # A 3-way merge of the whole patch set onto the branch tip: conflicts iff Gerrit's rebase-on-submit would.
            result = git_run("merge-tree", "--write-tree", "--name-only", "--no-messages", target, patch_set["revision"])
            _conflicts_memo[key] = result.stdout.splitlines()[1:] if result.returncode == 1 else []
        if _conflicts_memo[key]:
            conflicts[change["number"]] = _conflicts_memo[key]
    return conflicts


def change_id_of(sha):
    return git("log", "-1", "--format=%(trailers:key=Change-Id,valueonly)", sha).strip()


_cleanup_memo = {}


def cleanup_candidates(open_ids=frozenset()):
    """Read-only: local branches (and their worktree) whose tip is a pushed patch set of a merged CL of mine.

    Branches of still-open CLs are skipped, so the Gerrit query only reruns when a CL leaves `open_ids` or a
    branch moves."""
    blocks = [dict(line.partition(" ")[::2] for line in block.splitlines())
              for block in git("worktree", "list", "--porcelain").split("\n\n") if block.strip()]
    main_branch = blocks[0].get("branch", "").removeprefix("refs/heads/") if blocks else ""
    worktree_of = {b["branch"].removeprefix("refs/heads/"): b for b in blocks[1:] if "branch" in b}

    branches = {}
    for line in git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines():
        branch, sha = line.split(" ")
        if branch in PROTECTED_BRANCHES or branch == main_branch:
            continue
        worktree = worktree_of.get(branch)
        if worktree and ("locked" in worktree or git("status", "--porcelain", cwd=worktree["worktree"]).strip()):
            continue
        change_id = change_id_of(sha)
        if change_id and change_id not in open_ids:
            branches[branch] = (sha, change_id, worktree["worktree"] if worktree else None)
    if not branches:
        return []
    key = frozenset(branches.items())
    if key not in _cleanup_memo:
        _cleanup_memo.clear()
        _cleanup_memo[key] = list(merged_branches(branches))
    return _cleanup_memo[key]


def merged_branches(branches):
    ids = " OR ".join(f"change:{cid}" for cid in sorted({cid for _, cid, _ in branches.values()}))
    merged = {
        row["id"]: ({ps["revision"] for ps in row.get("patchSets", [])}, row["number"], row["subject"])
        for row in gerrit_query(f"owner:self status:merged ({ids})", "--patch-sets")
    }
    for branch, (sha, change_id, path) in branches.items():
        revisions, number, subject = merged.get(change_id, (set(), None, None))
        # Exact pushed SHA only: a local amend made after the merge is not a candidate.
        if sha in revisions:
            yield f"cleanup:{branch}:{sha}", {"kind": "cleanup_candidate", "branch": branch, "worktree": path,
                                               "change": number, "subject": subject}


CI_NEGATIVE_VOTE = re.compile(rf"\b(?:{'|'.join(CI_LABELS)})-[12]\b")
POSITIVE_VOTES_ONLY = re.compile(r"^Patch Set \d+:(?: [\w-]+\+[12])+$")


def is_actionable(message):
    author = message.get("reviewer", {}).get("username", "")
    if author == USER or (author in BOT_USERS and author != CI_USER):
        return False
    header, _, body = message.get("message", "").partition("\n")
    if author == CI_USER:
        # Only the header line carries zuul's votes ("Patch Set 5: Build-1"); job names and URLs below can contain "-1".
        return bool(CI_NEGATIVE_VOTE.search(header))
    # A bare +1/+2 needs nothing; a +2 that unblocks the CL comes back as ready_to_submit.
    return not (POSITIVE_VOTES_ONLY.match(header.strip()) and not body.strip())


def is_ready_to_submit(patch_set):
    # submitRecords can report OK on a change voted -1 (seen on Gerrit 3.x), so read the votes instead.
    votes = votes_of(patch_set)
    code_review = votes.get("Code-Review", [])
    return (
        2 in code_review
        and min(code_review) >= 0
        and all(1 in votes.get(label, []) and min(votes[label]) >= 0 for label in CI_LABELS)
    )


def votes_of(patch_set):
    votes = {}
    for approval in patch_set.get("approvals", []):
        votes.setdefault(approval["type"], []).append(int(approval["value"]))
    return votes


def failed_ci_labels(votes):
    return [label for label in CI_LABELS if min(votes.get(label, [0])) < 0]


def latest_ci_verdict(change, patch_set):
    prefix = f"Patch Set {patch_set.get('number')}:"
    verdicts = [m for m in change.get("comments", [])
                if m["reviewer"].get("username") == CI_USER and m["message"].startswith(prefix)]
    return max(verdicts, key=lambda m: m["timestamp"], default=None)


def ci_idle_since(change, patch_set):
    """Zuul's last sign of life on this patch set (a recheck's "Starting" counts), else the upload."""
    prefix = f"Patch Set {patch_set.get('number')}:"
    stamps = [m["timestamp"] for m in change.get("comments", [])
              if m["reviewer"].get("username") == CI_USER and m["message"].startswith(prefix)]
    return max(stamps, default=patch_set.get("createdOn", 0))


def is_ci_stuck(change, patch_set, now):
    return (ci_state(change, patch_set, votes_of(patch_set)) == "running"
            and now - ci_idle_since(change, patch_set) > CI_STUCK_S)


def is_merge_failed(change, patch_set):
    """Zuul's latest verdict on this patch set is 'Merge Failed.': no job ran, the base is stale."""
    verdict = latest_ci_verdict(change, patch_set)
    return verdict is not None and "Merge Failed." in verdict["message"]


ZUUL_JOB_LINE = re.compile(r"^- (\S+) (https://\S+/build/(\w+)) : (\w+)", re.MULTILINE)
LOG_PREFIX = re.compile(r"^\S+ \S+ \| \w+ \| ?")


def failed_jobs(message):
    return [{"job": job, "url": url, "uuid": uuid, "result": result}
            for job, url, uuid, result in ZUUL_JOB_LINE.findall(message)
            if result not in ("SUCCESS", "CANCELED", "SKIPPED")]


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


RECHECK = re.compile(r"^recheck(?:-[\w-]+)?$")


def my_rechecks(change, patch_set, before):
    """How many times I already asked zuul to rerun this patch set before the given verdict."""
    prefix = f"Patch Set {patch_set.get('number')}:"
    return sum(1 for m in change.get("comments", [])
               if m["reviewer"].get("username") == USER and m["timestamp"] < before
               and m["message"].startswith(prefix) and RECHECK.match(m["message"].partition("\n")[2].strip()))


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


def diagnose_ci(event):
    """Network-bound: only for fresh zuul failures, so the idle loop stays free."""
    diagnosis = []
    for job in failed_jobs(event["message"]):
        entry = {"job": job["job"], "result": job["result"], "url": job["url"]}
        try:
            build = json.loads(http_get(f"{ZUUL_API}/build/{job['uuid']}"))
            log_url = build["log_url"].rstrip("/")
            entry["log_url"] = log_url
            entry["failure"] = gradle_failure(http_get(f"{log_url}/job-output.txt"))
            sarif = next((a["url"] for a in build.get("artifacts", []) if a["name"].endswith("lint.sarif")), None)
            errors = lint_errors(json.loads(http_get(sarif))) if sarif else []
            if errors:
                entry["lint_errors"] = errors
            comments = fetch_file_comments(log_url)
            if comments:
                entry["file_comments"] = comments
            # One call covers both: 200 builds of a job span well over the ±3 h flaky window.
            history = json.loads(http_get(f"{ZUUL_API}/builds?job_name={job['job']}&limit={JOB_HISTORY_LIMIT}"))
            around = iso_to_epoch(build["end_time"]) if build.get("end_time") else time.time()
            entry["others_failing"] = others_failing(history, job["job"], event["change"], around)
            entry["job_history"] = job_history(history)
        except (OSError, ValueError, KeyError) as error:
            entry["diagnosis_error"] = str(error)
        entry["category"] = categorize(job["job"], event["message"], job["result"], entry.get("failure", ""),
                                       entry.get("lint_errors") or entry.get("file_comments") or ())
        diagnosis.append(entry)
    return diagnosis


def base_build(branch):
    """Latest periodic build of the target branch: red means the base itself is broken, so a recheck can't help."""
    query = urllib.parse.urlencode({"pipeline": PERIODIC_BUILD["pipeline"], "job_name": PERIODIC_BUILD["job"],
                                    "branch": branch, "limit": 1})
    try:
        builds = json.loads(http_get(f"{ZUUL_API}/builds?{query}"))
    except (OSError, ValueError) as error:
        return {"error": str(error)}
    if not builds:
        return None
    return {key: builds[0].get(key) for key in ("result", "end_time", "log_url")}


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


def prereview(event):
    path = CACHE / "prereview" / f"{event['change']}-{event['patch_set']}.md"
    return {"path": str(path), "done": path.exists()}


def interdiff(event):
    """What moved since the patch set I last saw; the diff is limited to the change's files to keep rebase noise out."""
    refs = {ref: f"{FETCH_NAMESPACE}/review/{event['change']}/{ref.rsplit('/', 1)[1]}"
            for ref in (event["since_ref"], event["current_ref"])}
    fetch = git_run("fetch", "--quiet", "--no-write-fetch-head", "origin",
                    *(f"+{ref}:{local}" for ref, local in refs.items()), timeout=180)
    if fetch.returncode:
        return {"error": error_detail(fetch)}
    old, new = refs[event["since_ref"]], refs[event["current_ref"]]
    files = sorted({f for sha in (old, new) for f in git("diff", "--name-only", f"{sha}^", sha).splitlines()})
    result = {"rebased": git("rev-parse", f"{old}^") != git("rev-parse", f"{new}^"),
              "stat": git("diff", "--stat", old, new, "--", *files).strip()[-3000:] if files else ""}
    for local in refs.values():
        git_run("update-ref", "-d", local)
    return result


def enrich(reported):
    """Network-bound extras, only for events about to be reported, so the idle loop stays free."""
    for event in reported:
        verdict = event.get("ci_verdict") or (event.get("message") if event.get("author_username") == CI_USER else None)
        if verdict and ZUUL_API:
            event["ci_diagnosis"] = diagnose_ci({"change": event["change"], "message": verdict})
            if PERIODIC_BUILD:
                event["base_build"] = base_build(event["branch"])
        if event["kind"] == "review_new_patch_set" and event.get("since_ref") and event.get("current_ref"):
            event["interdiff"] = interdiff(event)
        if event["kind"] == "ci_stuck" and ZUUL_API:
            event["zuul_queue"] = zuul_queue(event["change"], event["patch_set"])
        if event["kind"] == "review_requested" and not event.get("participated"):
            event["prereview"] = prereview(event)
    return reported


def ci_state(change, patch_set, votes):
    if failed_ci_labels(votes):
        return "stale_base" if is_merge_failed(change, patch_set) else "failed"
    if all(1 in votes.get(label, []) for label in CI_LABELS):
        return "passed"
    return "running"


def worktrees_by_change_id():
    """Change-Id → worktree path; linked worktrees win over the main checkout, whose branch keeps moving."""
    paths = [line.removeprefix("worktree ") for line in git("worktree", "list", "--porcelain").splitlines()
             if line.startswith("worktree ")]
    found = {}
    for path in reversed(paths):
        trailers = git("log", "-10", "--format=%(trailers:key=Change-Id,valueonly)", "HEAD", cwd=path)
        for change_id in trailers.split():
            found.setdefault(change_id, path)
    return found


def write_status(result=None, error=None):
    """Snapshot read by the SwiftBar plugin and the Claude Code status line, so they never hit Gerrit themselves.

    On error the last good rows are kept and `updated` stays at the last success."""
    now = time.time()
    if error is not None:
        previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        atomic_write(STATUS, {**previous, "last_attempt": now, "last_error": error})
        return
    worktrees = worktrees_by_change_id()
    threads_error = result.get("threads_error")
    rows = []
    for change in result["changes"]:
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        code_review = votes.get("Code-Review", [])
        rows.append({
            "number": change["number"],
            "patch_set": patch_set.get("number"),
            "subject": change["subject"],
            "url": change["url"],
            "wip": change.get("wip", False),
            "code_review": min(code_review) if code_review and min(code_review) < 0 else max(code_review, default=0),
            "ci": ci_state(change, patch_set, votes),
            "ci_failed": failed_ci_labels(votes),
            "ci_stuck": is_ci_stuck(change, patch_set, now),
            "threads": None if threads_error else len(result.get("threads", {}).get(change["number"], [])),
            "conflict": change["number"] in result["conflicts"],
            "open_parent": result["parents"].get(change["number"]),
            "ready": (is_ready_to_submit(patch_set) and not threads_error
                      and not result.get("threads", {}).get(change["number"])),
            "worktree": worktrees.get(change["id"]),
        })
    atomic_write(STATUS, {"updated": now, "last_attempt": now, "last_error": None, "threads_error": threads_error,
                          "changes": rows})


def event_base(change):
    return {
        "change": change["number"],
        "subject": change["subject"],
        "branch": change["branch"],
        "change_id": change["id"],
        "url": change["url"],
        "patch_set": change.get("currentPatchSet", {}).get("number"),
    }


def message_event(base, message, kind="message"):
    return {
        **base,
        "kind": kind,
        "author": message["reviewer"].get("name", ""),
        "author_username": message["reviewer"].get("username", ""),
        "message": message["message"][:1500],
    }


def awaiting_threads(comments_by_file):
    """Unresolved threads whose last word is not mine, from REST `/comments`; `reply_to` is the draft's in_reply_to."""
    by_id = {c["id"]: {**c, "file": file} for file, comments in comments_by_file.items() for c in comments}

    def root(comment):
        while comment.get("in_reply_to") in by_id:
            comment = by_id[comment["in_reply_to"]]
        return comment["id"]

    threads = {}
    for comment in sorted(by_id.values(), key=lambda c: c["updated"]):
        threads.setdefault(root(comment), []).append(comment)
    awaiting = [{"patch_set": thread[0].get("patch_set"), "file": thread[0]["file"], "line": thread[0].get("line"),
                 "author": thread[-1]["author"].get("name", ""), "reply_to": thread[-1]["id"],
                 "messages": [f'{m["author"].get("username", "")}: {m["message"][:800]}' for m in thread[-3:]]}
                for thread in threads.values()
                if thread[-1].get("unresolved") and thread[-1]["author"].get("username", "") not in {USER, *BOT_USERS}]
    return awaiting[-MAX_THREADS:]


def work_day(now):
    """Reminder bucket: flips at the start of each working day, so nothing re-fires at night or on weekends."""
    day = datetime.date.fromtimestamp(now - WORK_HOURS[0] * 3600)
    return (day - datetime.timedelta(days=max(0, day.weekday() - 4))).isoformat()


def is_quiet(now):
    local = time.localtime(now)
    return local.tm_wday >= 5 or not WORK_HOURS[0] <= local.tm_hour < WORK_HOURS[1]


def working_days_since(start, now):
    first, last = datetime.date.fromtimestamp(start), datetime.date.fromtimestamp(now)
    return sum(1 for n in range(1, (last - first).days + 1)
               if (first + datetime.timedelta(days=n)).weekday() < 5)


def unreviewed_days(change, patch_set, now):
    """Working days the current patch set has waited without a word or vote from a reviewer; None if not waiting."""
    if change.get("wip") or votes_of(patch_set).get("Code-Review"):
        return None
    uploaded = patch_set.get("createdOn", now)
    if any(m["timestamp"] >= uploaded and m["reviewer"].get("username", "") not in {USER, *BOT_USERS}
           for m in change.get("comments", [])):
        return None
    days = working_days_since(uploaded, now)
    return days if days >= UNREVIEWED_WORKING_DAYS else None


def ready_since(patch_set):
    return max((int(a.get("grantedOn", 0)) for a in patch_set.get("approvals", [])
                if a["type"] == "Code-Review" and int(a["value"]) == 2), default=None)


def events(result, now=None):
    now = now or time.time()
    day = work_day(now)
    conflicts = result["conflicts"]
    stale = result.get("stale_parents", {})
    threads = result.get("threads", {})
    threads_error = result.get("threads_error")
    attention = set(result.get("attention", []))
    for change in result["changes"]:
        number = change["number"]
        patch_set = change.get("currentPatchSet", {})
        base = event_base(change)
        for message in change.get("comments", []):
            if not is_actionable(message):
                continue
            author = message["reviewer"].get("username", "")
            # A late verdict on a patch set already replaced by a push says nothing about the current one.
            if author == CI_USER and patch_set_of(message) < int(patch_set.get("number") or 0):
                continue
            event = message_event(base, message)
            if author == CI_USER:
                event["rechecks"] = my_rechecks(change, patch_set, message["timestamp"])
            else:
                event["threads_awaiting_me"] = None if threads_error else threads.get(number, [])
            yield f'{number}:{message["timestamp"]}:{author}', event
        if is_ci_stuck(change, patch_set, now):
            yield f'{number}:ci_stuck:{patch_set.get("number")}', {
                **base, "kind": "ci_stuck", "idle_since": ci_idle_since(change, patch_set)}
        if number in stale:
            # Takes over merge_conflict: the fix is a rebase --onto that drops the old parent, conflicts or not.
            yield f'{number}:parent_merged:{patch_set.get("number")}', {
                **base, "kind": "parent_merged", **stale[number], "files": conflicts.get(number, [])}
        elif number in conflicts:
            yield f'{number}:conflict:{patch_set.get("number")}', {
                **base, "kind": "merge_conflict", "files": conflicts[number]}
        elif (is_ready_to_submit(patch_set) and number not in result["parents"] and not threads.get(number)
              and not threads_error):
            # One key per working day: a CL left unsubmitted comes back the next morning.
            yield f'{number}:ready:{patch_set.get("number")}:{day}', {
                **base, "kind": "ready_to_submit", "ready_since": ready_since(patch_set)}
        days = unreviewed_days(change, patch_set, now)
        if (days and number not in conflicts and number not in stale
                and ci_state(change, patch_set, votes_of(patch_set)) not in ("failed", "stale_base")):
            yield f'{number}:unreviewed:{patch_set.get("number")}:{day}', {
                **base, "kind": "waiting_for_review", "working_days": days,
                "reviewers": [r.get("name") or r.get("username") for r in change.get("allReviewers", [])
                              if r.get("username", "") not in {USER, *BOT_USERS}]}
    for change in result.get("reviews", []):
        yield from review_events(change, day, change["number"] in attention)


def patch_set_of(message):
    match = re.match(r"(?:Patch Set|Uploaded patch set) (\d+)", message.get("message", ""))
    return int(match.group(1)) if match else 0


def is_new_patch_set_notice(message):
    """Upload/rebase notices duplicate review_new_patch_set; anything else from the owner is a reply."""
    text = message.get("message", "")
    return text.startswith("Uploaded patch set") or " was rebased" in text.split("\n\n", 1)[0]


def review_events(change, day, needs_my_attention=False):
    """Changes I review: signal only, never fixed — they are someone else's code.

    Gerrit's attention set names me when another reviewer answers one of my threads, so their replies count too."""
    number = change["number"]
    base = {**event_base(change), "owner": change.get("owner", {}).get("name", "")}
    owner = change.get("owner", {}).get("username")
    current = change.get("currentPatchSet", {}).get("number", 0)
    comments = change.get("comments", [])
    mine = [m for m in comments if m["reviewer"].get("username") == USER]
    my_votes = [(int(ps["number"]), int(a["value"]))
                for ps in change.get("patchSets", []) for a in ps.get("approvals", [])
                if a["by"].get("username") == USER and a["type"] == "Code-Review"]
    humans = [r for r in change.get("allReviewers", []) if r.get("username", "") not in {owner, *BOT_USERS}]

    last_seen_ps = max([ps for ps, _ in my_votes] + [patch_set_of(m) for m in mine], default=0)
    if not change.get("wip") and len(humans) <= MAX_REVIEWERS:
        # Untouched requests come back each working day; once I took part, it is not a request anymore.
        key = f"{number}:review_requested" if last_seen_ps else f"{number}:review_requested:{day}"
        yield key, {**base, "kind": "review_requested", "reviewers": len(humans), "participated": bool(last_seen_ps),
                    "current_ref": change.get("currentPatchSet", {}).get("ref")}

    # A vote Gerrit copied onto the current patch set (trivial rebase) means nothing needs re-reviewing.
    if last_seen_ps and last_seen_ps < current and not any(ps == current for ps, _ in my_votes):
        last_vote = max(my_votes, default=None)
        refs = {int(ps["number"]): ps.get("ref") for ps in change.get("patchSets", [])}
        yield f"{number}:review_ps:{current}", {
            **base, "kind": "review_new_patch_set", "since_patch_set": last_seen_ps,
            "since_ref": refs.get(last_seen_ps), "current_ref": refs.get(int(current)),
            "my_last_vote": {"patch_set": last_vote[0], "value": last_vote[1]} if last_vote else None}

    if not mine:
        return
    my_last = max(m["timestamp"] for m in mine)
    for message in comments:
        author = message["reviewer"].get("username")
        replied = author == owner or (needs_my_attention and (author or "") not in {USER, *BOT_USERS})
        if message["timestamp"] > my_last and replied and not is_new_patch_set_notice(message):
            yield f'{number}:review_reply:{message["timestamp"]}', message_event(base, message, "review_reply")


def pending_events(result, now=None):
    """Start-up sweep: what needs me right now, instead of replaying every past message."""
    for change in result["changes"]:
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        ci = ci_state(change, patch_set, votes)
        code_review = min(votes.get("Code-Review", [0]))
        threads = None if result.get("threads_error") else result.get("threads", {}).get(change["number"], [])
        if not (threads or code_review < 0 or ci in ("failed", "stale_base")):
            continue
        event = {**event_base(change), "kind": "pending", "wip": change.get("wip", False), "ci": ci,
                 "code_review": code_review, "threads_awaiting_me": threads}
        verdict = latest_ci_verdict(change, patch_set) if ci == "failed" else None
        if verdict:
            event["ci_verdict"] = verdict["message"][:1500]
            event["rechecks"] = my_rechecks(change, patch_set, verdict["timestamp"])
        yield event
    for _, event in events(result, now):
        if event["kind"] != "message" and not event.get("participated"):
            yield event


def remember(seen, keys, result, current, now):
    """Marks `keys` seen. Keys of changes gone from both queries (merged, abandoned) are kept for a while, so a
    change restored soon after does not replay its history, then dropped so the seen files stop growing."""
    alive = {str(c["number"]) for c in result["changes"] + result.get("reviews", [])}
    live = lambda key: key in current or key.split(":", 1)[0] in alive
    return {key: now if live(key) else stamp
            for key, stamp in {**seen, **dict.fromkeys(keys, now)}.items()
            if live(key) or now - stamp < SEEN_RETENTION_S}


def claude_pid():
    """The Claude session running this watcher, found by walking up the process tree."""
    pid = os.getppid()
    while pid > 1:
        row = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
        if len(row) < 2:
            return None
        if pathlib.Path(row[1].strip()).name == "claude":
            return pid
        pid = int(row[0])
    return None


def write_session_lock():
    """Read by macos/open-babysit.sh: a notification click focuses this session instead of opening a second one."""
    pid = claude_pid()
    if pid:
        atomic_write(SESSION, {"claude_pid": pid, "orca_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")})


def load_seen(state=None):
    """{event key: last time its change was alive}."""
    state = state or STATE
    return json.loads(state.read_text()) if state.exists() else {}


def save_seen(seen, state=None):
    atomic_write(state or STATE, dict(sorted(seen.items())))


def swiftbar(action, **params):
    subprocess.run(["open", "-g", f"swiftbar://{action}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"],
                   capture_output=True, timeout=10)


def notification(event):
    n = event.get("change")
    kind = event["kind"]
    if kind == "merge_conflict":
        return t("conflict", n=n), ", ".join(pathlib.Path(f).name for f in event["files"])
    if kind == "ready_to_submit":
        since = event.get("ready_since")
        days = int((time.time() - since) // 86400) if since else 0
        return t("ready", n=n) + (t("ready_for", days=days) if days else ""), event["subject"]
    if kind == "ci_stuck":
        hours = int((time.time() - event["idle_since"]) // 3600)
        return t("ci_stuck", n=n), t("ci_stuck_body", hours=hours, subject=event["subject"])
    if kind == "parent_merged":
        return t("rebase", n=n), t("rebase_body", parent=event["parent"], subject=event["subject"])
    if kind == "waiting_for_review":
        return t("unreviewed", n=n, days=event["working_days"]), event["subject"]
    if kind == "message":
        return f"{n} · {event['author']}", event["message"].split("\n\n", 1)[-1][:200]
    if kind == "review_requested":
        return t("review_requested", owner=event["owner"]), f"{n} · {event['subject']}"
    if kind == "review_new_patch_set":
        return t("new_patch_set", n=n, ps=event["patch_set"]), f"{event['owner']} · {event['subject']}"
    if kind == "review_reply":
        return t("replied", n=n, author=event["author"]), event["message"].split("\n\n", 1)[-1][:200]
    return None


def notifications(fresh):
    """(title, body, href): a click opens the change in Gerrit, or my dashboard for a summary."""
    contents = [(*content, event.get("url") or DASHBOARD) for event in fresh if (content := notification(event))]
    if len(contents) <= DIGEST_OVER:
        return contents
    return [(t("digest", count=len(contents)), " · ".join(title for title, _, _ in contents)[:200], DASHBOARD)]


def daemon(interval):
    """Status snapshot + notifications with no Claude session; clicking a notification opens one."""
    first_run = not DAEMON_STATE.exists()
    while True:
        try:
            result = poll()
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            detail = error_detail(error)
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} poll failed: {detail}", file=sys.stderr, flush=True)
            write_status(error=detail)
            write_daemon_poll(error=detail)
            swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)
            time.sleep(interval)
            continue
        write_status(result)
        write_daemon_poll(result)
        swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)

        now = time.time()
        current = dict(events(result, now))
        seen = load_seen(DAEMON_STATE)
        quiet = is_quiet(now)
        if not first_run and not quiet:
            for title, body, href in notifications(current[key] for key in sorted(current.keys() - seen.keys())):
                swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=title, body=body, href=href)
        # Held back at night and on weekends: still unseen, they notify at the start of the next working day.
        marked = () if quiet and not first_run else current.keys()
        first_run = False
        save_seen(remember(seen, marked, result, current, now), DAEMON_STATE)
        time.sleep(interval)


def main():
    if not HOST:
        sys.exit("gerrit-babysit: set gerrit_host in config.json (see config.example.json)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pending", action="store_true",
                        help="print what needs me now, mark everything current as seen and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever: status snapshot + macOS notifications")
    args = parser.parse_args()
    if args.daemon:
        return daemon(args.interval)

    write_session_lock()
    failures = 0
    settling = {}
    while True:
        try:
            result = daemon_poll()
            if result is None:
                result = poll()
                write_status(result)
            cleanup = dict(cleanup_candidates(frozenset(c["id"] for c in result["changes"])))
            now = time.time()
            current = dict(events(result, now)) | cleanup
            failures = 0
        except (subprocess.SubprocessError, OSError, ValueError, DaemonError) as error:
            if not isinstance(error, DaemonError):
                write_status(error=error_detail(error))
            failures += 1
            if failures >= 10:
                print(json.dumps({"status": "error", "detail": error_detail(error)}))
                return 1
            time.sleep(args.interval * failures)
            continue

        seen = load_seen()
        if args.pending:
            # Same poll for the report and the seen set: nothing can slip in between.
            report = enrich(list(pending_events(result, now)) + list(cleanup.values()))
            save_seen(remember(seen, current.keys(), result, current, now))
            print(json.dumps({"status": "pending", "events": report, "threads_error": result.get("threads_error")},
                             ensure_ascii=False, indent=1))
            return 0

        fresh = {key: current[key] for key in current.keys() - seen.keys()}
        if fresh and not settling:
            # A reply, its vote and zuul's verdict often land a minute apart: wait once to wake for all of them.
            settling = fresh
            time.sleep(SETTLE_S)
            continue
        settling = {key: current.get(key, event) for key, event in settling.items()} | fresh
        if settling:
            save_seen(remember(seen, settling.keys(), result, current, now))
            print(json.dumps({"status": "events", "events": enrich(list(settling.values())),
                              "threads_error": result.get("threads_error")}, ensure_ascii=False, indent=1))
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
