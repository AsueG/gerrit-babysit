---
name: ship
description: Test, lint, commit only this session's hunks, push to GitHub, and redeploy the gerrit-babysit plugin, daemon and SwiftBar item
---
1. Run `git status` and `git diff`. Identify changes from other sessions and exclude them.
2. Run `pytest`, `ruff check --fix` and `ruff format` (via `uvx` if not on PATH; format only the files this session changed, since the repo is not ruff-formatted). Report any failure that already existed before this session's changes, separately from new ones.
3. Stage only this session's hunks. Match the commit style of `git log -5` (`feat:` / `fix:` / `docs:` / `chore:`).
4. Run `git push` (the remote is GitHub, there is no Gerrit review for this repo).
5. Release every ship that contains a `feat:` or `fix:` (otherwise the plugin cache keeps the old code under the same version): bump `version` in `.claude-plugin/plugin.json` (minor for `feat:`, patch for `fix:`), commit it alone as `chore: release X.Y.Z`, then `git tag vX.Y.Z` and `git push origin main vX.Y.Z` in the same step. The tag is not optional. Before finishing, check with `git ls-remote --tags origin` that every `chore: release` commit has its `vX.Y.Z` tag on origin, and tag and push any that are missing.
6. Deploy:
   - Refresh the plugin so the cache is not stale (`claude plugin marketplace update gerrit-babysit`, then `claude plugin update gerrit-babysit@gerrit-babysit`, or reinstall with `claude plugin install gerrit-babysit@gerrit-babysit`). Check that the installed version matches `plugin.json`.
   - Restart the daemon from the new install: `launchctl kickstart -k gui/$(id -u)/com.tiffany.gerrit-babysit`, then confirm with `launchctl print gui/$(id -u)/com.tiffany.gerrit-babysit` that it is running and points to the new install.
   - Refresh SwiftBar (`open -g "swiftbar://refreshallplugins"`) and run `~/.swiftbar/gerrit.30s.py` to confirm the new menu items show up.
7. If Gerrit, ArgoCD or Zuul is unreachable, check the VPN (GlobalProtect) and DNS first and say so explicitly, rather than retrying.
