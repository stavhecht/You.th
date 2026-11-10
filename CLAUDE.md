# CLAUDE.md

Guidance for Claude Code in this repository. **You.th** (slug `youth`) publishes short,
evidence-checked wellness articles: a pipeline retrieves real papers, a local model writes an
article where every finding cites one, and a human approves it before the public feed shows it.

This file holds what applies everywhere: commands, gates, the invariants and a map. Each area
keeps its own rules in a nested `CLAUDE.md`, loaded when you work in that directory. The
reasoning behind decisions is in [DESIGN.md](DESIGN.md) (an index into [docs/design/](docs/design/)).

## Commands

All backend commands run from `backend/` with the venv active (`source .venv/bin/activate`).

```bash
# setup
docker compose up -d db                  # pgvector/pgvector:pg16 on :5433
uv sync                                  # installs from uv.lock, dev group included; add
                                         # deps with `uv add` / `uv add --dev`, never pip
python -m scripts.migrate                # applies migrations via asyncpg; no psql needed
                                         # history and squash rule: backend/migrations/CLAUDE.md
python -m scripts.migrate --status
python -m scripts.seed_admin --email you@example.com --name "Your Name"

# maintenance (all dry by default; --apply to write)
python -m scripts.reclassify_sources          # after any classifier change
python -m scripts.check_retractions           # re-check cited sources
python -m scripts.check_retractions --scope all
python -m scripts.reembed_sources             # after any EMBEDDING_PROVIDER / model /
                                              # chunk-size change
python -m scripts.prune_media                 # stored images no article references
                                              # (7-day grace); Regenerate orphans them
python -m scripts.privacy_request --email a@b.c   # access/erasure request for someone
                                              # without a usable account; counts only by
                                              # default, --export f.json, --erase --apply.
                                              # Verify they own the address first.
                                              # It cannot reach the copy each contact
                                              # message emails to the editorial Gmail
                                              # inbox: delete that by hand.
python -m scripts.scan_trends                 # trend discovery; dry run makes the API calls
                                              # (same code as the console's Run scan button)
python -m scripts.scan_trends --bootstrap --apply   # one-time, ~3 min: build the baseline
                                                    # (4 months; retention keeps 120 days)
python -m scripts.trend_search                # Trend Search Agent, live and dry: what the
                                              # sources say today; --apply queues a real run

# run
uvicorn app.main:app --reload            # API :8000, OpenAPI at /docs
python -m app.pipeline.runner            # generation worker, separate terminal; also runs
                                         # research runs (the desk's Find trending topics)

# n8n (weekly research schedule + manual webhook), from the repo root — see n8n/README.md
docker compose --profile automation up -d n8n
docker compose exec n8n n8n import:workflow --separate --input=/workflows

# check
pytest tests/ -q
pytest tests/unit/test_invariants.py::test_name -q     # single test
ruff check app tests scripts evaluation  # clean; treat as a gate
mypy app                                 # clean (strict); treat as a gate

# evaluate — quality metrics over 143 cases; see evaluation/README.md
python -m evaluation.run                 # exits 1 when a critical metric misses its threshold
python -m evaluation.run --suite retrieval --limit 20   # stops after RANK, no synthesis
python -m evaluation.run --test basic_002 --save-baseline
                                         # (replaces the suite's whole baseline; a run over
                                         # other cases than the baseline's is not compared:
                                         # baseline.py::other_cases)
python -m evaluation.run --suite wellness_pilot   # 19 of the 107 wellness-trend cases;
                                         # then wellness_core (50), wellness (107);
                                         # side_effects (6 broad safety questions)
python -m evaluation.trend_check [run_id]   # a research run's links, freshness, wellness;
                                         # writes datasets/trends.jsonl for --suite trends
python -m evaluation.image_check evaluation/reports/<run>   # one FLUX render per article
                                         # (spends, even with IMAGE_PROVIDER=none)
python -m evaluation.reader_check        # the social/product reader model on 14 labelled
                                         # texts (evaluation/reader_cases.jsonl, not datasets/:
                                         # every .jsonl there must be an article case); one
                                         # batch, ~2 min on llama-server, real model calls

# frontend, from frontend/
npm run dev            # :5173, public site at /, review desk at /review
npm run typecheck
npm test               # vitest over src/ only: pure logic (console/checks.test.ts); e2e/ is Playwright's
npm run build          # tsc -b && vite build
npm run test:e2e       # Playwright, Chromium: the built app against its own API on
                       # :8001 + youth_test (reseeded by backend/tests/e2e/
                       # browser_seed.py each run), served on :4174. Never touches the
                       # dev stack. Dev-only: nothing in src/ imports it, so dist/
                       # never contains it. First time: npx playwright install chromium
```

