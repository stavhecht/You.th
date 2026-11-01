#!/bin/bash
# Run every two minutes by launchd as com.evidwell.llama-server.watchdog
# (installed by manage.sh).
#
# llama-server can be running, answer /health with "ok", and fail every
# completion: after a GPU out-of-memory it returns "Compute error" (or an empty
# answer) until it is restarted. The worker's heartbeat cannot see that — the
# worker is alive and beating, each run just fails retryably until its attempts
# run out — and the worker cannot restart a host process from inside Docker.
#
# So this asks for a real four-token completion. After FAILURES_BEFORE_RESTART
# failed checks in a row it restarts the server through launchd, which runs
# run.sh (unload Ollama's language models, start llama-server) again. Two
# checks two minutes apart recover a broken server within ~4 minutes, inside
# the worker's retry schedule (60 s, then 5 min), so the run that hit the
# failure usually still succeeds on its third attempt.
set -u

LABEL="${SERVER_LABEL:-com.evidwell.llama-server}"
URL="${LLAMACPP_URL:-http://127.0.0.1:8080}"
LOG_DIR="${LOG_DIR:-$HOME/Library/Logs/evidwell}"
FAILURES_BEFORE_RESTART="${FAILURES_BEFORE_RESTART:-2}"
# Warm, the probe answers in ~1 s. The first one after a long idle took 23 s
# (2026-10-15) while macOS paged the weights back in.
PROBE_TIMEOUT="${PROBE_TIMEOUT:-120}"
# A server that a restart does not fix (model file gone, GPU held by something
# else) is not restarted more often than this; it is retried as restarts age out.
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-3}"
SERVER_LOG_MAX_BYTES=$((20 * 1024 * 1024))

FAILURES="$LOG_DIR/watchdog.failures"
RESTARTS="$LOG_DIR/watchdog.restarts"
GAVE_UP="$LOG_DIR/watchdog.gave-up"
DOMAIN="gui/$(id -u)"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
notify() {
  osascript -e "display notification \"$1\" with title \"llama-server watchdog\"" >/dev/null 2>&1 || true
}

# launchd appends to the server's log for as long as the job lives. Truncating
# in place keeps the open descriptor valid; the previous tail is kept beside it.
trim_server_log() {
  local f="$LOG_DIR/llama-server.log"
  [ -f "$f" ] || return 0
  if [ "$(stat -f %z "$f")" -gt "$SERVER_LOG_MAX_BYTES" ]; then
    tail -c 5000000 "$f" >"$f.1" && : >"$f"
  fi
}

mkdir -p "$LOG_DIR"
trim_server_log

# Stopped on purpose (`manage.sh stop`, e.g. to run the eval judge on Ollama):
# nothing to watch, and nothing to start.
if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  rm -f "$FAILURES"
  exit 0
fi

body=$(mktemp)
trap 'rm -f "$body"' EXIT
code=$(curl -s -m "$PROBE_TIMEOUT" -o "$body" -w '%{http_code}' "$URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say OK."}],"max_tokens":4,"reasoning_effort":"none","chat_template_kwargs":{"reasoning_effort":"none"}}')
rc=$?

reason=""
if [ $rc -eq 0 ] && [ "$code" = 200 ]; then
  if [ -z "$(jq -r '.choices[0].message.content // ""' "$body" 2>/dev/null)" ]; then
    reason="HTTP 200 with an empty answer (the GPU out-of-memory failure)"
  fi
elif [ $rc -eq 0 ] && [ "$code" = 503 ] && grep -qi loading "$body"; then
  exit 0 # still loading the model after a start; not a failure
elif [ $rc -eq 28 ]; then
  # Four slots, so a probe normally runs beside an article; but a long prefill
  # can hold it up. A slow server that is generating is not a broken one.
  if curl -s -m 5 "$URL/slots" | jq -e 'any(.[]; .is_processing)' >/dev/null 2>&1; then
    exit 0
  fi
  reason="no answer within ${PROBE_TIMEOUT}s, with no request in progress"
elif [ $rc -eq 7 ]; then
  reason="nothing listening at $URL"
elif [ $rc -ne 0 ]; then
  reason="curl failed with exit code $rc"
else
  reason="HTTP $code: $(head -c 200 "$body" | tr '\n' ' ')"
fi

if [ -z "$reason" ]; then
  if [ -f "$FAILURES" ] || [ -f "$GAVE_UP" ]; then
    log "healthy again"
  fi
  rm -f "$FAILURES" "$GAVE_UP"
  exit 0
fi

failures=$(($(cat "$FAILURES" 2>/dev/null || echo 0) + 1))
echo "$failures" >"$FAILURES"
log "check failed ($failures of $FAILURES_BEFORE_RESTART): $reason"
[ "$failures" -lt "$FAILURES_BEFORE_RESTART" ] && exit 0

now=$(date +%s)
awk -v now="$now" 'now - $1 < 3600' "$RESTARTS" 2>/dev/null >"$RESTARTS.tmp"
mv "$RESTARTS.tmp" "$RESTARTS"
if [ "$(wc -l <"$RESTARTS" | tr -d ' ')" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
  if [ ! -f "$GAVE_UP" ]; then
    touch "$GAVE_UP"
    log "not restarting: $MAX_RESTARTS_PER_HOUR restarts in the last hour did not fix it; see $LOG_DIR/llama-server.log"
    notify "llama-server is still failing after $MAX_RESTARTS_PER_HOUR restarts. See ~/Library/Logs/evidwell."
  fi
  exit 0
fi

log "restarting llama-server"
notify "llama-server stopped answering ($reason). Restarting it."
echo "$now" >>"$RESTARTS"
rm -f "$FAILURES" "$GAVE_UP"
launchctl kickstart -k "$DOMAIN/$LABEL"
