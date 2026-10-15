#!/bin/bash
# What launchd runs as com.evidwell.llama-server (installed by manage.sh): free
# the GPU, then exec llama-server so launchd supervises the server process
# itself — KeepAlive restarts it when it exits, and the watchdog's
# `launchctl kickstart -k` lands here again.
#
# Bonsai cannot share the GPU with an Ollama language model: loading beside one
# leaves llama-server failing every request with "Compute error". The pipeline
# client unloads them before each call; this covers a start or a restart, which
# is exactly when a stray one (an eval judge, a manual `ollama run`) is likely.
set -u

BONSAI_DIR="${BONSAI_DIR:-$HOME/bonsai}"
MODEL="${BONSAI_MODEL:-Ternary-Bonsai-2-27B-PTQ1_0.gguf}"
PORT="${LLAMACPP_PORT:-8080}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
# Ollama models allowed to stay resident beside Bonsai: the embedding model
# (0.7 GB) fits, a language model does not.
KEEP_OLLAMA_MODELS="${KEEP_OLLAMA_MODELS:-mxbai-embed-large}"

log() { printf '%s run.sh: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

unload_ollama_language_models() {
  local name base keep k
  # Ollama not running (at login it may start after us) is fine: nothing to unload.
  for name in $(curl -s -m 5 "$OLLAMA_URL/api/ps" | jq -r '.models[]?.name' 2>/dev/null); do
    base="${name%:latest}"
    keep=no
    for k in $KEEP_OLLAMA_MODELS; do
      [ "$base" = "${k%:latest}" ] && keep=yes
    done
    [ "$keep" = yes ] && continue
    log "unloading Ollama model $name to make room on the GPU"
    curl -s -m 60 "$OLLAMA_URL/api/generate" -d "{\"model\":\"$name\",\"keep_alive\":0}" >/dev/null || true
  done
}

shopt -s nullglob
servers=("$BONSAI_DIR"/llama-prism-*/llama-server)
if [ ${#servers[@]} -eq 0 ]; then
  log "no llama-server under $BONSAI_DIR/llama-prism-*/ (PrismML's fork; stock llama.cpp cannot load PTQ1_0)"
  exit 1
fi
server="${servers[${#servers[@]}-1]}"
if [ ! -f "$BONSAI_DIR/$MODEL" ]; then
  log "no model at $BONSAI_DIR/$MODEL"
  exit 1
fi

cd "$BONSAI_DIR" || exit 1
unload_ollama_language_models
log "starting $server on port $PORT"
exec "$server" -m "$MODEL" -c 32768 -ngl 99 --port "$PORT" --jinja
