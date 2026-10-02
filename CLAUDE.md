# CLAUDE.md

## Deploy

After changing gerrit-babysit, do all of these:
- Reinstall or refresh the plugin so the cache is NOT stale.
- Restart the launchd daemon from the new install path.
- Confirm that SwiftBar shows the new menu items.

If Gerrit, ArgoCD or Zuul is unreachable, check the VPN (GlobalProtect) and DNS first and tell me, rather than retrying.
