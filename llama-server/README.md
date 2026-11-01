# llama-server under launchd, with a watchdog

`LLM_PROVIDER=llamacpp` sends every generative call to `llama-server` on the host
(Ternary-Bonsai-2-27B, PrismML's llama.cpp fork, in `~/bonsai`). This directory
keeps that server running without anyone watching it.

```bash
./manage.sh install     # both LaunchAgents; starts now and at every login
./manage.sh status      # launchd state, a live probe, the watchdog's last lines
./manage.sh stop        # until `start` or the next login — e.g. to run the eval judge
./manage.sh start
./manage.sh uninstall
```

Logs are in `~/Library/Logs/evidwell/`: `llama-server.log` (trimmed to its last 5 MB
past 20 MB) and `watchdog.log` (only failures, restarts and recoveries).

## What runs

- **`com.evidwell.llama-server`** runs `run.sh`: it unloads Ollama's language models
  (keeping `mxbai-embed-large`), then execs llama-server. `KeepAlive` restarts it
  whenever it exits; `RunAtLoad` starts it at login.
- **`com.evidwell.llama-server.watchdog`** runs `watchdog.sh` every 2 minutes. It asks
  for a real four-token completion and restarts the server after **two failed checks in
  a row** (`launchctl kickstart -k`, which runs `run.sh` again).

## Why a watchdog, when the worker has a heartbeat

They watch different things. The heartbeat (`pipeline_runs.heartbeat_at`) catches a
worker that died mid-run and requeues the run. A broken llama-server leaves the worker
alive and beating; each run fails retryably at 60 s and 5 min, then fails for good, and
the worker cannot restart a host process from inside Docker.

The failure this exists for: with an Ollama language model resident beside Bonsai, the
GPU runs out of memory and llama-server answers every request with `Compute error`
until it is restarted, **while `/health` still says ok**. So the probe is a completion,
never `/health`. Two checks two minutes apart recover it within ~4 minutes, inside the
worker's retry schedule, so the run that hit the failure usually succeeds on its third
attempt.

What the watchdog does not count as a failure: `503 Loading model` (a start in
progress), and a probe that times out while a slot is processing (busy, not broken —
the server has four slots, so this is rare). The first completion after a long idle took
23 s while macOS paged the weights back in, so the probe waits up to 120 s.

A server that restarts do not fix (model file moved, GPU held by something else) is
restarted at most three times an hour; the watchdog then sends one notification and
tries again as those restarts age out.

## Running the eval judge

The judge is an Ollama language model, which cannot share the GPU with Bonsai. Stop the
server first, or the watchdog's restart (via `run.sh`) will unload the judge:

```bash
./manage.sh stop
python -m evaluation.run --rescore …
./manage.sh start
```
