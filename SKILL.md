---
name: gerrit-babysit
description: Watches my open Gerrit changes in the background (reviewer comments, Code-Review -1, zuul failures classified by cause, Merge Failed, merged parent) and the changes I am asked to review, prepares the fix in the change's worktree, then waits for my approval before any push or published reply. Use when the user says 'babysit', 'watch my reviews', 'watch gerrit', or '/gerrit-babysit'.
argument-hint: "[stop]"
---

# Gerrit babysit

Event loop: `watch.py` polls Gerrit over SSH every 60 s **without spending tokens** and only returns
when an actionable event arrives. (`gerrit stream-events` needs a capability most accounts lack, hence
the polling.)

`<skill>` below is this skill's directory: `${CLAUDE_PLUGIN_ROOT}` when installed as a plugin (if that
still reads literally, the skill was cloned: use the folder of this file).

**Before anything else**, read `LOCAL.md` if it exists, next to this file or in `~/.config/gerrit-babysit/`
(plugin installs): it holds this team's conventions (build commands, recheck variants, fix recipes,
ownership rules) and overrides the generic advice below. Talk to the user in their language.

Settings (Gerrit host, CI labels, zuul API…) live in `<skill>/config.json` or
`~/.config/gerrit-babysit/config.json`; `ADAPTING.md` explains them.

**Absolute rule: nothing visible to others without an explicit "yes".** No push, no published comment,
no vote, no recheck. Everything else (worktree, fix, local amend, tests) happens on its own.

## Start

1. **Initial sweep**: `python3 <skill>/watch.py --pending`. It returns
   `{"status": "pending", "events": [...]}` and marks the whole current state as seen **from the same
   poll**, so nothing slips between the sweep and the watch. Every change of mine that needs something
   gives a `kind: pending` item (`ci`, `code_review`, `threads_awaiting_me`, plus `ci_diagnosis` and
   `base_build` when CI is red). State events follow: `merge_conflict`, `parent_merged`,
   `parent_updated`, `ci_stuck`, `ready_to_submit`, `submit_blocked`, `waiting_for_review`, `base_red`, `cleanup_candidate`,
   and the `review_*` ones (`review_requested` where I already voted or commented are left out). Handle each
   item with the steps below. `snoozed` = `{number: {until | patch_set | base_green}}` of the changes muted by the user:
   their events are held back (neither reported nor marked seen) and come back when the snooze ends. Do not
   mention them unless asked.

   The sweep opens on `unfinished` items: work an earlier session left halfway (it crashed or ended between
   the fix and its approval). `worktree` + `unpushed` = a local commit Gerrit has not seen (an amend, a prepared
   rebase), `busy` = edits or a rebase/merge halfway through, `drafts` = draft comments I never published (on
   my changes or on reviews). Look at them first (`git -C <worktree> status`, `git log -1`, the drafts in
   Gerrit), then offer to resume where it stopped (verify, then "4. Present and ask"); never push or
   publish them on your own.

   **Present the sweep as a briefing**, not a replay: the events come most urgent first (unfinished work, red
   base, conflicts and rebases, my changes in trouble, submit blocked, ready to submit, reviews waiting on me,
   waits, cleanup). Keep that order, one short line per change, grouped under those headings, and give the
   counts at the top. Then handle them in that order.
2. **Start the watcher** with `Bash` and `run_in_background: true`: `python3 <skill>/watch.py`.
   End the turn. The process exit notification is the wake-up — never poll its output.

The watcher writes `~/.cache/gerrit-babysit/session.json` (PID of the `claude` process +
`ORCA_TERMINAL_HANDLE`). Only `macos/open-babysit.sh` reads it, to focus this session instead of opening
a second one.

Stop (`/gerrit-babysit stop` or "stop watching"): `TaskStop` on the watcher task, then
`rm ~/.cache/gerrit-babysit/session.json`.

If the `Stop` hook (`stop_hook.py`) is installed, it refuses to end a turn of this session while no session
`watch.py` (neither `--daemon` nor `--pending`) runs under its `claude` process. Other sessions are left
alone, and it blocks only once per turn so a failing relaunch cannot loop. To stop on purpose, delete
`session.json` **before** ending the turn.

The snapshot `~/.cache/gerrit-babysit/status.json` (per change: votes, `ci` =
`running`/`passed`/`failed`/`stale_base` (Merge Failed), `ci_failed`, conflict, `open_parent`, worktree,
patch set, `branch`, `base_red`; plus `last_attempt`/`last_error` and `base_health` = `{branch: base_build}`) feeds the status line segment and the SwiftBar menu.

