# Adapting gerrit-babysit to a new environment

Written for a coding agent (Claude Code or similar) asked to install this skill for its user. Work
through the steps in order, check each one, and ask the user whenever a value below cannot be discovered.
Never publish anything on Gerrit while setting up.

## 1. Install location

**As a plugin** (every repository, Stop hook included):

```sh
claude plugin marketplace add AsueG/gerrit-babysit
claude plugin install gerrit-babysit@gerrit-babysit
```

The skill is then `/gerrit-babysit:gerrit-babysit` and `<skill>` is the plugin folder, which updates replace:
put `config.json` and `LOCAL.md` in `~/.config/gerrit-babysit/`. The watched repository is the Claude
Code project the session runs in; set `repo` anyway if the macOS extras are wanted (section 5). Skip the
Stop hook of section 4, and do not also clone the skill, or the hook runs twice.

**As a cloned skill**, it expects to live in `<repo>/.claude/skills/gerrit-babysit`, where `<repo>` is the git checkout
whose Gerrit changes are watched:

```sh
git clone https://github.com/AsueG/gerrit-babysit <repo>/.claude/skills/gerrit-babysit
```

Keep the clone out of the host repository's history: add `.claude/skills/gerrit-babysit/` to
`<repo>/.git/info/exclude` (personal) rather than to the tracked `.gitignore`, unless the team adopts the
skill. Updating is then `git -C <repo>/.claude/skills/gerrit-babysit pull`.

Installed elsewhere (e.g. `~/.claude/skills/`), set `repo` in `config.json` to the checkout's path.

## 2. `config.json`

Copy `config.example.json` to `config.json` in the skill folder (git-ignored), or to
`~/.config/gerrit-babysit/` for a plugin install. The file is looked up in
this order: `$GERRIT_BABYSIT_CONFIG`, `<skill>/config.json`, `~/.config/gerrit-babysit/config.json`.
Missing keys take the defaults of `config.py`.

| Key | Default | How to find it |
|---|---|---|
| `gerrit_host` | — (required) | Host of the `origin` remote or of `.gitreview` |
| `ssh_port` | `29418` | `.gitreview`, or the SSH remote URL |
| `gerrit_user` | `$GERRIT_USER`, `git config gitreview.username`, `whoami` | Check with `ssh -p <port> <user>@<host> gerrit version` |
| `repo` | three levels above the skill; else the Claude Code project | When the skill is not inside the repo; always for the macOS extras then |
| `gerrit_mcp_config` | `null` → `~/.netrc` | Path to a gerrit-mcp-server `gerrit_config.json` if the user has one; else a `machine <host> login <user> password <HTTP password>` line in `~/.netrc` (mode 600) |
| `ci_user` | `"zuul"` | Username that posts CI verdicts (look at a change's messages) |
| `ci_labels` | `["Verified"]` | Labels CI votes on (`gerrit query --format=JSON --all-approvals change:<n>`) |
| `bot_users` | `[]` | Other automated accounts whose messages and votes mean nothing to the user |
| `zuul_api` | `null` (no CI diagnosis) | `https://<zuul>/api/tenant/<tenant>`: the build links in CI messages point to `https://<zuul>/t/<tenant>/build/<uuid>` |
| `zuul_status_url` | `null` | `https://<zuul>/t/<tenant>/status` |
| `periodic_build` | `null` | `{"pipeline", "job"}` of a periodic build of the target branch, used to spot a red base |
| `screenshot_regression_marker` | `null` | Text on the line of a CI message that reports screenshot regressions |
| `recheck_comment` | `"recheck"` | Comment the SwiftBar menu posts to rerun CI on a known flake (`recheck` or `recheck-<pipeline>`) |
| `protected_branches` | `["main", "master"]` | Long-lived branches never offered for cleanup |
| `review_dashboard_url` | `https://<host>/dashboard/self` | Link at the bottom of the SwiftBar menu |
| `max_reviewers` | `10` | Above this many human reviewers, an addition is a group one, not a review request |
| `work_hours` | `[9, 19]` | Local hours when the daemon notifies (Mon–Fri) |
| `launchd_label` | `"local.gerrit-babysit"` | LaunchAgent label |
| `language` | `"auto"` | `"en"` or `"fr"`; `auto` follows the macOS language, then `$LANG` |

Check the result: `python3 <skill>/watch.py --pending` must print `{"status": "pending", ...}` with
`"threads_error": null`. A `threads_error` means the HTTP credentials are wrong; an SSH error means the
host, port or user is.

Without zuul, leave the `zuul_*` and `periodic_build` keys at `null`: events still flow, only
`ci_diagnosis`, `base_build` and `zuul_queue` disappear. Another CI that votes on Gerrit works the same
way as long as `ci_user` and `ci_labels` match it.

## 3. Team overlay: `LOCAL.md`

`SKILL.md` stays generic. Everything specific to the user's team goes in `<skill>/LOCAL.md` (git-ignored;
`~/.config/gerrit-babysit/LOCAL.md` for a plugin install),
which the skill reads first and which wins over `SKILL.md`. Typical content:

- the exact build, test and screenshot commands, or the skills that hold them;
- the team's `recheck` variants (e.g. rerun a single pipeline) and when to use each;
- fix recipes per `ci_diagnosis` category and per lint rule;
- the name of the target branch if it is not `main`, and files that must never be merged by hand;
- ownership rules (modules the user must not touch) and the language to use with the user.

Ask the user for these rather than guessing; start small and let it grow.

## 4. Claude Code wiring

- **Stop hook** (recommended, keeps the watcher alive; the plugin already ships it): in
  `<repo>/.claude/settings.local.json`,

  ```json
  {"hooks": {"Stop": [{"hooks": [{"type": "command", "timeout": 10,
    "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/skills/gerrit-babysit/stop_hook.py"}]}]}}
  ```

  Merge it with existing settings, never replace them.
- **Status line** (optional): call `python3 <skill>/statusline_segment.py` from the user's status line
  script and append its output (e.g. `⎇ 3 · 1⚠ · 1✓`). It only reads the snapshot, never Gerrit.
- **Gerrit MCP server** (optional): the skill mentions draft comments, publishing drafts and suggesting
  reviewers. With a Gerrit MCP server those are tool calls; without one, use the Gerrit REST API with the
  same HTTP credentials, or ask the user to do it in the UI.

## 5. macOS extras (optional)

`macos/install.sh` builds the `GerritBabysit.app` applet, installs the LaunchAgent (`watch.py --daemon`:
snapshot + notifications even without a Claude session) and links the SwiftBar plugin when SwiftBar is
configured. Rerun it after moving the skill, changing `launchd_label` or updating the plugin (the plugin
folder's path changes with each version). Outside `<repo>/.claude/skills`, it requires `repo`. Notifications go through
SwiftBar (`swiftbar://notify`); without it, the daemon still keeps the snapshot fresh.

`macos/open-babysit.sh` focuses the running `/gerrit-babysit` session or opens one, in an Orca
terminal tab when the `orca` CLI is installed, else in Terminal.app.

On Linux, run `watch.py --daemon` from a systemd user service instead; the SwiftBar and applet parts do
not apply.

## 6. Verify

```sh
cd <skill> && python3 -m unittest
```

The suite uses its own `fixtures/config.json`, so it does not depend on the user's settings.
