#!/bin/zsh
# Optional macOS extras: the background daemon (LaunchAgent), the SwiftBar menu and the "open Claude" applet.
# Idempotent: rerun it after moving the skill or changing launchd_label.
set -euo pipefail
SKILL=${0:A:h:h}
PY=/usr/bin/python3
setting() { $PY -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.CONFIG[sys.argv[2]] or "")' "$SKILL" "$1"; }

[[ -n $(setting gerrit_host) ]] || { echo "Set gerrit_host in config.json first (see config.example.json)." >&2; exit 1; }
# launchd starts the daemon and the applet from /, so outside <repo>/.claude/skills the checkout must be named.
if [[ -z $(setting repo) ]] && ! $PY -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; sys.exit(not config.inside_repo())' "$SKILL"; then
  echo "Set repo in config.json to the git checkout to watch (the skill is not inside it)." >&2; exit 1
fi

LABEL=$(setting launchd_label)
CACHE=$HOME/.cache/gerrit-babysit
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
mkdir -p "$CACHE" "${PLIST:h}"

osacompile -o "$SKILL/GerritBabysit.app" -e "do shell script quoted form of \"$SKILL/macos/open-babysit.sh\" & \" > /dev/null 2>&1 &\""
echo "applet: $SKILL/GerritBabysit.app"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$SKILL/watch.py</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>60</integer>
    <key>ProcessType</key><string>Background</string>
    <key>StandardErrorPath</key><string>$CACHE/daemon.log</string>
    <key>StandardOutPath</key><string>$CACHE/daemon.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "daemon: $LABEL (log: $CACHE/daemon.log)"

PLUGINS=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)
if [[ -n $PLUGINS ]]; then
  ln -sf "$SKILL/macos/gerrit.30s.py" "${PLUGINS/#\~/$HOME}/gerrit.30s.py"
  echo "SwiftBar plugin linked into $PLUGINS"
else
  echo "SwiftBar not configured: skipped the menu bar plugin."
fi