When installed (`macos/install.sh`), a LaunchAgent runs `watch.py --daemon` permanently: fresh snapshot +
one macOS notification per new event, with its own state (`daemon-seen.json`) and SSH failures logged to
`daemon.log`. It also publishes its latest poll to `daemon-poll.json`: while it is alive (< 6 min), the
session watcher reuses it instead of querying Gerrit and fetching again (only the cleanup query stays in
the session). No notifications outside working hours (`work_hours`, Mon–Fri): events stay unseen and
notify at the start of the next working day. Past 3 notifications in one poll (typically that morning
catch-up), a single summary replaces them. Changes waiting on the same reviewer notify once ("3 CLs waiting on
Alice", opening their `attention:` query in Gerrit). The daemon fixes nothing: clicking opens the change in Gerrit.
After editing `watch.py`, restart it: `launchctl kickstart -k gui/$(id -u)/<launchd_label>`.

## On each wake-up

The output is `{"status": "events", "events": [...]}`. After the first new event, the watcher waits 90 s
more and delivers everything that arrived meanwhile in a single wake-up (reply + vote + zuul verdict).
The events come most urgent first, as in the sweep: when many arrive at once (a snooze ending, the morning
catch-up), present them as the same briefing. Each item has a `kind`:
`message` (comment/vote/zuul verdict: `author`, `author_username`, text; a zuul failure also carries
`ci_diagnosis`, `base_build` and `rechecks` = how many `recheck*` I already posted on this patch set
before this verdict, and `known_flaky` = `{job: {week, month, last}}` from the local flake memory),
`merge_conflict` (+ `files`), `parent_merged` (+ `parent`, `old_parent_sha`, `files`, `rebase`),
`parent_updated` (+ `parent`, `parent_patch_set`, `old_parent_sha`, `new_parent_sha`, `rebase`), `ci_stuck` (+ `idle_since`, `zuul_queue`), `waiting_for_review` (+ `working_days`,
`reviewers`, `waiting_on`, `dismissed`), `base_red` (no `change`: `branch`, `changes`, see `base_build`
below), `ready_to_submit` / `submit_blocked` (+ `requirements`) ("Submit" section), `cleanup_candidate` ("Cleanup" section) or
`review_requested` / `review_new_patch_set` / `review_reply` ("Other people's reviews" section).
The watcher never gives up on Gerrit being unreachable (VPN off…): it retries with a backoff capped at
10 min, and the status line shows `?` meanwhile. Only `--pending` returns `status: error` (with `detail`)
on the first failure → tell the user, run the "Doctor" section, and start the watcher anyway.

`threads_error` (at the root of the output and in `status.json`) = the REST call for comment threads
failed (HTTP password expired or missing) while SSH works. Polling goes on: `threads_awaiting_me` is then
`null` (unknown, **not** "no threads"), `ready_to_submit` is held back and the status line shows
`threads ?`. Tell the user once (renew the HTTP password), without stopping. With no open change of mine,
`--pending` still checks the HTTP password and reports it the same way, before a change needs it.

`nudges` (at the root of both outputs) = the reported `waiting_for_review` grouped by the reviewer holding
them: `[{reviewer, username, changes, working_days}]`, most loaded first.

Bots are ignored everywhere: the CI user (except its verdicts), the `bot_users` of `config.json` and
Gerrit itself (messages without a username, e.g. auto-abandon). They never count as the last word of a
thread, as a reviewer in `waiting_for_review`, or as a reply in `review_reply`.

Already filtered by the watcher: +1/+2 votes without a comment (a +2 comes back as `ready_to_submit`) and
zuul verdicts on a patch set replaced since. A human `message` carries `threads_awaiting_me`: the
**unresolved** threads of the change whose last word is not mine (REST API; at most 20, last 3 messages
each), with `file`, `line`, `patch_set` and `reply_to` = id of the last comment. That is directly the
draft's `in_reply_to`: no need to list the comments again.

`seen.json` / `daemon-seen.json` (key → last time alive) keep the keys of changes gone from both queries
(merged, abandoned) for 30 days: a change restored within that window does not replay its history.

**Relaunch the watcher in the background before ending the turn**, even while an approval is pending —
otherwise the next events are lost.

### 1. Qualify each touched change

| Signal | Action |
|---|---|
| zuul `Merge Failed` (every CI label -1 at the same second) | Stale base, no job ran → local rebase on `origin/<branch>` |
| `merge_conflict` | The target branch moved and the patch set no longer applies (Gerrit would say "Merge Conflict" without posting anything). The watcher runs a local `git merge-tree` against the branch tip (private refs `refs/gerrit-babysit/*`); when the project has `use_content_merge` off, like Gerrit it flags any file both sides changed, so `git rebase` often goes through with nothing to resolve. Emitted once per patch set → rebase on `origin/<branch>`, resolve `files`, run tests, then ask before pushing |
| `parent_merged` | The parent merged under another SHA (rebase on submit): the change will not merge as is, no need to wait for Merge Failed. The rebase is already prepared locally: read `rebase` below |
| `parent_updated` | The open parent got a new patch set and my change still sits on the old one. Same handling as `parent_merged` via `rebase`; emitted once per new parent revision |
| `ci_stuck` | CI "running" but nothing from zuul (no verdict, no "Starting") for 2 h on the current patch set, once per patch set. Read `zuul_queue`: a list of `{pipeline, enqueued_at, remaining_s, jobs_waiting, jobs_running}` = the change is queued, report only, **no** recheck. `[]` = zuul lost the change → offer (`AskUserQuestion`) a `recheck`, public so only after "yes". `{"error": …}` = open `zuul_status_url` by hand |
| `waiting_for_review` | Non-WIP patch set, CI red neither, not ready to submit, reminded every working day. Read from Gerrit's attention set of my change (REST): `waiting_on` = `[{name, username, working_days}]`, the reviewers holding it for ≥ 2 working days, even after a first round of comments — they are the ones to nudge. Never emitted while I am in the attention set myself (the ball is mine). `waiting_on: []` = nobody holds it and nobody reviewed the patch set for `working_days` ≥ 2: `dismissed` = `[{name, username, reason}]` lists who left the attention set since the upload without a word (saw it and passed) → suggest other reviewers rather than nudging them. When the REST call fails, both are `null` and only the old count remains (no comment nor vote from a reviewer for ≥ 2 working days). If `reviewers` is empty or they are away: suggest reviewers. Offer (`AskUserQuestion`) **Add reviewers** / **Nudge** / **Nothing**. Nudge per reviewer, not per change: when `nudges` gives a reviewer several changes, draft **one** message listing them all (to send wherever the user wants, e.g. chat) instead of a comment on each. The first two are public, so only after "yes" |
| Failed zuul job | Start from `ci_diagnosis` (one item per failed job: `category`, `failure` = Gradle's "What went wrong" block, `log_tail` = the log's last lines before the failed zuul task when there is no such block, `lint_errors`, `file_comments`, `others_failing`, `job_history`, `log_url`, `resembles`). Per-category detail below. On `diagnosis_error`: read `<log_url>/job-output.txt` by hand (`curl -L --compressed`) |
| Clear, local inline comment | Fix it |
| Question, design disagreement, ambiguous request, out of the change's scope | **Do not touch the code** — draft a reply |
| Bare -1/-2 vote without a comment | Report, nothing to do |
| +2 while CI has not finished | Report; `ready_to_submit` will come once everything is green |

**`rebase`** (session watcher only; the daemon touches no worktree) = what the watcher already did in the
change's worktree. It is never pushed.

| `status` | Action |
|---|---|
| `rebased` | Done locally (`onto`, `previous_head`, `head`, `commits` replayed). Check the Change-Ids, run the tests and the pre-push checks (step 3), show `git diff <previous_head> HEAD --stat`, then ask (**Push** / **Undo**: `git reset --hard <previous_head>` / **Ignore**) |
| `conflict` | Aborted, worktree untouched. Rerun `command` from `worktree`, resolve `files`, `git rebase --continue`, tests and pre-push checks, then ask |
| `up_to_date` | Already on the new base (rebased by hand): report only |
| `busy` | Uncommitted edits or an operation in progress: left alone. Report, rebase by hand once it is clean |
| `diverged` | HEAD no longer contains `old_parent_sha` (rewritten by hand): do not guess, report and ask |
| `no_worktree` | No worktree holds the change: create one (step 2), then `git rebase --onto <new base> <old_parent_sha>` |
| `error` | `detail`: rebase by hand |

**`ci_diagnosis` categories** — `others_failing` lists the other changes where the same job failed within
±3 h. `job_history` sums up the job's ~200 latest builds across all changes: `failure_rate`, `retried` =
patch sets rerun after a failure and `retried_green` = those that went green. A high rate or green reruns
support the flaky hypothesis. `flaky_here` = `{week, month, last}` from the local flake memory
(`~/.cache/gerrit-babysit/flaky.json`, 90 days: a job that failed then went green on the **same** patch set
of one of my changes or reviews). `file_comments` = the job's robot comments (`zuul-file-comments.json`),
Error/Fatal levels only: `file`, `line`, `rule`, `message`; the message often has a `**Fix:**` line, follow
it first. The `screenshots` category (only when `screenshot_regression_marker` is set) applies only to the
job whose path is on the marker line: other failed jobs keep their own category.
Before any fix, check that `failure` / `lint_errors` / `file_comments` point at files the change touches.

**`rechecks` ≥ 1**: a recheck was already tried on this patch set and the job still fails. Do not offer
another one by default: diagnose by hand (`log_url`), comparing with the previous failure.

**`base_build`** (only when `periodic_build` is set) = health of the target branch, read on every poll
from its last 20 periodic builds: `result`, `end_time`, `log_url` of the latest, and when red `red_since`
(end of the streak's first failure), `failures` and `last_green` (`null` = red for longer than the
window). `{"error": …}` = zuul unreachable. `result: FAILURE` = **confirmed red base**: offer neither a
`recheck` (it would fail the same way) nor a fix outside the change. Report it with the link and "red
for 5 h", and wait for the base to be fixed. An old build or `SUCCESS` does not rule it out (newer
breakage): `others_failing` is then the hint.

