# 11. Phased roadmap

The organising principle: **one topic end-to-end before any breadth.** Each phase ends with
something demonstrable.

## Phase 0 — foundations (½ day)
Compose file, Postgres + pgvector up, migration applied, FastAPI boots, `/healthz` green,
Vite dev server renders an empty feed. No AI.

## Phase 1 — the vertical slice, one topic, hand-driven
Target: `"ashwagandha for stress"` produces one validated draft.
1. `sources` upsert + cache, embedding provider wired.
2. PubMed provider only — one API, learn its shape properly.
3. Extraction call (LLM 1).
4. Semantic re-rank against the claim.
5. Synthesis call (LLM 2) with the real prompt.
6. Citation validation. **Deliberately try to make it fail** — hand-edit a source id in a
   fixture and confirm the draft is rejected. Invariant #2 is untested until you have watched
   it reject something.
7. Persist as `pending_review`.

Exit criterion: one row in `articles` with `status = pending_review` and `4/4` citations
resolving, produced end to end.

## Phase 2 — the console
Auth, review queue, article detail with sources panel, TipTap editor with autosave, approve
and reject. Exit criterion: that draft is human-approved and reaches `published`.

## Phase 3 — the public feed
Masonry feed of published articles, card → article route, citation rendering, disclaimer.
Exit criterion: the approved article is visible and readable by someone who isn't you.

**At this point the whole loop works for one topic. Only now scale breadth.**

## Phase 4 — retrieval breadth
Europe PMC, Semantic Scholar, OpenAlex. Cross-provider dedup. Study-type classification
tuning. Review-bias tuning against real output. Exit criterion: five topics of varying
evidence strength produce appropriately different verdicts — and at least one correctly says
"not enough evidence."

## Phase 5 — pipeline hardening
Retries, rate-limit handling, per-stage transaction boundaries and heartbeat-based crash
recovery are done (§3.3, §4) — they were pulled forward because each of them fails *silently*
rather than loudly, and a silent failure in this system reads as a confident answer. What
remains: `pipeline_runs` UI (including `attempts`, `nextAttemptAt` and `heartbeatAt`, so a run
in backoff does not read as a stalled worker), batch topic submission, and putting
`scripts/check_retractions.py` on a schedule — it exists and is dry-by-default, but a sweep
nobody runs is the same as no sweep, and that is the whole feature (§4). Cost tracking is
done (§3.7) — tokens are recorded per stage per attempt with the model that spent them, and
the API returns an estimate. It was pulled forward ahead of the hosted-provider switch on the
same reasoning as the rest of this phase: instrumentation added *after* the switch has no
before-picture to compare against, and the saving from prompt caching is exactly the kind of
quantity that is assumed rather than measured.

## Phase 6 — production
AWS migration per §10, S3 assets, managed Postgres, EventBridge schedule. The API image exists
(§10) and is the one piece pulled forward, because it is also what pins down the runtime
contract — non-root, no migration on boot, one worker per container. Still outstanding before
anything is deployable: a migration step that runs as its own task rather than at container
start, `--proxy-headers --forwarded-allow-ips` once a balancer is in front (§3.2 — until then
the login throttle must keep reading the socket peer), and secrets from somewhere other than an
`.env` file mounted into compose.

## Later, explicitly deferred
Full text for paywalled papers, and caching full-text passages (§4, Pass 3); *model-generated* query refinement (the deterministic
broadening loop shipped 2026-09-07, see §3.2); multi-reviewer roles and invites; SerpApi
fallback; Next.js port if SEO becomes a priority.