There is also a whole-stack container path — `docker compose up` runs db → migrate (one-shot)
→ api + worker + web on :8000 and :5173, and needs no `backend/.env`, because every setting in
`config.py` has a working local default. Four things about it:

- **`migrate` and `seed` get their entry point from a bind mount, not the image.** `scripts/`
  stays in [.dockerignore](backend/.dockerignore) — it is maintenance tooling, not part of what
  deploys — and compose mounts `./backend/scripts` read-only instead. So the image did not
  widen. Everything else `depends_on` migrate completing, which is what stops the API booting
  against an unmigrated database and failing at the first query.
  **`./backend/migrations` is mounted too**, over the copy the Dockerfile bakes in, because
  otherwise a newly written `.sql` file needs a rebuild before `docker compose run --rm
  migrate` can see it — and the failure mode is the bad kind: migrate prints "Database is up
  to date" and exits 0, having silently not applied the file you just wrote.
- **`seed` is behind a profile** because `seed_admin` prompts for a password:
  `docker compose run --rm seed --email you@example.com --name "Your Name"`.
- **Ollama stays on the host.** A container on macOS has no Metal access, so a containerised
  `llama3.1:8b` would be CPU-only. `OLLAMA_BASE_URL` is overridden to
  `http://host.docker.internal:11434` — verified reachable from the backend container against a
  host `ollama serve` bound to its default loopback.
- **Source is mounted read-only over the image copy**, so an edit needs a `restart`, not a
  rebuild — only a `pyproject.toml`/`uv.lock` change needs `--build`. Both watchers are forced to polling
  (`WATCHFILES_FORCE_POLLING`, `VITE_DEV_POLL`): Docker Desktop's bind mounts do not deliver
  inotify events on macOS, so an event-based watcher looks healthy and silently never reloads.

`ruff`, `mypy` and `pytest` are the backend's gates (`strict = true` in
[pyproject.toml](backend/pyproject.toml); clean since 2026-11-07). The frontend has no linter;
`npm run typecheck` is its check.

## Where the details live

Read the nested file before changing code in its area. They hold the load-bearing rules, the
measured reasons behind them, and the traps.

| Area | File | What is in it |
|---|---|---|
| Backend-wide | [backend/CLAUDE.md](backend/CLAUDE.md) | config and `.env` traps, keys, pruning, what has met a real response |
| Pipeline | [backend/app/pipeline/CLAUDE.md](backend/app/pipeline/CLAUDE.md) | nine stages, LangGraph ordering, transactions, worker/heartbeat, APPRAISE, synthesis coverage, reader edit, repair |
| Retrieval | [backend/app/retrieval/CLAUDE.md](backend/app/retrieval/CLAUDE.md) | query building, providers, dedup, cache upsert, re-rank, full text, throttling, retractions |
| Evidence | [backend/app/evidence/CLAUDE.md](backend/app/evidence/CLAUDE.md) | verdict cap, validation checks, study-type classification |
| Domain | [backend/app/domain/CLAUDE.md](backend/app/domain/CLAUDE.md) | `contracts.py` as spec, docstrings as prompt text, `NullableJSONB`, TipTap body and citation markers |
| LLM | [backend/app/llm/CLAUDE.md](backend/app/llm/CLAUDE.md) | providers (Ollama, llama-server/Bonsai, Anthropic), model sizing, token/cost ledger |
| Services | [backend/app/services/CLAUDE.md](backend/app/services/CLAUDE.md) | accounts, feed ordering, subjects, card derivation, generated cover, media store |
| Imagery | [backend/app/imagery/CLAUDE.md](backend/app/imagery/CLAUDE.md) | the claim-free image prompt builder |
| MeSH scan | [backend/app/discovery/CLAUDE.md](backend/app/discovery/CLAUDE.md) | trend discovery from PubMed MeSH tags |
| Research agent | [backend/app/research/CLAUDE.md](backend/app/research/CLAUDE.md) | Trend Search Agent + evidence check |
| Social, products | [backend/app/social/CLAUDE.md](backend/app/social/CLAUDE.md) (imported by `products/` and `intel/`) | social listening, product discovery, `app/intel` |
| Tests | [backend/tests/CLAUDE.md](backend/tests/CLAUDE.md) | suites that skip without a DB, `youth_test`, e2e |
| Evaluation | [backend/evaluation/CLAUDE.md](backend/evaluation/CLAUDE.md) | running the eval, recordings, measurement log |
| Migrations | [backend/migrations/CLAUDE.md](backend/migrations/CLAUDE.md) | file list, squash rule, next number |
| Frontend | [frontend/CLAUDE.md](frontend/CLAUDE.md) | theme, route groups, desk preview and checks, editor block types |

## Rules that cross areas

