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
   `base_build` when CI is red). State events follow: `merge_conflict`, `parent_merged`, `ci_stuck`,
   `ready_to_submit`, `waiting_for_review`, `cleanup_candidate`, and the `review_*` ones
   (`review_requested` where I already voted or commented are left out). Handle each item with the steps
   below.
2. **Start the watcher** with `Bash` and `run_in_background: true`: `python3 <skill>/watch.py`.
   End the turn. The process exit notification is the wake-up — never poll its output.

The watcher writes `~/.cache/gerrit-babysit/session.json` (PID of the `claude` process +
`ORCA_TERMINAL_HANDLE`). Only `macos/open-babysit.sh` reads it, to focus this session instead of opening
a second one.

Stop (`/gerrit-babysit stop` or "stop watching"): `TaskStop` on the watcher task, then
`rm ~/.cache/gerrit-babysit/session.json`.

If the `Stop` hook (`stop_hook.py`) is installed, it refuses to end a turn of this session while no session
`watch.py` (neither `--daemon` nor `--pending`) runs under its `claude` process. Other sessions are left
alone, and it blocks only once per turn so a failing relaunch cannot loop. To stop on purpose (stop,
`status: error`), delete `session.json` **before** ending the turn.

The snapshot `~/.cache/gerrit-babysit/status.json` (per change: votes, `ci` =
`running`/`passed`/`failed`/`stale_base` (Merge Failed), `ci_failed`, conflict, `open_parent`, worktree,
patch set; plus `last_attempt`/`last_error`) feeds the status line segment and the SwiftBar menu.

When installed (`macos/install.sh`), a LaunchAgent runs `watch.py --daemon` permanently: fresh snapshot +
one macOS notification per new event, with its own state (`daemon-seen.json`) and SSH failures logged to
`daemon.log`. It also publishes its latest poll to `daemon-poll.json`: while it is alive (< 6 min), the
session watcher reuses it instead of querying Gerrit and fetching again (only the cleanup query stays in
the session). No notifications outside working hours (`work_hours`, Mon–Fri): events stay unseen and
notify at the start of the next working day. Past 3 notifications in one poll (typically that morning
catch-up), a single summary replaces them. The daemon fixes nothing: clicking opens the change in Gerrit.
After editing `watch.py`, restart it: `launchctl kickstart -k gui/$(id -u)/<launchd_label>`.

## On each wake-up

The output is `{"status": "events", "events": [...]}`. After the first new event, the watcher waits 90 s
more and delivers everything that arrived meanwhile in a single wake-up (reply + vote + zuul verdict).
Each item has a `kind`:
`message` (comment/vote/zuul verdict: `author`, `author_username`, text; a zuul failure also carries
`ci_diagnosis`, `base_build` and `rechecks` = how many `recheck*` I already posted on this patch set
before this verdict), `merge_conflict` (+ `files`), `parent_merged` (+ `parent`, `old_parent_sha`,
`files`), `ci_stuck` (+ `idle_since`, `zuul_queue`), `waiting_for_review` (+ `working_days`,
`reviewers`), `ready_to_submit` ("Submit" section), `cleanup_candidate` ("Cleanup" section) or
`review_requested` / `review_new_patch_set` / `review_reply` ("Other people's reviews" section).
`status: error` = Gerrit SSH keeps failing → tell the user and stop.

