#!/bin/bash
# Run llama-server (Bonsai, LLM_PROVIDER=llamacpp) under launchd, with its
# watchdog. See README.md.
#
#   ./manage.sh install     write both LaunchAgents and start them (and at every login)
#   ./manage.sh status      what launchd says, a live probe, the watchdog's last lines
#   ./manage.sh stop        stop the server until `start` or the next login
#   ./manage.sh start
#   ./manage.sh uninstall   stop both and remove the LaunchAgents
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER=com.evidwell.llama-server
WATCHDOG=com.evidwell.llama-server.watchdog
AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/evidwell"
URL="http://127.0.0.1:8080"
DOMAIN="gui/$(id -u)"

loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

plist() { # label, script, extra keys
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$HERE/$2</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string></dict>
$3
</dict>
</plist>
EOF
}

install() {
  mkdir -p "$AGENTS" "$LOG_DIR"
  plist "$SERVER" run.sh "  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/llama-server.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/llama-server.log</string>" >"$AGENTS/$SERVER.plist"
  plist "$WATCHDOG" watchdog.sh "  <key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/watchdog.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/watchdog.log</string>" >"$AGENTS/$WATCHDOG.plist"
  plutil -lint "$AGENTS/$SERVER.plist" "$AGENTS/$WATCHDOG.plist" >/dev/null

  for label in "$WATCHDOG" "$SERVER"; do
    loaded "$label" && launchctl bootout "$DOMAIN/$label" || true
  done
  # A server started by hand holds port 8080; launchd's would crash-loop on bind.
  if pkill -f 'llama-prism-.*/llama-server' 2>/dev/null; then
    echo "stopped a llama-server that was not started by launchd"
    while pgrep -f 'llama-prism-.*/llama-server' >/dev/null; do sleep 1; done
  fi
  launchctl bootstrap "$DOMAIN" "$AGENTS/$SERVER.plist"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$WATCHDOG.plist"
  echo "installed; logs in $LOG_DIR. The model takes ~30 s to load."
}

uninstall() {
  for label in "$WATCHDOG" "$SERVER"; do
    loaded "$label" && launchctl bootout "$DOMAIN/$label" || true
    rm -f "$AGENTS/$label.plist"
  done
  echo "uninstalled"
}

start() {
  [ -f "$AGENTS/$SERVER.plist" ] || { echo "not installed; run ./manage.sh install" >&2; exit 1; }
  loaded "$SERVER" || launchctl bootstrap "$DOMAIN" "$AGENTS/$SERVER.plist"
  echo "started"
}

stop() {
  # The watchdog sees the job unloaded and leaves it alone.
  loaded "$SERVER" && launchctl bootout "$DOMAIN/$SERVER" || true
  echo "stopped until ./manage.sh start or the next login"
}

status() {
  for label in "$SERVER" "$WATCHDOG"; do
    if loaded "$label"; then
      echo "$label:"
      launchctl print "$DOMAIN/$label" | grep -E $'^\t(state|pid|runs|last exit code) =' | sed 's/^[[:space:]]*/  /'
    else
      echo "$label: not loaded"
    fi
  done
  printf 'probe: '
  curl -s -m 120 -w ' (%{time_total}s)\n' "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"Say OK."}],"max_tokens":4,"reasoning_effort":"none","chat_template_kwargs":{"reasoning_effort":"none"}}' |
    sed -E 's/.*"content":"([^"]*)".*\(/answered "\1" (/' || echo "no answer"
  if [ -f "$LOG_DIR/watchdog.log" ]; then
    echo "watchdog.log (last 5):"
    tail -5 "$LOG_DIR/watchdog.log" | sed 's/^/  /'
  fi
}

case "${1:-}" in
  install | uninstall | start | stop | status) "$1" ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' && exit 2 ;;
esac