- **Fail loudly on the degradations nobody can see; never fail a run on the ones a reviewer
  will see.** A throttled search or an unanchored query must never look like "found nothing"; a missing picture, missing full text or missing
  appraisal never fails a run.
- **The orchestrator owns every transaction boundary.** Never commit in a stage or the worker.
- **Proposers propose; only a reviewer enqueues.** The MeSH scan, research agent, social and
  product discovery write candidates and never start an article run.
- **A docstring in `domain/contracts.py` (and any enum in a model's output schema) is prompt
  text.** It reaches the model as the generation grammar.
- **Unknown is never zero.** A missing measurement is `None`/NULL, an unpriced model costs
  `None`, an unchecked retraction is `retraction_checked_at IS NULL`.
- **A green `pytest` with Postgres down proves less than it looks**: the thirteen suites in
  `backend/tests/db/` skip silently (what each one covers is in
  [backend/tests/CLAUDE.md](backend/tests/CLAUDE.md)). Check the skip count.
- **Stop llama-server before `npm run test:e2e`** (`llama-server/manage.sh stop`): with it
  resident the browser tests took 31-38 min with random timeouts; stopped, 21 s.
- **Do not compress the synthesis prompt**; it was measured and reverted (see the LLM notes).

## Invariants — do not route around these

The four are stated with their enforcement mechanisms in [DESIGN.md](./DESIGN.md) §1. What
matters when editing:

1. `ReviewService.approve()` is the **only** assignment of `published` in the codebase.
   `tests/unit/test_publish_path.py` greps the AST and fails when a second one appears. If a change
   seems to need another publish path, that is a design conversation, not a fix.
2. Citation validation runs after synthesis, before persistence. A failing draft is written as
   `validation_failed` and never enters the review queue — do not soften it to a warning.
   Its cited-prose rule covers beat 2 **and every section**; beats 1 and 3 are the only
   exemptions, and they are exempt because they carry no findings. When the body grows a new
   place prose can live, that place belongs in `check_beats_are_cited` — otherwise lengthening
   the article silently widens a three-sentence allowance into most of the page.
   A draft failing only fixable checks is sent back to SYNTHESIZE **once**
   (`graph.py::MAX_REVISE_ROUNDS`, `REVISABLE_FAILURES`) with the failures fed back, and the
   rewrite is validated like any draft. That is a second chance, not a softening: a second
   failure is stored `validation_failed` as before. Stage-row ordinals are offset by
   `refine_round + revise_round`, for the same `IntegrityError` reason as refinement.
   Two checks were added 2026-10-02, both revisable, both after the evaluation measured the
   failure: **`unsourced_number`** (`evidence/numbers.py`) — every number in a cited sentence
   must appear in the title, year, abstract or excerpts of the sources it cites, since 14% of
   articles quoted a real statistic cited to the wrong paper — and
   **`no_evidence_with_citations`**, since `no_evidence` is exempt from the cited-beat rule and
   drafts were citing studies under it. That second one blocks **only the first draft**: a
   refuted claim honestly reads `no_evidence` while citing the trials that found no effect, so a
   rewrite that keeps the pairing passes with a `ValidationReport.warnings` entry the desk shows
   the reviewer (`validate_draft(..., final_round=True)`). A revision is now shown the draft it
   is correcting (`revision_feedback(report, draft)`): told to "keep everything else" without
   it, 10 of 12 rewrites came back with a new misattribution elsewhere. `count_sentences` now
   strips citation markers first: ". [S5]" was counted as a sentence and failed correct
   five-sentence beats.
3. Evidence grade **and quantity** cap verdict confidence (`evidence/grading.py`). Exceeding
   the cap is a validation failure, not a style note. Two rules, both **per claim**, with the
   article inheriting its weakest claim's ceiling: the study-type ceiling, and a quorum of
   `QUORUM_FOR_SUPPORTED` sources at a supported-tier grade before `supported` is reachable —
   one study is a finding, not a conclusion. Use `max_verdict_for_claims`, not
   `max_verdict_for_grade`, which answers only the first question. The quorum lowers the
   *ceiling*; `best_evidence_grade` still records the best grade cited, because the console
   needs to say what the article actually rests on.
4. `articles.original_content` is written once and trigger-protected. Human edits go to
   `edited_content`; the feed renders `COALESCE(edited_content, original_content)`.

Plus one structural: the feed card is **derived** by `services/card.py::derive_card()`, never
generated by a separate model call, so card and article cannot contradict each other.

## Keeping these files current

Load-bearing changes to the pipeline, retrieval or the eval go into the nested `CLAUDE.md` for
that area in the same change, with the date and the measurement behind them. Keep this root
file to what applies everywhere; a new area rule goes in its area's file, and a new area gets a
row in the map above.
