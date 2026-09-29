# gerrit-babysit

A [Claude Code](https://claude.com/claude-code) skill that babysits your Gerrit changes.

A small Python watcher polls Gerrit over SSH and costs no tokens while nothing happens. Claude wakes up
only for something actionable:

- a reviewer comment or a Code-Review -1;
- a CI failure, already sorted by cause: compile, unit tests, lint, screenshots, dependency guard, infra
  or flaky;
- a merge conflict, a "Merge Failed", a parent merged under another SHA, or CI stuck for hours;
- a change that is ready to submit, or has waited too long for a review;
- someone asking for your review, pushing a new patch set, or replying to your threads;
- local branches of merged changes that can be cleaned up.

For your own changes, Claude prepares the fix in the change's worktree, runs the tests, drafts the
replies as private Gerrit drafts, and then asks you. **Nothing visible to others happens without your
explicit "yes"**: no push, published comment, vote, recheck or submit.

On macOS you can add:

- a background daemon that sends notifications, even with no Claude session open;
- a SwiftBar menu bar item listing your changes, with a guarded one-click submit;
- a Claude Code status line segment.

The interface follows the macOS language (English or French).

## Requirements

- Python 3.9+ (standard library only) and git
- SSH access to Gerrit (`ssh -p 29418 <user>@<host> gerrit version`)
- A Gerrit HTTP password, in `~/.netrc` or in a gerrit-mcp-server config, to read comment threads
- Optional: zuul, for CI diagnosis
- Optional: SwiftBar, for the menu bar item

## Install

```sh
git clone https://github.com/AsueG/gerrit-babysit <repo>/.claude/skills/gerrit-babysit
cd <repo>/.claude/skills/gerrit-babysit
cp config.example.json config.json   # then edit: gerrit_host, ci_labels, zuul_api…
python3 watch.py --pending           # sanity check
macos/install.sh                     # optional: daemon, SwiftBar, applet
```

Then run `/gerrit-babysit` in Claude Code from `<repo>`.

[ADAPTING.md](ADAPTING.md) covers every setting, the Stop hook and the status line. It is written so you
can hand it to your agent: *"install gerrit-babysit following ADAPTING.md"*.

## Team conventions

`SKILL.md` stays generic. Put your team's specifics in a git-ignored `LOCAL.md` next to it: build
commands, recheck variants, fix recipes, ownership rules. The skill reads it first. That keeps your
clone free of local edits, so updating is just `git pull`.

## How it works

| File | Role |
|---|---|
| `SKILL.md` | What Claude does with each event |
| `watch.py` | Polls Gerrit, turns the state into events, prints them as JSON and exits (session mode) or notifies (`--daemon`) |
| `ci.py` | Zuul failure diagnosis: failing jobs, gradle/lint errors, category, flakiness hints |
| `config.py` | Settings and FR/EN strings |
| `stop_hook.py` | Claude Code Stop hook that keeps the watcher running during a babysit session |
| `statusline_segment.py` | Status line segment read from the snapshot |
| `macos/` | LaunchAgent installer, SwiftBar plugin, session launcher |

State lives in `~/.cache/gerrit-babysit/`. Run the tests with `python3 -m unittest`.

## License

[MIT](LICENSE)
