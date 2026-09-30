"""Pure logic: a poll's rows in, keyed events and notification texts out. No network, no git."""
import datetime
import pathlib
import re
import time
import urllib.parse

import ci
from config import CI_STUCK_S, CONFIG
from i18n import t
from gerrit import BOT_USERS, CI_USER, HOST, NOT_ME, USER

CI_LABELS = tuple(CONFIG["ci_labels"])
# Group additions put dozens of people on a change: not a personal review request.
MAX_REVIEWERS = CONFIG["max_reviewers"]
MAX_THREADS = 20
WORK_HOURS = tuple(CONFIG["work_hours"])
UNREVIEWED_WORKING_DAYS = 2
# Past this many notifications in one poll (typically the morning flush), a single summary replaces them.
DIGEST_OVER = 3
DASHBOARD = f"https://{HOST}/dashboard/self"

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
    return 2 in code_review and min(code_review) >= 0 and ci_passed(votes)


def ci_passed(votes):
    # Some CIs vote +2 (e.g. a gate pipeline), not only +1.
    return all(max(votes.get(label, [0])) >= 1 and min(votes[label]) >= 0 for label in CI_LABELS)


def votes_of(patch_set):
    votes = {}
    for approval in patch_set.get("approvals", []):
        votes.setdefault(approval["type"], []).append(int(approval["value"]))
    return votes


def failed_ci_labels(votes):
    return [label for label in CI_LABELS if min(votes.get(label, [0])) < 0]


def ci_state(change, patch_set, votes):
    if failed_ci_labels(votes):
        return "stale_base" if is_merge_failed(change, patch_set) else "failed"
    if ci_passed(votes):
        return "passed"
    return "running"


def patch_set_messages(change, patch_set, author):
    prefix = f"Patch Set {patch_set.get('number')}:"
    return [m for m in change.get("comments", [])
            if m["reviewer"].get("username") == author and m["message"].startswith(prefix)]


def latest_ci_verdict(change, patch_set):
    return max(patch_set_messages(change, patch_set, CI_USER), key=lambda m: m["timestamp"], default=None)


def ci_idle_since(change, patch_set):
    """Zuul's last sign of life on this patch set (a recheck's "Starting" counts), else the upload."""
    return max((m["timestamp"] for m in patch_set_messages(change, patch_set, CI_USER)),
               default=patch_set.get("createdOn", 0))


def is_ci_stuck(change, patch_set, now):
    return (ci_state(change, patch_set, votes_of(patch_set)) == "running"
            and now - ci_idle_since(change, patch_set) > CI_STUCK_S)


def is_merge_failed(change, patch_set):
    """Zuul's latest verdict on this patch set is 'Merge Failed.': no job ran, the base is stale."""
    verdict = latest_ci_verdict(change, patch_set)
    return verdict is not None and "Merge Failed." in verdict["message"]


def my_rechecks(change, patch_set, before):
    """How many times I already asked zuul to rerun this patch set before the given verdict."""
    return sum(1 for m in patch_set_messages(change, patch_set, USER)
               if m["timestamp"] < before and ci.RECHECK.fullmatch(m["message"].partition("\n")[2].strip()))


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
                if thread[-1].get("unresolved") and thread[-1]["author"].get("username", "") not in NOT_ME]
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
    if any(m["timestamp"] >= uploaded and m["reviewer"].get("username", "") not in NOT_ME
           for m in change.get("comments", [])):
        return None
    days = working_days_since(uploaded, now)
    return days if days >= UNREVIEWED_WORKING_DAYS else None


def attention_entries(attention_set):
    """REST's `attention_set` / `removed_from_attention_set` ({account id: AttentionSetInfo}) as a list."""
    return [{"username": info["account"].get("username", ""),
             "name": info["account"].get("name") or info["account"].get("username", ""),
             "since": gerrit_time(info["last_update"]), "reason": info.get("reason", "")}
            for info in (attention_set or {}).values()]


def gerrit_time(text):
    """REST timestamps are UTC, "2026-09-29 10:00:00.000000000"."""
    return datetime.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()


def review_wait(change, patch_set, attention, now):
    """(working days, waiting_on, dismissed) when the change waits on reviewers, else None.

    With the attention set (`attention` = {"holders", "removed"}), a reviewer holding it counts from the moment
    Gerrit handed it over, even after a first round; one who left it without a word saw the change and passed.
    Without it (None), only a patch set nobody reviewed counts, and both lists are None."""
    unreviewed = unreviewed_days(change, patch_set, now)
    if attention is None:
        return (unreviewed, None, None) if unreviewed else None
    if change.get("wip") or any(h["username"] == USER for h in attention["holders"]):
        return None
    uploaded = patch_set.get("createdOn", now)
    holders = [{"name": h["name"], "username": h["username"], "working_days": working_days_since(h["since"], now)}
               for h in attention["holders"] if h["username"] not in NOT_ME]
    spoke = {m["reviewer"].get("username", "") for m in change.get("comments", []) if m["timestamp"] >= uploaded}
    dismissed = [{"name": r["name"], "username": r["username"], "reason": r["reason"]} for r in attention["removed"]
                 if r["since"] >= uploaded and r["username"] not in NOT_ME | spoke | {h["username"] for h in holders}]
    held = max((h["working_days"] for h in holders), default=0)
    if held >= UNREVIEWED_WORKING_DAYS:
        return held, holders, dismissed
    # Held for a short while: the reviewer just got it, too soon to chase.
    return (unreviewed, [], dismissed) if unreviewed and not holders else None


