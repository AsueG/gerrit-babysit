"""One poll of Gerrit, zuul and the local repo: the raw state watch.py turns into events and the snapshot."""
import concurrent.futures
import subprocess
import urllib.parse

import ci
import events
import gerrit
import repo
from memo import PollMemo

QUERY = "owner:self status:open"
REVIEW_QUERY = "reviewer:self status:open -owner:self"
ATTENTION_QUERY = "attention:self status:open"
DRAFTS_QUERY = "has:draft status:open"


def fetch_changes():
    return gerrit.query(QUERY, "--current-patch-set", "--comments", "--dependencies", "--all-reviewers",
                        "--submit-records")


def fetch_reviews():
    return gerrit.query(REVIEW_QUERY, "--current-patch-set", "--all-approvals", "--all-reviewers", "--comments",
                        "--submit-records")


def fetch_attention():
    return sorted(row["number"] for row in gerrit.query(ATTENTION_QUERY))


def my_attention_sets():
    """{change number: {"holders", "removed"}} for my changes, None when REST fails. SSH queries carry no attention
    set; without it, waiting_for_review falls back to counting days."""
    try:
        rows = gerrit.rest_get(f"/changes/?q={urllib.parse.quote(QUERY)}&o=DETAILED_ACCOUNTS")
    except (OSError, ValueError, KeyError):
        return None
    return {row["_number"]: {"holders": events.attention_entries(row.get("attention_set")),
                             "removed": events.attention_entries(row.get("removed_from_attention_set"))}
            for row in rows}


_threads_memo = PollMemo()


def open_threads(changes):
    """{change number: threads awaiting me}; the REST call only reruns when the change moves."""
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes}
    with _threads_memo.poll() as memo:
        threads = memo.get_all(keys.values(),
                               lambda key: events.awaiting_threads(gerrit.rest_get(f"/changes/{key[0]}/comments")))
    return {number: threads[key] for number, key in keys.items()}


_requirements_memo = PollMemo()


def submit_blockers(changes):
    """{change number: unsatisfied submit requirements} for the changes the votes call ready, None when REST fails.
    SSH queries carry no requirement, and the code-owners plugin only shows up there; asked again when the change
    moves."""
    ready = {c["number"]: c for c in changes if events.is_ready_to_submit(c)}
    keys = {number: (number, c.get("lastUpdated")) for number, c in ready.items()}

    def fetch(key):
        return events.unsatisfied_requirements(gerrit.rest_get(f"/changes/{key[0]}?o=SUBMIT_REQUIREMENTS"))

    try:
        with _requirements_memo.poll() as memo:
            requirements = memo.get_all(keys.values(), fetch)
    except (OSError, ValueError, KeyError):
        return None
    return {number: events.submit_blockers_of(ready[number], requirements[key]) for number, key in keys.items()}


def parent_statuses(changes):
    """{change number: (parent number, parent status, parent's current patch set)} — `dependsOn` lists merged
    parents too."""
    parent_of = {c["number"]: c["dependsOn"][0]["number"] for c in changes if c.get("dependsOn")}
    parents = {c["number"]: c for c in changes}
    others = sorted(set(parent_of.values()) - parents.keys())
    if others:
        query = " OR ".join(f"change:{n}" for n in others)
        parents |= {row["number"]: row for row in gerrit.query(query, "--current-patch-set")}
    return {number: (parent, parents.get(parent, {}).get("status"), parents.get(parent, {}).get("currentPatchSet", {}))
            for number, parent in parent_of.items()}


def outdated_parents(changes, statuses):
    """{change number: {parent, parent_patch_set, parent_ref, old_parent_sha, new_parent_sha}} when the open parent
    got a patch set the change is not based on yet."""
    outdated = {}
    for change in changes:
        parent, status, parent_patch_set = statuses.get(change["number"], (None, None, {}))
        base = repo.first_parent(change)
        new = (parent_patch_set or {}).get("revision")
        if status == "NEW" and base and new and base != new:
            outdated[change["number"]] = {"parent": parent, "parent_patch_set": parent_patch_set.get("number"),
                                          "parent_ref": parent_patch_set.get("ref"), "old_parent_sha": base,
                                          "new_parent_sha": new}
    return outdated


def fetch_drafts():
    """{change number: row} of the changes holding drafts I never published; {} when Gerrit is unreachable."""
    try:
        return {row["number"]: row for row in gerrit.query(DRAFTS_QUERY)}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


def threads_or_error(changes):
    """REST relies on the HTTP password, SSH does not: an expired token must not blind the whole watcher."""
    try:
        return open_threads(changes), None
    except (OSError, ValueError, KeyError) as error:
        return {}, gerrit.error_detail(error)


def base_health(changes):
    """{branch: periodic build health} for the branches my changes target, zuul unreachable included: the red base
    is watched on every poll, not only once a change of mine fails. Each branch is its own zuul round trip, so poll()
    does not pay N x their latency."""
    if not (ci.ZUUL_API and ci.PERIODIC_BUILD):
        return {}
    branches = sorted({c["branch"] for c in changes})
    if len(branches) < 2:
        return {branch: ci.base_health(branch) for branch in branches}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(branches)) as pool:
        return dict(zip(branches, pool.map(ci.base_health, branches)))


def poll():
    # Independent round trips run side by side: the SSH queries share one multiplexed connection, and the REST
    # calls overlap the local git fetch.
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        reviews = pool.submit(fetch_reviews)
        attention = pool.submit(fetch_attention)
        attention_sets = pool.submit(my_attention_sets)
        changes = fetch_changes()
        threads_job = pool.submit(threads_or_error, changes)
        blockers = pool.submit(submit_blockers, changes)
        base_job = pool.submit(base_health, changes)
        statuses_job = pool.submit(parent_statuses, changes)
        conflicts = repo.merge_conflicts(changes)
        statuses = statuses_job.result()
        stale = repo.stale_parents(changes, statuses)
        threads, threads_error = threads_job.result()
        return {
            "changes": changes,
            "conflicts": conflicts,
            "parents": {n: parent for n, (parent, status, _) in statuses.items() if status == "NEW"},
            "stale_parents": stale,
            "outdated_parents": outdated_parents(changes, statuses),
            "threads": threads,
            "threads_error": threads_error,
            "reviews": reviews.result(),
            "attention": attention.result(),
            "attention_sets": attention_sets.result(),
            "submit_blockers": blockers.result(),
            "base_health": base_job.result(),
        }


def with_int_keys(result):
    """JSON turns the change-number keys into strings."""
    keys = ("conflicts", "parents", "stale_parents", "outdated_parents", "threads", "attention_sets", "submit_blockers")
    return {**result, **{key: {int(k): v for k, v in result[key].items()} for key in keys if result.get(key) is not None}}
