# n8n: when research runs happen

n8n decides **when** a research run starts and what to do with its result.
What a run does lives in the backend (`backend/app/research/`): its first half,
the Trend Search Agent, finds and scores what people are getting interested in;
its second half checks the best of those in PubMed and Europe PMC. There is no
research logic in n8n.

| Workflow | Trigger | Calls |
|---|---|---|
| `workflows/weekly_research.json` | Schedule, cron from `RESEARCH_WEEKLY_CRON` (default `0 8 * * 0`, Sunday 08:00) | `POST /api/automation/research/runs` with `RESEARCH_WEEKLY_PARAMS` and `"mode": "weekly"`, then `GET …/runs/{id}` every minute until the run finishes (at most 45 checks), then `GET …/runs/{id}/result`, then (optionally) a summary to `RESEARCH_NOTIFY_URL` |
| `workflows/manual_research.json` | `POST http://localhost:5678/webhook/evidwell-research` | `POST /api/automation/research/runs` with your body and `"mode": "manual"`; answers with the backend's status code and body |

The desk's **Find trending topics** button does not go through n8n. It calls
`POST /api/console/research/runs`, which calls the same
`research/service.py::start_research_run`. So the button still works when n8n
is not running.

## What the weekly workflow does with each outcome

- **202** — the run started. Poll, fetch the result, notify.
- **409** — a run is already queued or running (runs are single-flight). Ends
  quietly at "Skipped: a run is already going".
- **Anything else** (401 wrong token, 404 token unset on the backend, 422 bad
  `RESEARCH_WEEKLY_PARAMS`, 5xx) — "Could not start" stops the execution
  **with an error**, so it shows as failed in n8n's execution list. It used to
  end at the same no-op as a success.
- **The run failed before finding any trends** (`/result` status `failed`) —
  "Research run failed" stops with the run's errors. A run whose *evidence
  check* failed after the trends were found reports `partial`, and is still
  notified: the trends are in the result.

## Setup

1. Put a shared secret in the repo-root `.env`. The backend and n8n both read it from there:

   ```bash
   RESEARCH_TRIGGER_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
   ```

   While it is unset, the backend's `/api/automation/*` routes answer 404 and
   the workflows cannot start anything. After setting it, restart the API: a
   host venv picks the value up on restart, and a container needs
   `docker compose up -d --force-recreate backend worker`.

2. Optionally, in the same `.env`:

   ```bash
   # Parameters for the weekly run: the same fields as the API (camelCase).
   RESEARCH_WEEKLY_PARAMS={"categories": ["fitness", "nutrition", "sleep_recovery"], "targetArticleCount": 5}
   # Where to POST a summary when a weekly run finishes (a Slack incoming webhook works).
   RESEARCH_NOTIFY_URL=https://hooks.slack.com/services/…
   ```

3. Start n8n and import the workflows:

   ```bash
   docker compose --profile automation up -d n8n
   docker compose exec n8n n8n import:workflow --separate --input=/workflows
   ```

   If the API runs in a host venv rather than in compose, point n8n at the
   host: `EVIDWELL_API_URL=http://host.docker.internal:8000 docker compose --profile automation up -d n8n`.
   Re-importing replaces a workflow with the same id, so this is also how an
   updated workflow file is loaded.

4. Open http://localhost:5678, create the owner account, then open each
   workflow and **publish/activate** it. The schedule and the production
   webhook only run while their workflow is active.

## Parameters

Every field is optional; anything left out uses the backend's settings. A
trigger can narrow and tighten what settings allow, never widen or loosen it,
and **no parameter can switch the evidence check off**.

| Field | Meaning |
|---|---|
| `targetArticleCount` | Topics to propose (clamped to 4-6 by default) |
| `categories` | Editorial categories: `fitness`, `nutrition`, `supplements`, `sleep_recovery`, `lifestyle`, `preventive_health`, `general_health`, `wellness`, `other` |
| `trendWindowDays` | The trend window, 1-30 days (default 7) |
| `geo`, `language` | Two-letter region and language (default `US`, `en`) |
| `enabledSources` | A subset of `RESEARCH_ENABLED_SOURCES`: `google_trends`, `rss`, `google_news`, `gdelt`, `news`, `web`, `youtube` |
| `maxRequestsPerSource` | Lower the per-source request budget (1-200) |
| `minTrendScore` | Raise the minimum trend score (0-100) |
| `minDataCoverage` | Raise the minimum share of the score that must be measured (0-1) |
| `queries` | Up to 10 topics to look into on top of what the sweep finds |

## Try it

```bash
curl -i -X POST http://localhost:5678/webhook/evidwell-research \
  -H 'Content-Type: application/json' \
  -d '{"targetArticleCount": 5, "categories": ["sleep_recovery"], "enabledSources": ["rss", "gdelt", "news"], "queries": ["mouth taping"]}'
```

A second call while the first run is going answers **409**. Once it finishes,
the trend output is at:

```bash
curl -H "X-Trigger-Token: $RESEARCH_TRIGGER_TOKEN" \
  http://localhost:8000/api/automation/research/runs/<id>/result
```

That is the Trend Search Agent's result, in snake_case: the collection window,
which sources succeeded, failed or were skipped, run counts, and each topic
with its trend score, score breakdown, data coverage, confidence, trend status,
key questions, supporting source links, selection reason and limitations. Each
topic says `"scientific_assessment": "not_assessed"` and carries the evidence
check's outcome as a **status** (`downstream.evidence_gate`: `passed`,
`failed` or `not_checked`) — never an evidence score.

## Security

- n8n is published on `127.0.0.1:5678` only. The manual webhook starts a
  research run for anyone who can reach it. If you expose n8n beyond this
  machine, add Header Auth to the Webhook node.
- The token only lets a caller start and read research runs and their trend
  results. It gives no access to articles, reviewers or readers.
- The n8n container gets `RESEARCH_TRIGGER_TOKEN`, `RESEARCH_WEEKLY_PARAMS`
  and `RESEARCH_NOTIFY_URL`, not the whole `.env`. It has no reason to hold
  model, image or source API keys, and none appear in a payload or result.
- `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` lets the workflows read `$env`. They
  read only `EVIDWELL_API_URL`, `RESEARCH_TRIGGER_TOKEN`, `RESEARCH_WEEKLY_CRON`,
  `RESEARCH_WEEKLY_PARAMS` and `RESEARCH_NOTIFY_URL`.