def nudges(reported):
    """One entry per reviewer holding waiting changes: a single reminder for all of them, not one per change."""
    by_reviewer = {}
    waiting = [event for event in reported if event["kind"] == "waiting_for_review"]
    for event in waiting:
        for holder in event.get("waiting_on") or []:
            entry = by_reviewer.setdefault(holder["username"], {"reviewer": holder["name"], "username": holder["username"],
                                                                "changes": [], "working_days": 0})
            entry["changes"].append(event["change"])
            entry["working_days"] = max(entry["working_days"], holder["working_days"])
    return sorted(by_reviewer.values(), key=lambda e: (-len(e["changes"]), e["reviewer"]))


def ready_since(patch_set):
    return max((int(a.get("grantedOn", 0)) for a in patch_set.get("approvals", [])
                if a["type"] == "Code-Review" and int(a["value"]) == 2), default=None)


def events(result, now=None):
    now = now or time.time()
    day = work_day(now)
    conflicts = result["conflicts"]
    stale = result.get("stale_parents", {})
    outdated = result.get("outdated_parents", {})
    threads = result.get("threads", {})
    threads_error = result.get("threads_error")
    attention = set(result.get("attention", []))
    attention_sets = result.get("attention_sets")
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
        elif number in outdated:
            # Rebasing onto the parent's new patch set comes first: a conflict with the branch may go with it.
            yield f'{number}:parent_updated:{patch_set.get("number")}:{outdated[number]["new_parent_sha"]}', {
                **base, "kind": "parent_updated", **outdated[number]}
        elif number in conflicts:
            yield f'{number}:conflict:{patch_set.get("number")}', {
                **base, "kind": "merge_conflict", "files": conflicts[number]}
        elif (is_ready_to_submit(patch_set) and number not in result["parents"] and not threads.get(number)
              and not threads_error):
            # One key per working day: a CL left unsubmitted comes back the next morning.
            yield f'{number}:ready:{patch_set.get("number")}:{day}', {
                **base, "kind": "ready_to_submit", "ready_since": ready_since(patch_set)}
        wait = None if attention_sets is None else attention_sets.get(number, {"holders": [], "removed": []})
        wait = review_wait(change, patch_set, wait, now)
        if (wait and number not in conflicts and number not in stale and not is_ready_to_submit(patch_set)
                and ci_state(change, patch_set, votes_of(patch_set)) not in ("failed", "stale_base")):
            days, waiting_on, dismissed = wait
            yield f'{number}:unreviewed:{patch_set.get("number")}:{day}', {
                **base, "kind": "waiting_for_review", "working_days": days, "waiting_on": waiting_on,
                "dismissed": dismissed,
                "reviewers": [r.get("name") or r.get("username") for r in change.get("allReviewers", [])
                              if r.get("username", "") not in NOT_ME]}
    yield from base_events(result)
    for change in result.get("reviews", []):
        yield from review_events(change, day, change["number"] in attention)


def base_events(result):
    """One event per red streak of a target branch, naming every change of mine on it: the last green build keys
    the streak, so it is announced once however long it lasts."""
    for branch, health in sorted(result.get("base_health", {}).items()):
        changes = sorted(c["number"] for c in result["changes"] if c["branch"] == branch)
        if ci.is_red(health) and changes:
            yield f'base_red:{branch}:{health.get("last_green") or "?"}', {
                "kind": "base_red", "branch": branch, "url": health.get("log_url"), "changes": changes, **health}


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
        replied = author == owner or (needs_my_attention and (author or "") not in NOT_ME)
        if message["timestamp"] > my_last and replied and not is_new_patch_set_notice(message):
            yield f'{number}:review_reply:{message["timestamp"]}', message_event(base, message, "review_reply")


