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
USER_DIR=$HOME/.config/gerrit-babysit
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
mkdir -p "$CACHE" "$USER_DIR" "${PLIST:h}"

# Everything below goes through the launcher, which outlives plugin updates (each version has its own folder).
LAUNCH=$USER_DIR/launch.py
$PY -c 'import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text().replace("FALLBACK = \"\"", f"FALLBACK = {sys.argv[3]!r}", 1)
pathlib.Path(sys.argv[2]).write_text(source)' "$SKILL/macos/launch.py" "$LAUNCH" "$SKILL"
chmod +x "$LAUNCH"
echo "launcher: $LAUNCH"

osacompile -o "$USER_DIR/GerritBabysit.app" -e "do shell script quoted form of \"$PY\" & \" \" & quoted form of \"$LAUNCH\" & \" macos/open-babysit.sh > /dev/null 2>&1 &\""
echo "applet: $USER_DIR/GerritBabysit.app"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$LAUNCH</string>
        <string>watch.py</string>
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
# Paused from the menu = disabled in launchd: a reinstall keeps it paused rather than failing to bootstrap.
if launchctl print-disabled "gui/$(id -u)" | grep -Eq "\"$LABEL\" => (disabled|true)"; then
  echo "daemon: $LABEL paused from the menu, not started (resume it there)"
else
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "daemon: $LABEL (log: $CACHE/daemon.log)"
fi

$PY "$SKILL/pre_push.py" --install

PLUGINS=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)
if [[ -n $PLUGINS ]]; then
  STUB=${PLUGINS/#\~/$HOME}/gerrit.30s.py
  # Removed first: an older install left a symlink there, and writing through it would overwrite the plugin itself.
  rm -f "$STUB"
  # SwiftBar reads the plugin's settings from the `# <xbar…>` headers of the file it runs.
  { echo '#!/usr/bin/python3'
    grep '^# <' "$SKILL/macos/gerrit.30s.py"
    echo '# Written by macos/install.sh: runs the menu of the install Claude Code currently uses.'
    echo 'import os, sys'
    echo "os.execv(sys.executable, [sys.executable, '$LAUNCH', 'macos/gerrit.30s.py', *sys.argv[1:]])"
  } > "$STUB"
  chmod +x "$STUB"
  echo "SwiftBar plugin written to $PLUGINS"
else
  echo "SwiftBar not configured: skipped the menu bar plugin."
fi
