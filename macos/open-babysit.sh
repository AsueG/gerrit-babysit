#!/bin/zsh
# Focuses the running /gerrit-babysit session if there is one; otherwise opens a new one in an Orca tab,
# falling back to Terminal.app if Orca is not installed or unreachable.
# Launched from a LaunchAgent/applet with a bare PATH, hence the fallbacks.
SKILL=${0:A:h:h}
REPO=$(/usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.REPO)' "$SKILL")
CLAUDE=${commands[claude]:-$HOME/.local/bin/claude}
ORCA=${commands[orca]:-/opt/homebrew/bin/orca}
LOCK=$HOME/.cache/gerrit-babysit/session.json

has_orca() { [[ -x $ORCA ]] && open -Ra Orca 2>/dev/null; }

if [[ -f $LOCK ]]; then
  pid=$(plutil -extract claude_pid raw -o - "$LOCK" 2>/dev/null)
  terminal=$(plutil -extract orca_terminal raw -o - "$LOCK" 2>/dev/null)
  if [[ -n $pid && "$(ps -o comm= -p "$pid" 2>/dev/null)" == *claude ]]; then
    if [[ -n $terminal ]] && has_orca; then
      open -a Orca
      $ORCA terminal switch --terminal "$terminal" >/dev/null 2>&1 && exit 0
    else
      osascript -e 'tell application "Terminal" to activate' && exit 0
    fi
  fi
fi

if has_orca; then
  open -a Orca
  $ORCA terminal create --worktree "path:$REPO" --title "gerrit-babysit" \
      --command "${(q)CLAUDE} /gerrit-babysit" --focus >/dev/null 2>&1 && exit 0
fi
# Paths go in as argv, never spliced into the script: a quote in them cannot break or inject AppleScript.
osascript -e 'on run argv' \
    -e 'tell application "Terminal" to do script "cd " & quoted form of item 1 of argv & " && " & quoted form of item 2 of argv & " /gerrit-babysit"' \
    -e 'tell application "Terminal" to activate' \
    -e 'end run' "$REPO" "$CLAUDE"