def pending_events(result, now=None):
    """Start-up sweep: what needs me right now, instead of replaying every past message."""
    for change in result["changes"]:
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        state = ci_state(change, patch_set, votes)
        code_review = min(votes.get("Code-Review", [0]))
        threads = None if result.get("threads_error") else result.get("threads", {}).get(change["number"], [])
        if not (threads or code_review < 0 or state in ("failed", "stale_base")):
            continue
        event = {**event_base(change), "kind": "pending", "wip": change.get("wip", False), "ci": state,
                 "code_review": code_review, "threads_awaiting_me": threads}
        verdict = latest_ci_verdict(change, patch_set) if state == "failed" else None
        if verdict:
            event["ci_verdict"] = verdict["message"][:1500]
            event["rechecks"] = my_rechecks(change, patch_set, verdict["timestamp"])
        yield event
    for _, event in events(result, now):
        if event["kind"] != "message" and not event.get("participated"):
            yield event


def status_rows(result, flakes, worktrees, now):
    """The snapshot rows read by the SwiftBar plugin and the status line; `worktrees` is {Change-Id: path}."""
    threads = result.get("threads", {})
    threads_error = result.get("threads_error")
    rows = []
    for change in result["changes"]:
        number = change["number"]
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        code_review = votes.get("Code-Review", [])
        state = ci_state(change, patch_set, votes)
        verdict = latest_ci_verdict(change, patch_set) if state == "failed" else None
        failed_jobs = [{"job": j["job"], "result": j["result"]} for j in ci.failed_jobs(verdict["message"])] if verdict else []
        outdated = result.get("outdated_parents", {}).get(number)
        rows.append({
            "number": number,
            "patch_set": patch_set.get("number"),
            "subject": change["subject"],
            "url": change["url"],
            "wip": change.get("wip", False),
            "code_review": min(code_review) if code_review and min(code_review) < 0 else max(code_review, default=0),
            "ci": state,
            "ci_failed": failed_ci_labels(votes),
            "ci_failed_jobs": failed_jobs,
            "flaky": ci.flaky_counts(flakes, [j["job"] for j in failed_jobs], now),
            # Every recheck on the patch set, also after the verdict: one may already be running.
            "rechecks": my_rechecks(change, patch_set, float("inf")),
            "ci_stuck": is_ci_stuck(change, patch_set, now),
            "threads": None if threads_error else len(threads.get(number, [])),
            "conflict": number in result["conflicts"],
            "open_parent": result["parents"].get(number),
            "outdated_parent": outdated["parent"] if outdated else None,
            "ready": is_ready_to_submit(patch_set) and not threads_error and not threads.get(number),
            "worktree": worktrees.get(change["id"]),
            "branch": change["branch"],
            "base_red": ci.is_red(result.get("base_health", {}).get(change["branch"])),
        })
    return rows


def notification(event, now=None):
    now = now or time.time()
    n = event.get("change")
    kind = event["kind"]
    if kind == "merge_conflict":
        return t("conflict", n=n), ", ".join(pathlib.Path(f).name for f in event["files"])
    if kind == "ready_to_submit":
        since = event.get("ready_since")
        days = int((now - since) // 86400) if since else 0
        return t("ready", n=n) + (t("ready_for", days=days) if days else ""), event["subject"]
    if kind == "ci_stuck":
        hours = int((now - event["idle_since"]) // 3600)
        return t("ci_stuck", n=n), t("ci_stuck_body", hours=hours, subject=event["subject"])
    if kind == "parent_merged":
        return t("rebase", n=n), t("rebase_body", parent=event["parent"], subject=event["subject"])
    if kind == "parent_updated":
        return t("parent_updated", n=n, parent=event["parent"]), event["subject"]
    if kind == "waiting_for_review" and event.get("waiting_on"):
        names = ", ".join(h["name"] for h in event["waiting_on"])
        return t("waiting_on", n=n, names=names, days=event["working_days"]), event["subject"]
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
    if kind == "base_red":
        hours = int(ci.red_for_s(event, now) // 3600)
        return (t("base_red", branch=event["branch"], hours=hours),
                t("base_red_body", changes=", ".join(map(str, event["changes"]))))
    return None


def waiting_on_url(username):
    return f"https://{HOST}/q/" + urllib.parse.quote(f"owner:self status:open attention:{username}")


def notifications(fresh, now=None):
    """(title, body, href): a click opens the change in Gerrit, or my dashboard for a summary.

    A reviewer holding several waiting changes gets one line for all of them, opening that list in Gerrit."""
    fresh = list(fresh)
    grouped = [n for n in nudges(fresh) if len(n["changes"]) > 1]
    covered = {number for n in grouped for number in n["changes"]}
    contents = [(t("waiting_on_many", count=len(n["changes"]), name=n["reviewer"]), " · ".join(map(str, n["changes"])),
                 waiting_on_url(n["username"])) for n in grouped]
    contents += [(*content, event.get("url") or DASHBOARD) for event in fresh
                 if not (event["kind"] == "waiting_for_review" and event.get("change") in covered)
                 and (content := notification(event, now))]
    if len(contents) <= DIGEST_OVER:
        return contents
    return [(t("digest", count=len(contents)), " · ".join(title for title, _, _ in contents)[:200], DASHBOARD)]