**`base_red`** = a target branch went red: emitted **once per red streak** (keyed on `last_green`) with
the same fields plus `changes` = my open changes on it (snoozed ones left out). Report it once for all of
them instead of per change, then offer (`AskUserQuestion`, multiSelect, one option per change) to snooze
them until the base is green (`snooze.py <n> --base-green`, local so no approval rule). Their own red
verdicts then wait until the base recovers.

| `category` | Fix |
|---|---|
| `screenshots` | Re-record the affected modules' screenshots locally; never take the CI's golden archive unchecked |
| `dependency_guard` | Regenerate the dependency baseline in the worktree, amend in the same change |
| `lint` | Fix each `rule`; unknown rule: read the message, fix if it is clear |
| `compile` / `unit_tests` | Reproduce the Gradle task named in `failure` locally, fix. If the failing file is not in the change, the target branch probably moved → rebase |
| `infra` (POST_FAILURE, TIMED_OUT…) or `failure` unrelated to the change + non-empty `others_failing` | Flaky or red base, no code change. Offer (`AskUserQuestion`) to post `recheck` (or the team's variant from `LOCAL.md`) — public, so only after "yes" |
| `flaky` | An `infra`/`unknown` failure of a job that flaked ≥ 2 times this month (`flaky_here`). Say so ("flaky 4× this week") and offer the `recheck` — public, so only after "yes". The SwiftBar menu offers the same one-click recheck (with a confirmation dialog) |
| `unknown` | Open `log_url` and diagnose by hand. `resembles` (if any) = failures already diagnosed by hand whose normalized `failure`/`log_tail` looks like this one (`similarity` ≥ 0.5, closest first: `change`, `patch_set`, `job`, `cause`, `fix`). A **lead, never a verdict**: say "looks like #N, fixed by X", then check in the log that the cause really applies before reusing the fix |

**Remember a failure diagnosed by hand** (`unknown`, or `rechecks` ≥ 1), once its cause is established:
`python3 <skill>/known_failures.py record <change> <patch_set> <job> --log-url <log_url> --cause "…" --fix "…"`
(one line each). It stores the fingerprint of the same excerpt the diagnosis reads, in
`~/.cache/gerrit-babysit/known_failures.json` (180 days); the next look-alike failure carries `resembles`.
No argument lists the records, `forget <change> <patch_set> <job>` drops a wrong one. Local only, nothing
is published, so no approval is needed.

### 2. Move into the change's worktree

Never in the main checkout (another session may switch its branch).
`git worktree list` + `git log --all --grep="Change-Id: <id>"` to find the branch. If no worktree holds
it: create one (`git worktree add`), then `git review -d <n>` inside.

### 3. Fix and verify

- Follow the repository's CLAUDE.md and `LOCAL.md`.
- `git commit --amend --no-edit` (or amend keeping the `Change-Id` trailer **verbatim**); check with
  `git log -1 --format='%(trailers:key=Change-Id,valueonly)'`.
- Run at least the unit tests of the touched module, screenshot checks if the UI changes. A failure you
  cannot solve → say so, do not paper over it.
- **Pre-push checks**, after every rebase or amend and before step 4. They catch locally what would
  otherwise cost a red CI cycle:
  - **Deterministic CI checks** of the touched modules: dependency guard, lint, screenshot verification.
    The commands are the team's, in `LOCAL.md`; skip a check it does not list and say so. A failure
    fixable locally (regenerated baseline, lint rule, re-recorded screenshots) → fix, amend, rerun.
  - **Base**: `git fetch origin <branch>`. If the change has an open parent, HEAD must contain its
    current patch set (else rebase onto it, like `parent_updated`). `git merge-tree --write-tree
    origin/<branch> HEAD` must exit 0 (else rebase: the push would get Merge Failed). `base_build.result`
    `FAILURE` → the CI will go red whatever the change: say so in step 4.
- Write the replies as **Gerrit drafts** (private, so compatible with the absolute rule): a draft comment
  with `in_reply_to` = the thread's `reply_to`, `file`/`line` from the thread, `unresolved` false for
  "Done", true for an open discussion. First list my existing drafts on the change: if there are any,
  report them, since `publish-comments` would publish them too.

### 4. Present and ask

Per change: link, what triggered it (author + excerpt), diagnosis, `git diff HEAD@{1} --stat` + the key
points of the diff, test and pre-push check results (each check passed / fixed / skipped), text of the
drafts ("Done" for a fixed comment, an argued text otherwise; reviewable and editable in the Gerrit UI). Then **one `AskUserQuestion` question per change**
(at most 4 questions per call, 4 options per question):

- **Push + reply** — patch set and replies published in one go
- **Push only** — drafts stay private
- *(instead of the two above, when there is no amend — question, disagreement)* **Reply** — publish the
  drafts
- **Edit** — the user explains, redo step 3
- **Ignore** — leave the local amend and the drafts as they are, publish nothing

### 5. Publish (only after "yes")

From the worktree:

- **Push + reply**: `git push origin HEAD:refs/for/<branch>%publish-comments` — `git review` cannot pass
  that option. One notification for the reviewer, and the "Done"s arrive with the patch set that fixes
  them.
- **Push only**: `git review <branch>`.
- **Reply only**: publish the drafts.

After a push, check that the returned change number is the right one (otherwise the Change-Id was lost:
restore it and push again), then my draft list must be empty if the replies were meant to go.

## Submit

`ready_to_submit` = on the current patch set: Code-Review +2 without a negative vote, every `ci_labels`
label at +1 without -1, and no unresolved thread waiting for my answer. (Gerrit's `submitRecords` can say
`OK` on a change voted -1, hence this own computation.) Emitted only once the parent change (`dependsOn`)
is no longer open, once per patch set and per working day: an unsubmitted change comes back the next
morning (not on weekends). `ready_since` = timestamp of the +2, worth mentioning ("ready for 3 days").

`submit_blocked` replaces it when the votes are there but Gerrit's submit requirements (REST, Gerrit ≥ 3.5)
still refuse: `requirements` = the names of the unsatisfied ones, typically `Code-Owners` (code-owners plugin:
a file lacks an owner's approval). Once per patch set and set of requirements. Say what is missing; for
`Code-Owners`, list the files without an approving owner and who can approve them (the Gerrit MCP code-owner
tools, or `/changes/<n>/revisions/current/code_owners.status` in REST), then offer (`AskUserQuestion`) to add
one as reviewer: public, so only after "yes". No submit offer until `ready_to_submit` comes. Unreadable
requirements (REST down) leave the votes to decide alone, as before.

1. **Re-check right before**: change still open, same patch set, no unresolved thread, no parent change
   still open. If something blocks, say so instead of offering the submit.
2. **Ask** (`AskUserQuestion`, never an implicit submit): **Submit** / **Not now**. Recall the change's
   subject and who gave the +2.
3. **Submit** after "yes": `ssh -p <ssh_port> <user>@<gerrit_host> gerrit review --submit <n>,<patch_set>`
   (user = `gerrit_user`, else `$GERRIT_USER`, else `git config gitreview.username`, else `whoami`, like
   `watch.py`). Then confirm `status: MERGED`.
4. **Submit failure** (Gerrit rebases on submit, a conflict makes it fail): local rebase from the
   worktree, tests, push again after approval; the new patch set goes through CI again and will trigger
   `ready_to_submit` again if the +2 is kept.

"Not now": do nothing — the event comes back on the next working day.

## Cleanup

`cleanup_candidate` = local branch whose tip is **exactly** a pushed patch set of one of my merged or
abandoned changes (`status` = `merged` / `abandoned`; a local amend made afterwards is not a candidate), with
its worktree if it is clean and not locked. An abandoned change can still be restored, but its patch sets
stay on Gerrit (`git review -d <n>` brings it back). `protected_branches` and the main checkout's branch are
excluded. The watcher is read-only: it deletes nothing.

1. Group all the candidates of the wake-up in **one** `AskUserQuestion` question (`multiSelect: true`,
   one option per branch: change + subject + `status` + worktree path).
2. For each checked candidate, from the main checkout: `git worktree remove <worktree>` (if any), then
   `git branch -D <branch>`. If `git worktree remove` fails (new files meanwhile), do not force: report it.
3. Unchecked: nothing — the event does not come back until the branch tip changes.

## Other people's reviews

Query `reviewer:self status:open -owner:self`. Not my code: **report only**, never a fix, a draft or a
vote without an explicit request.

- `review_requested`: I was added to a non-WIP change with ≤ `max_reviewers` human reviewers (beyond that,
  it is a group addition). Comes back every working day until I vote or comment. Carries `current_ref`
  and `prereview` = `{path, done}`. If `done` is false: start a background agent (fork) that runs
  `git fetch origin <current_ref>:refs/gerrit-babysit/prereview/<n>`, reviews read-only through
  `git show` / `git diff` (no build, no worktree, no draft, no vote), writes its pre-review as Markdown to
  `path`, then deletes the ref (`git update-ref -d`). If `done` is true: read the file and sum up its
  points. This pre-review stays local, nothing is published.
- `review_new_patch_set`: new patch set on a change where I voted or commented (`since_patch_set`,
  `my_last_vote`) and where I have no vote on the current patch set — so also when my +2 was dropped. A
  vote Gerrit copied over (trivial rebase) triggers nothing. Carries `interdiff`: `stat` = `git diff
  --stat` between the two patch sets, limited to the change's files, and `rebased` (different bases:
  upstream changes on those same files may slip in).
- `review_reply`: after my last message (upload/rebase notices aside), the owner wrote, or another
  reviewer wrote while Gerrit put me in the change's attention set (query `attention:self`), typically
  because they answered one of my threads.

Present: link, owner, what changed (`interdiff.stat`; open a file's diff only to go into detail), and for
`review_reply` the threads concerned. Optionally offer a detailed review; what follows (vote, reply) is
decided with the user.

## Snooze

"Snooze 12345 until Monday / until the next patch set": `python3 <skill>/snooze.py <n> --until YYYY-MM-DD`
(morning of that day), `--days N` (a weekend lands on Monday), `--patch-set <current>` (wakes on a newer one)
or `--base-green` (wakes once the target branch's periodic build passes; a zuul outage keeps it asleep);
`--clear` wakes it, no argument lists them. The SwiftBar menu has the same Snooze submenu ("Until the base is
green" only on a red base), a "Snoozed" section and a line per red branch linking to its failing build. A snoozed change leaves the counts, the status line and the notifications.

## Doctor

"Is babysit OK?", a `status: error`, a `threads_error` or a daemon that seems silent:
`python3 <skill>/doctor.py`. Read-only; it prints JSON and exits 1 when SSH or HTTP fails:
`config` (path, host, user, `repo_ok`), `ssh` / `http` (`ok`, `detail`; `http` also has `source` =
`gerrit_mcp_config` or `netrc`, and fails when the password belongs to another account than `ssh_user`),
`zuul`, `daemon` (`running`, `last_poll_age_s`, `error`), `state` (each cache file: `bytes`, `entries`,
`age_s`, `corrupt`) and `prunable`: crashed temp files, prereviews of closed changes or of an old patch set,
the private `refs/gerrit-babysit/changes|review/<n>` of closed changes, snoozes of closed changes. Summarize
what is broken and how to fix it. `--prune` deletes what `prunable` lists, local only, so no "yes" needed
beyond the user's ask; while Gerrit is unreachable it only drops the temp files.

## Watcher tests

After any change to a `.py` file of the skill (including `macos/gerrit.30s.py`): `cd <skill> && python3 -m unittest`
(stdlib only, anonymized Gerrit/zuul fixtures in `fixtures/`), then restart the LaunchAgent if installed.

Our own pushes and replies are filtered by the watcher (author = me), so there is no loop; the zuul
verdict that follows a push wakes the loop only if it fails.