`threads_error` (at the root of the output and in `status.json`) = the REST call for comment threads
failed (HTTP password expired or missing) while SSH works. Polling goes on: `threads_awaiting_me` is then
`null` (unknown, **not** "no threads"), `ready_to_submit` is held back and the status line shows
`threads ?`. Tell the user once (renew the HTTP password), without stopping.

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
| `merge_conflict` | The target branch moved and the patch set no longer applies (Gerrit would say "Merge Conflict" without posting anything). The watcher runs a local `git merge-tree` against the branch tip (private refs `refs/gerrit-babysit/*`). Emitted once per patch set → rebase on `origin/<branch>`, resolve `files`, run tests, then ask before pushing |
| `parent_merged` | The parent merged under another SHA (rebase on submit): the change will not merge as is, no need to wait for Merge Failed. `git rebase --onto origin/<branch> <old_parent_sha>` from the worktree, resolve `files` if any, check the Change-Ids, run tests, then ask before pushing |
| `ci_stuck` | CI "running" but nothing from zuul (no verdict, no "Starting") for 2 h on the current patch set, once per patch set. Read `zuul_queue`: a list of `{pipeline, enqueued_at, remaining_s, jobs_waiting, jobs_running}` = the change is queued, report only, **no** recheck. `[]` = zuul lost the change → offer (`AskUserQuestion`) a `recheck`, public so only after "yes". `{"error": …}` = open `zuul_status_url` by hand |
| `waiting_for_review` | Current non-WIP patch set, CI not red, no comment nor vote from a reviewer for `working_days` ≥ 2 working days (reminded every working day). If `reviewers` is empty or they are away: suggest reviewers. Offer (`AskUserQuestion`) **Add reviewers** / **Nudge** (draft message to publish) / **Nothing**. The first two are public, so only after "yes" |
| Failed zuul job | Start from `ci_diagnosis` (one item per failed job: `category`, `failure` = Gradle's "What went wrong" block, `lint_errors`, `file_comments`, `others_failing`, `job_history`, `log_url`). Per-category detail below. On `diagnosis_error`: read `<log_url>/job-output.txt` by hand (`curl -L --compressed`) |
| Clear, local inline comment | Fix it |
| Question, design disagreement, ambiguous request, out of the change's scope | **Do not touch the code** — draft a reply |
| Bare -1/-2 vote without a comment | Report, nothing to do |
| +2 while CI has not finished | Report; `ready_to_submit` will come once everything is green |

**`ci_diagnosis` categories** — `others_failing` lists the other changes where the same job failed within
±3 h. `job_history` sums up the job's ~200 latest builds across all changes: `failure_rate`, `retried` =
patch sets rerun after a failure and `retried_green` = those that went green. A high rate or green reruns
support the flaky hypothesis. `file_comments` = the job's robot comments (`zuul-file-comments.json`),
Error/Fatal levels only: `file`, `line`, `rule`, `message`; the message often has a `**Fix:**` line, follow
it first. The `screenshots` category (only when `screenshot_regression_marker` is set) applies only to the
job whose path is on the marker line: other failed jobs keep their own category.
Before any fix, check that `failure` / `lint_errors` / `file_comments` point at files the change touches.

**`rechecks` ≥ 1**: a recheck was already tried on this patch set and the job still fails. Do not offer
another one by default: diagnose by hand (`log_url`), comparing with the previous failure.

**`base_build`** (only when `periodic_build` is set) = latest periodic build of the target branch
(`result`, `end_time`, `log_url`). `result: FAILURE` = **confirmed red base**: offer neither a `recheck`
(it would fail the same way) nor a fix outside the change. Report it with the link and wait for the base
to be fixed. An old build or `SUCCESS` does not rule it out (newer breakage): `others_failing` is then the
hint.

| `category` | Fix |
|---|---|
| `screenshots` | Re-record the affected modules' screenshots locally; never take the CI's golden archive unchecked |
| `dependency_guard` | Regenerate the dependency baseline in the worktree, amend in the same change |
| `lint` | Fix each `rule`; unknown rule: read the message, fix if it is clear |
| `compile` / `unit_tests` | Reproduce the Gradle task named in `failure` locally, fix. If the failing file is not in the change, the target branch probably moved → rebase |
| `infra` (POST_FAILURE, TIMED_OUT…) or `failure` unrelated to the change + non-empty `others_failing` | Flaky or red base, no code change. Offer (`AskUserQuestion`) to post `recheck` (or the team's variant from `LOCAL.md`) — public, so only after "yes" |
| `unknown` | Open `log_url` and diagnose by hand |

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
- Write the replies as **Gerrit drafts** (private, so compatible with the absolute rule): a draft comment
  with `in_reply_to` = the thread's `reply_to`, `file`/`line` from the thread, `unresolved` false for
  "Done", true for an open discussion. First list my existing drafts on the change: if there are any,
  report them, since `publish-comments` would publish them too.

### 4. Present and ask

Per change: link, what triggered it (author + excerpt), diagnosis, `git diff HEAD@{1} --stat` + the key
points of the diff, test results, text of the drafts ("Done" for a fixed comment, an argued text
otherwise; reviewable and editable in the Gerrit UI). Then **one `AskUserQuestion` question per change**
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

`cleanup_candidate` = local branch whose tip is **exactly** a pushed patch set of one of my merged changes
(a local amend made after the merge is not a candidate), with its worktree if it is clean and not locked.
`protected_branches` and the main checkout's branch are excluded. The watcher is read-only: it deletes
nothing.

1. Group all the candidates of the wake-up in **one** `AskUserQuestion` question (`multiSelect: true`,
   one option per branch: change + subject + worktree path).
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

## Watcher tests

After any change to `watch.py`, `ci.py`, `config.py`, `stop_hook.py`, `statusline_segment.py` or
`macos/gerrit.30s.py`: `cd <skill> && python3 -m unittest`
(stdlib only, anonymized Gerrit/zuul fixtures in `fixtures/`), then restart the LaunchAgent if installed.

Our own pushes and replies are filtered by the watcher (author = me), so there is no loop; the zuul
verdict that follows a push wakes the loop only if it fails.
