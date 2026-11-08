# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All backend commands run from `backend/` with the venv active (`source .venv/bin/activate`).

```bash
# setup
docker compose up -d db                  # pgvector/pgvector:pg16 on :5433
uv sync                                  # installs from uv.lock, dev group included; add
                                         # deps with `uv add` / `uv add --dev`, never pip
python -m scripts.migrate                # applies migrations via asyncpg; no psql needed
                                         # 0001_initial.sql, 0002_social_products.sql,
                                         # 0003_social_topic_question.sql,
                                         # 0004_drop_social_post_write_only_columns.sql,
                                         # 0005_product_confirmation_checked_at.sql,
                                         # 0006_drop_collection_write_only_columns.sql.
                                         # 0001 was squashed a sixth
                                         # time on 2026-11-03, folding in 0002-0008: editorial
                                         # subject categories, research categories,
                                         # article_sources.stance, the trend search's tables,
                                         # public search (tsvector + pg_trgm), and 7 more
                                         # write-only columns dropped. The dev and test
                                         # databases kept their data: schema_migrations was
                                         # collapsed to the new checksum after a schema-only
                                         # diff against a database built from the file showed
                                         # no change. (The fifth squash, 2026-09-30, COPIED the
                                         # data into a fresh database; its backup,
                                         # evidwell_backup_20260930, was dropped on 2026-10-14.)
                                         # Squashing is only free while every database holding
                                         # the schema can be rebuilt or have its history
                                         # rewritten; the file's header states that condition.
                                         # Next is 0007.
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
pytest tests/unitTest/test_invariants.py::test_name -q     # single test
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

## Testing gotchas

**Eleven suites skip silently when no database is reachable**, and they are the ones covering
guarantees a fake session cannot express — SQL semantics, transaction boundaries, and the
query planner. A green `pytest` with the DB down is a much weaker signal than it looks:

| Suite | What goes unverified when skipped |
|---|---|
| `test_db_invariants.py` | the CHECK constraint and the immutability trigger — invariants #1 and #4 — and SQL `NULL`, not JSON `null`, in the nullable JSON columns |
| `test_source_cache.py` | resolve-then-update, the `SAVEPOINT` retry, split-row reporting, re-embedding replacing stale chunks, a new chunk size counting as stale |
| `test_orchestrator_transactions.py` | per-stage commit, and persist+completion being atomic |
| `test_worker_claim.py` | the claim `UPDATE`, and `attempts` counted at claim time |
| `test_stale_recovery.py` | heartbeat sweep, requeue-vs-fail on a spent budget |
| `test_rerank_plan.py` | the `EXPLAIN` assertion that no approximate scan is chosen, and a paper scoring as its best chunk |
| `test_reader_accounts.py` | the composite FK on `reader_saves`, the save-is-a-move primary key, and folder-name uniqueness |
| `test_discovery_db.py` | the one-live-proposal-per-substance index, the observation ledger's idempotence, the three decision CHECKs, the descriptor FK that forces the scan's write order, and `pipeline_runs.origin` defaulting |
| `test_reader_privacy.py` | account export and erasure reaching exactly the caller's rows (the folder/save cascade), the password re-check and its throttle on `DELETE /readers/me`, and `privacy_request` erasing by address |
| `test_feed_search.py` | `approve()` indexing the *edited* body through `article_search_document`, drafts never answering a search, citation markers and `topic` staying out of the index, and the typo fallback answering only when full text found nothing and the query used no `-word` |
| `test_research_db.py` | the research candidates' three decision CHECKs, single-flight (the check *and* the `research_runs_one_active` index), the worker's claim, checkpoint upserts by primary key, topic resolution and observation upserts being idempotent, history pruning keeping referenced topics, and feed state storing SQL `NULL` (it deletes the rows it commits) |

Point them elsewhere with `TEST_DATABASE_URL`; they otherwise use `database_url` from
settings. `test_discovery_db.py` runs against a *populated* database on purpose — after a
`scan_trends --bootstrap` the observations table holds tens of thousands of rows — so its fixtures
use synthetic descriptor UIs (`D9999xx`) and generated PMIDs rather than real ones. Assertions
scoped to real MeSH terms fail for reasons that have nothing to do with the guarantee being
checked. `test_worker_claim.py` and `test_stale_recovery.py` also skip when the queue is
already non-empty — they need to see the whole table, so a leftover `queued` or `running` row
from a manual run turns them into passes. Clear it before trusting them. Worse than skipping:
they also *claim* such a row, so a leftover queued run can flip to `running` mid-suite and make
the failure look intermittent. Promoting a trend candidate by hand leaves exactly that residue.

**Stop llama-server before the browser tests** (`llama-server/manage.sh stop`). With its ~8 GB
wired and swap full, `npm run test:e2e` took 31-38 minutes with 2-3 random timeouts; stopped, all
23 passed in 21 s (2026-11-05). A timeout there is memory until proven otherwise.

**`tests/e2e/` runs the whole product against its own database, `youth_test`**, never
the dev one: a topic queued over HTTP, generated by the real worker and orchestrator (only
the models, paper search and embedder are faked, in `tests/e2e/support.py`), reviewed and
approved over HTTP, then read, searched and saved by a reader. Separate for two reasons:
each test starts from truncated tables, and a run queued in the dev database would be
claimed by the docker worker within five seconds and generated with the real model. The
fixture refuses to truncate any database whose name does not end in `_test`. Create it
once: `CREATE DATABASE youth_test`, then `DATABASE_URL=…/youth_test python -m
scripts.migrate` (and again after every new migration). Skipped when absent; point
elsewhere with `E2E_DATABASE_URL`. The e2e engine uses `NullPool` because `TestClient`
runs the app on its own event loop. Its fake abstracts must stay over RANK's
`MIN_ABSTRACT_CHARS` (200), or every paper is filtered and the run silently becomes a
`no_evidence` draft.

**`tests/conftest.py` sets `DISCOVERY_SCAN_ENABLED=false`**: every `TestClient(create_app())`
runs the lifespan, whose scheduler ticks at once, so with the ledger's last scan over a
day old each API test started a real PubMed scan against the dev database.
It sets **`COLLECTION_SCHEDULE_ENABLED=false`** for the same reason: the collection scheduler
would queue social and product runs in the dev database.

**`test_social_db.py` runs against `youth_test`, not the dev database** (override with
`SOCIAL_TEST_DATABASE_URL`), and truncates only the social/product tables around each test:
the daily and weekly runs read *whole* tables, so dev data would change their answers and
`prune` would delete real posts. It skips when `youth_test` lacks migration 0002 — apply it
there with `DATABASE_URL=…/youth_test python -m scripts.migrate`. A test that inserts a
reviewer must remove `social_candidates` first: deleting a user SET-NULLs `decided_by`, which
the decided-has-reviewer CHECK refuses.

The remaining suites use `FakeSession` from `tests/conftest.py` and need no database.
`asyncio_mode = "auto"`, so async tests need no decorator.

## Architecture

Read [DESIGN.md](./DESIGN.md) before non-trivial work — it carries the reasoning behind each
decision. The short version:

**`domain/contracts.py` is the spec.** Every pipeline stage's input and output is a Pydantic
model there, and `ExtractionOutput`/`SynthesisOutput` double as the structured-output schemas
the LLM generates under. If a stage's shape isn't in that file, it isn't defined. Field bounds
(sentence counts, claim caps) are validators there rather than a global word count, so thin
evidence yields a short article instead of padding.

**The pipeline is nine ordered stages** — extract → retrieve → rank → full_text → appraise →
synthesize → illustrate → validate → persist — in `pipeline/steps/`, driven by `pipeline/orchestrator.py`.
Each stage is `(input, ctx) -> output` and maps 1:1 onto a future Step Functions state, so keep
stages free of transport concerns. `ILLUSTRATE` sits where it does for a reason at both ends:
it needs `ctx.draft`, so it must follow SYNTHESIZE, and **PERSIST must stay last** because its
write commits atomically with the run-completion row (see below). `FULL_TEXT` follows RANK so it
reads only papers that made the cut, after the refinement loop has finished with them, and
`APPRAISE` follows it for the same reason; the revision loop returns to SYNTHESIZE, not to it.
`pipeline_stage_runs.ordinal` comes from list position *offset by the refinement round*, so runs
recorded before illustrate existed carry `validate` at 4 and `persist` at 5 rather than 5 and 6,
and runs before full_text (2026-09-29) carry everything from synthesize on one lower, and runs
before appraise (2026-10-02) one lower again — correct about the pipeline they ran on,
deliberately not backfilled. The orchestrator writes `pipeline_runs` / `pipeline_stage_runs` through
a **separate session factory**, so bookkeeping survives a rolled-back article write.

**Ordering is a LangGraph `StateGraph` (`pipeline/graph.py`); everything else stayed in the
orchestrator.** The graph decides what runs next and nothing more — `_run_stage` still records
the stage row, commits, and handles failure, verbatim from the `for` loop it replaced, because
the commit boundary and `_abandon`'s rollback-before-record order are guarantees with tests
attached and neither is expressible as a LangGraph feature. Three things are load-bearing:

- **`GraphState` has one context key, not a field-per-reducer.** LangGraph replaces `ctx`
  wholesale. `usage_by_stage` and `metrics` are mutated in place on purpose — a raising stage
  returns no context, and its burned tokens would vanish from exactly the runs whose cost is
  least visible — and a per-field reducer would silently drop those writes on the failure path.
- **Failure unwinds through `_RunAborted`, which carries the context out.** A raising node
  produces no state for LangGraph to return, and the object the stage mutated on its way out is
  the only record of what a failed model call cost.
- **Node names are positional (`stage_0`…).** Tests build orchestrators over a single fake
  stage; keying nodes on `StageName` would make the graph undefined for any list that is not the
  full eight. `attempt` rides in the state rather than on the instance, so `run` stays reentrant.

**`pipeline_stage_runs` is UNIQUE on `(run_id, attempt, ordinal)`, so a refinement pass cannot
reuse RETRIEVE's list index.** `_run_stage` writes `ordinal + refine_round * len(stages)`. Get
this wrong and the `IntegrityError` escapes the orchestrator entirely — it is raised by
`_record_stage_start`, *before* the try block that converts stage failures — and crashes the
worker's poll loop, leaving the run neither failed nor completed. The offset keeps ordinals
monotonic in execution order (0..8 for an ordinary run; with one refinement 0..2, then 10, 11,
then 12..17) and is a no-op on round 0, so an ordinary run records exactly what it always did. The **commit** boundary still keys on list
position: "is this the last stage" is a question about the pipeline's shape, not about how many
times it has looped. `tests/unitTest/test_orchestrator_transactions.py::test_a_refinement_pass_does_not_collide_with_its_own_stage_row`
fails with an `IntegrityError` without the offset.

**The one branch is RANK → RETRIEVE, and it is why this is a graph at all.** `rank.py::thin_claims`
flags any claim holding fewer than `QUORUM_FOR_SUPPORTED` supported-tier sources — meaning its
verdict was capped by what retrieval found, before the model wrote a word — and the run goes back
through RETRIEVE with `strategy.broaden()`, which drops the *outcome* clause and keeps the
subject. That direction is not negotiable: relaxing the subject instead is precisely the shape
`UnanchoredQuery` exists to refuse. `MAX_REFINE_ROUNDS` is 1 — each pass spends real budget
against NCBI's per-IP ceiling, which the worker shares — and a refinement pass merges into
`ctx.candidates` rather than replacing it, since returning only the re-searched claims would drop
the rest of the article's evidence and read downstream as a more cautious verdict.

**It is deliberately not "few sources".** The measured failure had *twelve* ranked sources on the
uncited claim. No amount of re-searching fixes a model that ignores what it is handed; that is
what rendering each source's claims into the prompt is for. Counting raw candidates would fire on
claims that are already well served and re-ask an answered question.

**LangGraph's checkpointer is unused, and should stay unused.** Durability is the per-stage commit
plus the heartbeat sweep. A second durability mechanism beside them is how the
no-double-execution guarantee gets re-derived by accident.

**The orchestrator owns every transaction boundary — do not commit in a stage or in the
worker.** It commits after each successful stage and rolls back a failing one, so a synthesis
failure no longer discards the `sources` rows and embeddings retrieval paid for, and no
transaction is held open across a model call. The one exception is the last stage: its write
commits together with the run's completion row, which is what stops a killed worker from
leaving an article whose run still says `running` (it would be requeued as stale and written
twice). A retryable `StageError` requeues the run with a backoff up to
`PIPELINE_MAX_ATTEMPTS`; anything else fails it permanently. **Outages are retryable,
contract failures are not** (since 2026-10-02): `LLMError.retryable` is set by the clients for a
model server that is unreachable, times out, 429s or 5xxs, and EXTRACT/SYNTHESIZE pass it on;
RETRIEVE and RANK turn `EmbeddingError` and SQLAlchemy `OperationalError`/`InterfaceError` into
retryable `StageError`s. A missing model, a refusal, or output that broke the contract twice
still fails for good — the retry would send the identical request. One narrow exception, inside
SYNTHESIZE: a *first* draft that broke the contract (a nine-word section heading) gets **one**
fresh draft before the run fails (`fresh_draft_after_contract_failure`), because synthesis
samples at 0.4 and one sample's overrun is not a property of the request.
**Before that fresh draft, a draft that only overflowed a length limit is fitted**
(`synthesize.py::fit_to_contract`, `metrics.synthesize.contract_fit`, 2026-10-28): an
over-long section becomes two ("<heading>, continued"), beat 2's overflow opens a section of its
own, a headline over 12 words keeps the part before its colon or dash. Every sentence keeps its
citation and VALIDATE checks the result like any draft. The client hands over its last answer on
`LLMError.content`. Measured over a week of eval runs: 41 synthesis calls still broke the
contract after the client's repair turn, nearly all sentence caps and headlines, each costing a
fresh draft (~10 min) or the run (wt_019). Anything else, or more than five sections, still
gets the fresh draft. Every synthesis call goes through `SynthesizeStage._draft`, so revisions
and the coverage re-prompt are fitted too.

**A `running` run proves nothing; its heartbeat does.** The worker pings
`pipeline_runs.heartbeat_at` on a timer while a run is in flight, and a periodic sweep in the
poll loop recovers rows that have gone quiet for `WORKER_STALE_AFTER_SECONDS`. Staleness is
deliberately *not* measured from `started_at`: a duration cutoff cannot tell a slow run from a
dead one, and requeueing a live run starts a second concurrent execution of it — the same
double-write the persist/completion atomicity above prevents. Two rules follow. `_beat` must
never raise (a dead heartbeat under a live run *is* the double-execution bug, so a failed ping
is logged and retried), and the sweep must stay in the poll loop rather than becoming a
background task, so a worker can never sweep its own run. Recovery consumes the run's attempt
budget — which is why `attempts` is incremented at claim time — and fails the run rather than
requeueing it once the budget is gone.

**The worker does not claim work while the model server is down** (`llm/readiness.py::model_down`,
`runner.py::_model_available`, 2026-11-05). Attempts are counted at claim, so a stopped llama-server
failed five queued runs in six minutes: three attempts each, two of them dead for good. Before a claim,
with work waiting, it asks for a real four-token completion (Ollama: `/api/version`, when it generates
or embeds), and on no answer holds and asks again every 30 s. Four things: **an idle worker never
probes**, since that would keep the weights paged in; a run in backoff is not work yet; **`social_daily`
still runs** (`MODEL_FREE_KINDS`: no model calls, and a missed day is lost coverage), while research,
weekly social and product runs wait too, because they degrade silently rather than fail; and **a server
dying mid-run still costs that run an attempt**, on purpose, or a request that crashes the server would
retry forever. A hold is a worker log line only: the desk shows such runs as plain `queued`. **The probe
unloads Ollama's language models first, as any llama-server call does** (`factory.py::llamacpp_make_room`),
but only after `/health` answers: a server stopped on purpose so the eval's judge can use Ollama must not
have the judge evicted every 30 s. `/health` proves nothing about completions, so it only gates the unload.

**Retrieval: the scholarly APIs are the index; pgvector is re-rank plus cache.** Pass 1 fans
out per claim across PubMed, Europe PMC, Semantic Scholar and OpenAlex, normalising into
`CandidatePaper`. Pass 2 upserts into `sources`, embeds new abstracts in overlapping chunks, and
ranks by best-chunk cosine + grade bonus + recency bonus. Those bonus constants in
`retrieval/rerank.py` are explicitly tunable starting values, not settled numbers.

**Chunking is one function, `retrieval/chunking.py::chunk_text`: 300-word windows, each
repeating the last 30 words of the one before.** The size is set by the embedding model, not
chosen to force a split: 300 words is ~420 tokens, inside `mxbai-embed-large`'s 512-token
window — and past that window Ollama truncates **silently**, dropping the end of a long
abstract, which is usually its results. It was 120 words for a day (2026-09-29), which split
94% of cached abstracts for no gain: the median is ~250 words and fits whole. At 300, three in
four are one chunk and rank exactly as a whole-abstract vector did; only the long ones split.
Every chunk gets its own vector in `source_chunks` (no text: a chunk is re-cut from the
abstract when needed), and a paper's similarity
to a claim is its **best** chunk's — not the mean, which lets the rest of a long abstract
dilute the one passage that answers the claim, and not the sum, which rewards length. The
model is still shown the whole abstract; chunks exist only for ranking.

**RANK scores against the subject plus the claim** (`rerank.py::ranking_text`, e.g. "magnesium
improves sleep quality"), with the subject chosen the way the query chose it — ingredients,
else product (`claim_subject`). The bare claim names only the outcome, so every paper about the
outcome scored alike whatever it studied; measured 2026-10-01, 13 known-relevant papers reached
a pool and missed its top 12 that way, beaten by off-subject papers that loose providers
(OpenAlex, Europe PMC's full-text fallback) let in. FULL_TEXT keeps the bare claim for excerpts
on purpose — see below.

`sources.embedding_model` and `sources.chunk_settings` record which model
embedded a paper's chunks and how they were cut, and a paper is current only when **both**
match the live values — so editing `CHUNK_WORDS` re-chunks the cache instead of leaving it cut
the old way forever. `cache.py::write_chunks` is the only writer: the pipeline and
`scripts/reembed_sources.py` both go through it, and it deletes a paper's old chunks before
writing new ones, so a shorter re-chunk cannot leave stale ordinals behind.

**Full text adds detail to ranked papers; it never ranks them.** `FULL_TEXT`
(`pipeline/steps/full_text.py`) asks Europe PMC which ranked papers are open access, picks up to
`full_text_max_papers` (6) with the claims taking turns so every claim gets one before any gets
a second, downloads them (`retrieval/full_text.py`, JATS body only), splits them with the same
`chunk_text`, and hands SYNTHESIZE the `EXCERPTS_PER_PAPER` (2) chunks closest to each paper's
claims. Ranking stays on abstracts on purpose: measured 2026-09-29, only 144 of the 309 cached
papers are open access, and best-chunk scoring over a ~20,000-word paper's sixty chunks
against an abstract's one is a length bias — it would reward being open access, not being good
evidence. Five things about it:

- **It never fails a run**, like ILLUSTRATE and for the same reason: without excerpts the
  article is written from abstracts, exactly as before the stage existed — the status quo, not a
  hidden degradation. `metrics.full_text.cause` says why there were none (`disabled`,
  `lookup_failed`, `no_open_access`, `download_failed`, `unexpected`). Europe PMC's search
  endpoint was measured returning 503 for every query for a stretch of 2026-09-29, so
  `lookup_failed` is a real state, not a theoretical one.
- **Introduction and Background are never excerpted**, and this was measured, not guessed: the
  first live run picked *both* its excerpts from a Background section, because background text
  restates the claim's own wording. It also summarises *other* papers — "[3, 5, 21]" — so an
  excerpt from it invites the model to credit this paper with its reference list's findings.
  With them excluded the same paper yielded Methods (57 recruited, 25 per arm) and Conclusion.
  Discussion is kept, though it too cites others; watch it.
- **The prompt says a source without excerpts is not weaker evidence.** Handing the model more
  detail about open-access papers pulls it toward them; that sentence and the cap of six are the
  counterweight. The verdict cap is untouched — it reads study types and counts, never text — so
  excerpts cannot raise how confident an article may sound.
- **Excerpts are recorded from `synthesis_input`, not from `ctx.excerpts`** (`persist.py::
  excerpts_shown`) — what the model saw, for the reason VALIDATE checks handles against it — onto
  `article_sources.excerpts` (`NullableJSONB`), and shown in the desk's sources
  panel. An article can quote a number that is in the paper but not its abstract, and the desk
  shows no abstracts, so without that list a reviewer had to open the paper to check one.
- **Full text is fetched per run, not cached.** Six downloads is cheap next to a synthesis call,
  and there is nothing to keep fresh. `source_passages` in `0001_initial.sql` is still only a
  sketch; build it if fetching becomes the bottleneck, not before.
- **A paper with no PMID is looked up by its DOI**, in the same Europe PMC request
  (`retrieval/identifiers.py`, `metrics.full_text.looked_up_by_doi`). It used to be skipped
  before Europe PMC was asked. The paper's own reference numbers (`[34,35,36]`) are stripped
  from excerpts in `parse_sections`: they point at a list the model never sees, and a copied
  square bracket fails the draft (see the citation-marker paragraph below).
- **Passages describing a review's literature search are never excerpted**
  (`describes_literature_search`, `metrics.full_text.search_passages_skipped`). "The literature
  search identified 4788 articles…" names the claim's words as search terms, so it scored 0.71
  against a findings passage's 0.712 and was chosen. Ranking against `"<product>: <claim>"`
  instead of the bare claim was measured the same day and made it worse: it chose two such
  passages for the review and swapped an RCT's Discussion for an off-topic one. Keep the bare
  claim. The filter needs two distinct search phrases and deliberately omits "were screened" and
  "eligibility criteria", which also describe a trial's recruitment and its sample size.

**A question is not a product.** Extraction was copying the user's question into `product`
("Is it bad for you?"), which then searched as a quoted phrase, found nothing, and published
`no_evidence` on a well-studied question. The extraction prompt now says to name the substance
or practice a question asks about (or `unspecified`), and `query_builder._is_a_name` refuses a
question, a sentence of more than eight words, or `unspecified` as an anchor — after
`_mesh_within` has had its chance to find a known substance inside it. **An unmapped product is cut at " for "** (`_subject_group`, 2026-11-05): "Smart ring for sleep"
searched as one quoted phrase; measured on 8 real trend and MeSH labels, 2 came back whole from
extraction and were saved only by a known substance inside them. A device has no such rescue.
**An unmapped name is searched singular and plural** (`_word_forms`, 2026-11-05), since a quoted
phrase matches one form: "smart rings" counted 17 papers on Europe PMC and either form 47. Words in
-ing stay single ("foam rolling"), short ones aside ("smart ring"). A lone plural stays as written
(2026-11-06): "dates" searched `"date"`, in every abstract saying "to date", and "greens" `"green"`.
That costs "wearables" and "mushrooms" their singular, and the suite figures below predate it. **Verbs a question words its
claim with are stopwords** ("replaces", "detects", "measures", "tracks", "accurately"). Both change
the terms of every search for an unmapped subject, so recordings keyed by those terms miss. Retrieval
suite re-run on them 2026-11-05 against 2026-10-24: 89 of 114 cases pass both times, nDCG@10 94.0% →
94.9%, precision@5 94.0% → 94.4%, recall@3 35.0% → 33.0%, recall@5 unchanged at 42.9%.
**One field can hold several names, and splitting it must never loosen the anchor**
(`_names_within`, `_split_name`, 2026-11-07). The search is an OR, so one bad part finds papers off the
subject and every later signal reads as success: the first version turned "vitamin d and depression" into
`"Vitamin D" OR "depression"`, "16/8 intermittent fasting" into `"16" OR "8 intermittent fasting"`, and split
43 of 242 names in eval traces, many badly. The rules: **a bracket beside a known substance only names if it is
known too** ("melatonin (low dose)" searches Melatonin only), and any unmapped bracket stating a dose goes:
"intermittent fasting (low dose)" counted 357,271 PubMed papers with it, 3,472 without. `_mesh_within` returns
every known substance in the field, so "creatine and beta-alanine" searches both, but not in the context after
"during" or "with" ("creatine during the menstrual cycle" is creatine). **An "or" alternative stays beside a known
substance** ("fish oil or krill oil" searches both). **A plain "and" never splits**, because it joins a subject to its outcome
("protein timing and muscle hypertrophy") as often as two names, and extraction rule 5 now says to write "or"
between alternatives; `/` splits only between letters; **a lone word beside a longer name takes its last
word** ("hot or cold compresses" searches "hot compresses" and "cold compresses"; whole, it matched no abstract once
names were tagged [tiab]), unless it is a known substance, and except in a comma list ("saunas, steam rooms").
The cost is on purpose: an unmapped "ice packs and cold wraps" stays one name (3 PubMed papers) and may draft
`no_evidence`, which now says it may be our search's words, while a wrong anchor drafts a confident article on the
wrong papers. A field over `_MAX_NAME_WORDS` is still a name when it splits into parts of at most `_MAX_LISTED_NAME_WORDS`
("green juices or celery juice or ginger and turmeric shots", wt_039, searched turmeric alone); a longer part is half
a sentence. One known gap, so add a hint rather than loosen a rule: "coffee or green tea" searches "coffee tea" and
"green tea". Over 200 real extracted names (2026-11-07): 2
unmapped pairs joined by "and" (45 and 933 papers), 3 fields with two known substances, none wrong, and 4 unmapped
brackets, all real synonyms.
**Who a claim is about narrows the search** (`_population_group`, `POPULATION_HINTS`, AND-ed in, kept by
`broaden`): "without diabetes" had no review in PubMed, and 9 with "nondiabetic" beside it. Several groups OR
("men and women"), but "pregnant women" is one group, not any study of women. Neither rule has been through the
retrieval suite yet, and names whose terms changed miss their recordings. **Identical searches
run once per RETRIEVE** (`searches_shared`): two claims reducing to the same terms used to send
every provider the same request twice.

**APPRAISE records which way each source points; it changes no verdict yet.** The verdict scale
measures support, and the cap counts a cited source toward a claim by what it was *retrieved
for*, never by what it *found* — so two RCTs that found nothing licensed `supported` exactly as two
that found an effect did, and on 2026-10-02 eight drafts refuting a claim ("creatine damages the
kidneys") read `no_evidence` while citing the trials that refuted it. Retrieval was never the gap:
queries are `<substance> AND <outcome>` with directional verbs stopworded, so null trials arrive.
`pipeline/steps/appraise.py` makes one call per claim over its ranked sources and labels each
pair `supports | no_effect | contradicts | unclear | off_topic`, relative to the claim *as
written* (no kidney harm found is `no_effect` for the harm claim). This is phase 1 of a two-phase
plan; phase 2 is a `not_supported` verdict with the same quorum as `supported` and a cap that
counts sources by direction, and it waits on the labels' measured accuracy. Six things about it:

- **It never fails a run**, like FULL_TEXT and ILLUSTRATE: `metrics.appraise.cause` is
  `disabled`, `no_sources`, `model_error`, `partial_model_error` or `unexpected`, and the draft
  proceeds exactly as it did before the stage existed. `APPRAISAL_ENABLED=false` switches it off.
- **A missing label means not appraised, never `unclear`.** A skipped handle stays absent, an
  invented one is dropped, and `article_sources.stance` is NULL for it (`0001_initial.sql`) — the
  same rule as `retraction_checked_at IS NULL`.
- **The synthesis model is not shown the labels**, by decision. `PromptSource.stances` carries
  them to VALIDATE and `render_source_block` ignores them; a test pins that the rendered prompt is
  identical with and without them. Two independent readings are what make a disagreement
  visible. If the evaluation shows the *article* model is usually the one misreading, render them.
- **It only warns.** `check_verdict_against_stance` raises `verdict_against_stance` (`supported`
  while trials and reviews that found nothing are at least as many as those that found the effect)
  and `refutation_understated` (`no_evidence`/`weak` while ≥2 of them found nothing and outnumber
  the rest) as *warnings* in every round, because the labels come from a model and are unmeasured.
  A third, `verdict_on_off_topic_sources`, fires on any verdict but `no_evidence` when every
  source cited for a claim was labelled `off_topic`. `off_topic` counts in neither direction, so
  that case left the tally at 0/0 and read as no conflict (2026-10-02: a `supported` article
  citing three cardiovascular meta-analyses for "increases antioxidant intake").
  `ValidationReport.stance_tally` records the per-claim counts on every report (None when nothing
  was appraised), so phase 2's effect can be read off real articles before anyone builds it.
- **`Stance` has no docstring** — it is part of `AppraisalOutput`, the appraisal model's grammar,
  and a docstring would reach the model as the enum's description. Same trap as `Category`.
- **It runs on `llama3.1:8b`, not the synthesis model, on measurement** (`OLLAMA_APPRAISAL_MODEL`
  default). Over 132 blind-labelled (source, claim) pairs on 2026-10-02, qwen2.5:7b *flipped* the
  direction of 15 of 54 studies that test the claim — it reads good news as support, so a coffee
  study finding a heart benefit "supported" the claim "has no effect on heart health" — while
  llama flipped none. A flipped label is the error that would push a verdict the wrong way.
- **Relevance comes before direction, in one call, anchored on a quote.** The commonest error
  on both models was giving a direction to a study that measured something else ("colds were
  shorter" counted for "prevents colds"; melatonin-for-migraine for falling asleep faster). The
  model now writes `claim_outcome` once — the *change* the claim promises, not just its topic —
  then per source a word-for-word `quote` of the abstract's sentence reporting that outcome (or
  "none"), `reports_claim_outcome`, and only then `stance`. `appraise.label` stores `off_topic`
  when there is no quote or the boolean is false (`metrics.appraise.relevance_overrides`).
- **Four designs were measured on 2026-10-02 (llama3.1:8b, 132 blind-labelled pairs, 11
  claims, three held out from the prompt work); this one won.** Right direction overall /
  flipped / off-topic caught: direction alone 61% / 0 / 23%; one call with free-text summaries
  72–75% / 5 / 72%; **one call with quotes (current) 79% / 2 / 75%**; two calls, relevance then
  direction on the relevant sources only, 78% / 2 / 69%. The two-call design looked best when
  *simulated* from earlier runs (80% / 0 / 72%) and lost that edge when built — the "0 flips"
  was partly luck on those items; the two flipping designs flipped *different* studies, so ~3%
  looks like this model's floor. The quote design is also one call (~60 s a claim) against two
  (~78 s).
- **Both leading designs are in the tree: `APPRAISAL_MODE=one_call` (default) | `two_call`.** The
  winner is a fact about the model, not the task — a larger or hosted model may reverse it — so
  the two-call mode (`RelevanceOutput`, then `DirectionOutput` shown only the relevant sources;
  a claim with nothing relevant skips the second call) is kept as a tested mode rather than
  deleted. Re-measure before switching: `metrics.appraise.mode` records which one ran, and the
  one-call prompt's bytes are what the numbers above describe.
- **A refutation must quote the outcome (`appraise.label`, `metrics.appraise.outcome_guard_overrides`).**
  After the designs above, the commonest directional error was a *made-up refutation* — 11 of 131
  labels — where llama quoted a sentence about something else ("increases in strength" for a
  claim about endurance) and called it `no_effect`. A `no_effect` or `contradicts` label whose
  quote shares no word (up to a suffix) with `claim_outcome` is now stored `off_topic`; `supports`
  is exempt, because synonyms ("TG") would turn real support into a false off-topic call.
  Measured: made-up refutations 11 → 6, flips 2 → 0, but real refutations missed 9 → 16 (a quote
  like "the pooled RR was 0.96" names no outcome). It cannot see a shared word answering a
  different question ("muscle strength" against "muscle gain"). **A prompt rule against the same
  error was tried and reverted**: "silence is not a result" left made-up refutations at 12 and
  made every other number slightly worse — llama does not follow it, so the rule is in code.
- **The eval keys appraisal recordings by prompt too** (`harness/llm.py::_APPRAISAL_PROMPTS`), so
  a prompt edit re-runs APPRAISE instead of replaying the old prompt's answers. Synthesis
  recordings are keyed by the system prompt and the output grammar too (since 2026-10-14;
  before that a synthesis prompt edit silently replayed the old prompt's drafts). Extraction
  recordings are keyed by its prompt too since 2026-10-23, for the same reason.
  **A failed synthesis call is recorded too** (2026-10-28): the nth identical request in a case
  has its own slot (`key#n`; the first keeps the plain key, so older recordings replay as
  before), and a permanent failure is stored there with the answer `fit_to_contract` may fit.
  Before, only the success was recorded, so a replay answered the first call with it and took a
  different path from the live run (wt_072, wt_009). Outages are never stored.
- **The eval judge is `qwen2.5:14b-instruct`** (`evaluation/config.yaml`; fallback
  `llama3.1:8b`). Measured 2026-10-26 on the same runs: citation correctness 92% vs 73%
  (regression) and 86% vs 58% (pilot), no false hallucination flag where llama had one, ~2.5×
  slower. Its window grows with the prompt (`llm_judge.py::_context_for`, up to 32K), because
  Ollama silently keeps only ~num_ctx/2 of a longer prompt and the judge answered blind.
  Its certainty label is the weakest part: on the 107-case run about half of its
  "overconfident" calls were a missing topic, not overclaiming.
- **Keep the Mac awake through a long eval** (`caffeinate -i -s`). macOS stops the monotonic
  clock in sleep, so `case_timeout_seconds` cannot fire and durations lie: on 2026-10-27, wt_072
  did 21 minutes of work over 9.3 hours of wall clock, and wt_070's embedding call timed out
  across a sleep. A case far past the timeout is the sign; compare the trace's `duration_ms`
  with `finished_at - started_at`.
- **`items` has `min_length=1`.** llama once wrote `claim_outcome` and stopped with an empty
  list; the bound puts `minItems` in the grammar and sends an empty answer through the repair
  retry rather than leaving every source unappraised.
- **Cost: one call per claim, ~55–110 s each on llama** (~12 abstracts, ~6,600 input tokens) —
  up to six per run. The evaluation reports `source_stance_accuracy`,
  `source_stance_direction_accuracy`, `stance_coverage` and `stance_disagreement_rate` as INFO,
  scored against hand labels in a case's `expected_stances` — labelled from the abstract only,
  because that is all APPRAISE sees, and keyed **per claim** (one paper can point different
  ways for two claims). Eleven cases carry the 132 labels the designs above were measured on. The prompt's worked examples deliberately name no claim in
  the evaluation set; the first version used creatine-and-kidneys, which is case `adv_010`.

**Every query must name a substance, and failing to is a stage failure — not a warning.**
`QueryStrategy.build()` takes `product` and `ingredients` separately: ingredients name the
actives but are optional, `product` is required, so the product is the fallback anchor when a
model returns `ingredients: []` (local models do this constantly). If neither yields a subject
term, `TemplateQueryStrategy` raises `UnanchoredQuery` and `RetrieveStage` converts it to a
**non-retryable** `StageError` before any provider is called — nothing varies between attempts,
so a retry just recomposes the identical unusable query. This replaced a keyword fallback that
logged a warning and continued, and the difference is not academic: a run on creatine with no
ingredients searched `(workout AND performance)`, retrieved 50 real papers about CrossFit and
pre-workout blends, cited two of them, passed citation validation, and produced a `supported`
verdict at `systematic_review` grade on a product none of the sources mention. Every downstream
signal reads as success — this stage is the last place the mistake is visible. Do not soften it
back to a warning, and do not merge `product` into `ingredients` at the call site.

**Identity is a union-find over every identifier a record carries** (`retrieval/dedup.py`), not
one preferred key per record. Providers report different identifier *subsets* for the same
paper, so a DOI-only and a PMID-only record only unify when some third record carrying both
bridges them — a single-key scheme leaves two candidates, two `sources` rows, and two handles
the model cites as independent corroboration. Titles are an identity key only for a record
carrying no identifier at all: errata and conference abstracts repeat their parent's title, and
over-merging silently deletes a study while under-merging only costs a prompt slot. This is
invisible with PubMed alone (its records carry both identifiers) and appears the day the other
providers come online — watch `retrieve.duplicates_merged`.

**A DOI-only paper is given its PMID when Europe PMC knows one**, in `RetrieveStage._fill_pmids`
after the first merge and before the cap, and the merge then runs again, so a filled PMID bridges
a DOI-only and a PMID-only record of one paper. A PMID is taken only from a record whose *own*
DOI matches, and a DOI answering to two PMIDs is left alone: guessing would merge two papers. A
lookup that fails never fails the stage (`retrieve.pmid_lookup_failed`), because the search
itself worked. Expect `pmids_filled` to be low: OpenAlex already reports a PMID when one exists,
and measured 2026-09-29 all nine DOI-only papers in the cache had none in PubMed, Europe PMC or
NCBI's ID converter. They are in journals MEDLINE does not index, so there is nothing to assign.
A cached DOI-only row picks up its PMID the next time a run retrieves it (the cache's
`COALESCE`).

**Queries keep a single-outcome claim's own phrase, and the cap cuts nothing** (2026-10-23).
`_outcome_group` adds `"sleep latency"[tiab]` beside the generic sleep vocabulary, directional
verbs ("lowers", "increases", "causes") are stopwords, the reviews pass asks for 30 (was 15) and
`retrieval_max_candidates_per_claim` is 65, the two passes' sum: at 50 the extra reviews cut 10
gold papers out of the general pass. Measured on the retrieval suite: gold recall@10 48.6% →
54.1%, precision@5 93.2% → 95.3%, nDCG@10 93.7% → 95.8%. Gold papers ranked 6–12 sit 0.002–0.03
below other meta-analyses, so the grade and recency bonuses were left alone: tuning them to a
two-paper gold list would fit the list, not the ranking.
An unmapped product drops usage words before it is quoted (`_USAGE_WORDS`: "daily sunscreen
use" searched as one phrase missed the trial titled "regular sunscreen use", 2026-10-25), and
the longest matching outcome key wins ("skin cancer" no longer brings in "skin"'s aging terms).
Since 2026-10-26: a subject naming examples ("wearables like WHOOP", "such as", "including")
searches each example; outcome keys match at a word start only; and a harm claim ("causes",
"damages", "toxic", "side effects") ORs in safety vocabulary (`"adverse effects"[sh]`,
"adverse events", "side effects"), because harm is reported in safety tables, not under the
outcome's own name.

**A broad question covers its four best-studied claims** (`rank.py::MAX_CLAIMS`,
`best_studied`, `metrics.rank.claims_dropped`, 2026-10-25). "Side effects of GLP-1 drugs"
extracted six; at 12 sources each the prompt overflowed and the thinnest claim capped the verdict
at weak. The cut ranks by supported-tier sources, then sources, whichever way they point, so good
news and bad both survive, and it narrows `ctx.candidates` and `target_claims` too: VALIDATE seeds
a quorum per claim, so a dropped claim left in the list would still cap the verdict.
**A serious harm goes ahead of the evidence count** (`rank.py::_serious`, 2026-10-28): a harm
claim (`query_builder.HARM_CLAIM`) naming the liver, kidneys, heart, blood, bleeding or a medicine
interaction is kept before any milder one. Ranked by evidence alone, se_001 (creatine) dropped
kidney and liver injury and kept stomach upset. The cost: a question with four or more serious
harms covers only those, so creatine's six claims (two of them arrhythmia and "blood pressure
elevation") now drop stomach upset and water retention, its commonest effects.
**Safety questions get one extra extraction instruction** (`extraction.py::safety_note`, only
when the topic asks about side effects, risks or "is X safe", so other questions' recordings still
replay): one claim per *kind* of effect ("digestive upset", not nausea, diarrhea and cramps), the
best-studied first, harm or no harm, and serious harms included even if rare. That is a prompt
rule because nothing in code tells a duplicate from a neighbour: mxbai scores diarrhea/loose
stools 0.79 and kidney/liver injury 0.79 too, and papers reporting adverse events share most of
their sources across symptoms (nausea/dizziness 0.60).

**A DOI-only paper doi.org has never registered is dropped** (`RetrieveStage._screen_unregistered`,
`identifiers.py::DoiRegistry`, after the retraction screen). OpenAlex indexes journals that print
DOIs they never registered: a gut-health article cited a "systematic review" whose DOI doi.org and
Crossref both 404. Only DOI-only papers are asked (0–26 per run measured), a refinement round
skips DOIs that already passed, and an unanswered DOI is kept. The eval's recordings gained
doi.org answers on 2026-10-23; a replay of older recordings would skip cases on the miss.

`RetrieveStage` is the only stage that writes to the cache; it carries each candidate's row id
forward as a `CachedCandidate` on `ctx.candidates`, so **`RankStage` takes no `SourceCache` and
reads only**. Don't reintroduce a lookup there — it was a full re-upsert of the candidate set
to recover ids the previous stage already had.

**The cache upsert is deliberately not a single `ON CONFLICT`.** A paper's identity spans two
partial unique indexes (`doi`, `pmid`) and Postgres takes one inference clause per statement,
so inferring against whichever identifier the incoming record happens to carry breaks as soon
as a record arrives *more complete* than the row it matches — PMID-only row, DOI-bearing
record, infers on the DOI index, finds nothing, inserts, violates `sources_pmid_key`. Union-find
makes that the common path, not the edge case. `SourceCache` therefore resolves first (one read
matching every identifier at once), updates matched rows **by primary key**, and inserts only
new papers; the read-then-write race surfaces as an `IntegrityError` inside a `begin_nested()`
`SAVEPOINT` and is retried once. The savepoint is load-bearing — the pipeline session is
long-lived and a bare rollback would discard the rest of the run. Matched-row refreshes are one
`UPDATE ... FROM (VALUES ...)` per batch, not per row; warm is the normal case, and the per-row
loop made it the most expensive path instead of the cheapest.

**The re-rank is exact and there is no HNSW index.** Top-k
is applied in Python after the grade bonus, so no `LIMIT` reaches SQL and the planner cannot
choose an approximate scan — `tests/unitTest/test_rerank_plan.py` pins this with `EXPLAIN`. That holds
for `source_chunks` exactly as it did for the old `sources.embedding`: creating an index there
means revisiting `rank_for_claim` first. `0001_initial.sql` records the statement and the
condition that would justify one, commented beside the index it declines to create.

**A throttled search must never look like an empty one.** Providers sit behind
`retrieval/throttle.py` (per-provider token bucket + bounded retries honouring `Retry-After`),
and `RetrieveStage` fails the stage when *any single claim* had every provider call fail.
Recall lost to a 429 produces a more cautious verdict, which reads as a correct answer — so the
degradation is invisible unless it is caught here. Rates are per process and the APIs limit per
IP; the constants in `retrieval/factory.py` are deliberately under the published ceilings. A
related consequence: Semantic Scholar without an API key is skipped **at construction** rather
than returning `[]` from `search()`, because a provider that answers without making a request
counts as a successful search and would mask a claim whose only real provider was throttled.

**Zero usable sources means no generative call at all.** `SynthesizeStage` branches to
`services/no_evidence.py`, which assembles the `SynthesisOutput` deterministically. A model
handed a product name and no abstracts is the ungrounded generation the pipeline exists to
prevent, and it would emit no citations — so invariant #2 would pass by vacuum rather than by
verification. The draft still takes the ordinary path and passes validation on its merits (empty
handle set, `no_evidence` exempt from the cited-beat rule, `best_grade([])` is `UNKNOWN`) and a
human still approves it. This is the pair to the throttle rule above: "we searched and found
nothing" is publishable, "we could not search" is not, and they reach synthesis as the same
empty list. `metrics.no_evidence_cause` separates nothing-retrieved from filtered-away.

**Nullable JSONB columns use `NullableJSONB` from `domain/models.py`, never bare `JSONB`.**
SQLAlchemy defaults to `none_as_null=False`, so a Python `None` persists as the JSON literal
`null`, not SQL `NULL`. `COALESCE(edited_content, original_content)` then returns JSON `null`
for an unedited article, and `WHERE error IS NULL` matches no succeeded stage. Python readers
hide it — `json.loads('null')` is `None`, so `edited_content or original_content` is correct —
which means it only breaks in the SQL that DESIGN.md §3.4 and `services/card.py` document as
the enforcement of invariant #4. Both columns were shipping JSON `null` before this was caught.

**The verdict cap is a ceiling, and nothing raises a verdict toward it.** This asymmetry is
correct as a safety property and it hides a whole class of failure: an over-confident draft fails
loudly, an under-confident one is indistinguishable from a correct cautious call. Measured
2026-09-07 — all three articles in the database were `weak`, every one of them citing only
meta-analyses and systematic reviews, every validation report clean. Two of the three had
ceilings permitting `supported`; the model simply declined. `ValidationReport.verdict_ceiling`
is recorded on every draft, pass or fail, and shown beside the verdict in the desk, because
without it "always weak" has no signal anywhere in the system. It is `Verdict | None` — reports
written before it existed must read as "not recorded", never as `no_evidence`, which is a real
verdict and the falsest thing a default could say.

**The other half of that diagnosis was a prompt that asked for reasoning it withheld the data
for.** §3 tells the model that a claim it cites nothing for caps the whole article at `weak` and
that the quorum is checked per claim — both judgements about a claim-to-source mapping that
`render_source_block` did not render. `PromptSource.claims` was populated, carried the whole way
to the prompt, and dropped. One claim drew twelve sources and nought citations, which capped its
article by itself (`validation.py` seeds `types_by_claim` with *every* target claim, so an
uncited one yields `best_grade([]) -> UNKNOWN -> WEAK`, and `max_verdict_for_claims` takes the
weakest). Sources now carry a `Retrieved for:` line. It says *retrieved for*, not *supports*:
which claim sent us looking is a fact about our query, and asserting the paper bears it out would
hand the model its conclusion.

**`SynthesizeStage` re-prompts once when a draft ignores its sources — and that is a coverage
rule, not a length one.** The gate is `MIN_CITED_FRACTION` (0.75) of the offered sources, and only
when `SECTIONS_EXPECTED_FROM` (6) or more were offered. A draft citing one of two sources has
nothing more to say and is never asked; a draft handed 24 systematic reviews that wrote about
three of them is.

**A source counts only when it has a citation of its own**: a chip of at most
`MAX_HANDLES_PER_CITATION` (3) handles, where a chip is a run of adjacent markers split by the same
`CITATION_RUN_RE` the renderer uses, so `[S1][S2][S3][S4]` is one citation of four. Counting every
handle in the body was gameable, and was gamed: a 109-word article counted as citing 22 of its 24
sources because 19 of them sat in one bracket. Of the 57 citations in the database on 2026-09-29,
54 carried one to three handles. Both changes landed together that day (the bar was 0.5 before),
and against the twelve articles then in the database the new rule re-prompts every first draft, so
expect it to fire on most runs; each firing is one more synthesis call. `handles_covered` and
`first_draft_handles_covered` record the count beside `handles_cited`, which, like `was_cited` and
validation, still counts every handle.

**It was briefly a word-count floor aimed at a three-minute read, and that was wrong twice over.**
It fired on essentially every run, because no local model this machine can hold writes 700 words
(see the sizing table below) — and length was never the defect anyway. The measured failure is
*evidence ignored*: 2 of 13, 3 of 24, 1 of 13. The one re-prompt that clearly worked took a draft
from 3 cited sources to 24. Aiming at coverage keeps DESIGN.md §6 intact rather than bending it:
there is still **no floor on article length**, a thoroughly-cited short article is a correct
output, and `test_a_well_covered_draft_is_left_alone_however_short` pins that. It also restates a
rule the prompt already gives ("every source bearing on a claim deserves a sentence") instead of
adding a competing one. `body_words` is still recorded as a metric — it is how the original
problem was found — but nothing branches on it.

Three rules keep the nudge from becoming a floor by another name: a thin source set is never
re-prompted, it asks exactly once, and a re-prompt that *raises* keeps the first draft rather than
failing the run. The feedback names the unused handles, separating never-cited ones from those
cited only inside a long list, states the three-handle limit, and refuses padding explicitly,
because an instruction to write more, handed to a model with nothing left to say, produces exactly
the manufactured nuance §6 refuses.

**The second draft is kept only if it gives more sources a citation of their own, and that rule
was written the hard way.** Keeping it unconditionally was tried. Live, a draft citing 3 of 13 came back citing 1; and
separately a 152-word `mixed` draft came back at `no_evidence` *while citing all thirteen
sources*. Nothing downstream catches the latter — `no_evidence` is exempt from the cited-beat rule
and sits below every ceiling, so it validates clean and publishes. The test is coverage, not
length: a second draft that says more about fewer papers is not what was asked for. And it is the
same count the gate uses, because raw handles would prefer a second draft that lists everything in
one bracket. Nothing guards the `no_evidence` case directly: a second draft that gives more sources
their own citation *and* drops to `no_evidence` is still kept.

**Sources APPRAISE labelled off-topic for every claim do not count toward coverage**
(`synthesize.py::_worth_citing`, `metrics.synthesize.sources_labelled_off_topic`, 2026-11-05). The
gate, the handles the feedback names and the keep-the-second-draft rule all read only the rest, and
the feedback no longer says "if a source does not bear on any claim, say so and cite it there". That
pair wrote a "What the other sources measured" section (VO2max, pregnancy) into a smart-ring article
whose twelve sources were labelled off-topic bar one. Common, not rare: in the 107-case run half the
cases had at least 67% of sources labelled so, and 72 of 102 had at least half. They stay in the
prompt, because the labels are right ~75% of the time ("Smart Ring in Clinical Medicine" was wrong):
the model may still cite one, it is just never pushed to. A missing label still counts as usable.
That is why this is not `SYNTHESIS_RELEVANCE_FILTER`, which removes them. The eval's
`coverage_reprompt_rule` reads the same `_worth_citing`, or it fails every draft the gate now spares.
**Measured on the 19-case pilot, 2026-11-05, against the 2026-10-26 baseline, and not settled.** Better:
citation correctness 85.6% → 89.0%, cases with a hallucination signal 1 → 0, and catch-all sections
mostly gone. Worse: verdict accuracy 15 → 13 of 19, proportionate certainty 12 → 9, citation recall
67.0% → 63.3%, and three articles fell to about 200 words (wt_011, wt_090, wt_098). The search changes above confound
it, except where the first draft replayed identically, and there the mechanism shows: **the re-prompt
was also what corrected some overconfident first drafts.** wt_098 (rosemary and hair) kept a 191-word
`supported` draft because 7 of 12 labels read off-topic, leaving 5 usable, under the gate's 6. The old
re-prompt had turned that draft into `mixed`, 12 sources, and a mention of hair greying, which the new
article lacks; the labelled-off-topic sources included the meta-analysis the draft cited and the greying
study, so wrong labels did this. wt_090's re-prompt returned 3 citations where the old feedback got 12,
one sample each. Off-topic-labelled sources are still 44% of citations (46% before). The 107-case run
waits on this.
**So "enough sources" counts every source offered (2026-11-06); only the 75% is over the usable ones**
(`_is_thin`, and the eval's rule with it). A wrong label can no longer cost a draft its second look. Replayed
over the pilot's first drafts it changes two cases. wt_098 went 225 → 579 words, 5 → 10 cited, hair greying
back and its citation-correctness and groundedness failures gone, but the new re-prompt still said
`supported` where the old one said `mixed`, so its verdict still fails. wt_007's re-prompt was not kept
(identical article). wt_090 and wt_011 already re-prompted, so they are untouched and still ~200 words.
**A cached re-run replays any case whose requests did not change, in seconds** (wt_090 2 s, wt_011 12 s), so
it reports the same article again: a pilot after a small gate change measures only the cases it changes.
A new sample of every case needs `--mode live`, which also overwrites their recordings. The cached 19-case
re-run (2026-11-06, 3.5 min with the judge) changed only wt_098: citation correctness 89.0% → 90.2%,
citation recall 63.3% → 65.6%, groundedness 95.7% → 97.1%, verdicts still 13 of 19, certainty still 9.
**Removing "leave it out" from the coverage feedback was tried and reverted (2026-11-06, 4 cases).** No
verdict improved, and failed checks went 8 → 11. wt_098 fell back to 225 words and three failed checks more
(its re-prompt cited 3, so the first draft stayed), wt_095 gained a groundedness failure and "What other
studies found", wt_090 did not move; only wt_011 gained, 213 → 884 words and 3 → 19 cited. **One sentence of
feedback swung citations from 10 to 3 on one case and 3 to 19 on another, so one re-prompt sample cannot rank
two wordings.** Compare wordings over several samples per case, or not at all.

**A verdict above a conclusion that disowns the claim is lowered to `weak`, in code**
(`validation.py::claim_not_backed`, `ValidateStage`, warning `verdict_lowered_by_text`,
`metrics.validate.verdict_lowered_from`, 2026-11-06). On the pilot, all six wrong verdicts were too high and
four were `mixed` over a bottom line saying the research does not back the claim. It reads the bottom line
and summary only, acts only on a draft that passed (a revision is shown the draft it corrects, and this way
no model request changes, so recordings replay), and only ever lowers. It re-validates so the warnings describe the stored verdict.
It is the weakest-claim rule applied to the article's own words, and it cannot tell the whole claim from part of it ("not supported
across all populations"), so the reviewer is shown the sentence. Two sentences do not count: a split between
studies ("others found no clear effect"), and on a claim saying "safe", "no clear evidence of harm" (`_NO_HARM_FOUND`), which
backs it; "insufficient evidence of harm" says nobody knows, so it still lowers. Since 2026-11-08 "does not support worries
that it harms" and "has not been shown to damage" back it too (both lowered a safety article before), but only with the harm
word before any "safe" or "harmless": "not proven harmless" still lowers. The quoted sentence no longer ends at the point in
"0.5" (`_SENTENCE_END`). **Replayed on the pilot: verdicts 13 → 18
of 19, answer correctness 52.6% → 68.4% (10 → 13), proportionate certainty 9 → 12; the phrases were written on
these drafts, so that flatters it.** On 86 other Oct 26 drafts: verdicts 64 → 74, correct answers 49 → 58 of
86 (fires on 24: 11 fixed, 12 no change, 1 broken). On the pilot's own Oct 26 drafts: no gain (2 fixed, 2 broken).
**Lowering everything to `weak` beat it on those 86 (78 verdicts, 62 correct, 64 changed): the labels lean
weak, so a rise in this metric alone proves little.** Still wrong: wt_002 (extraction dropped "people without
diabetes", so reviews of diabetics read as `supported`), and five the judge scores 3/5 "overconfident".
**Fresh 19-case pilot, 2026-11-06 (`--mode live`, report `20261106-150128`): correct answers 13 of 19 (68.4%), verdicts 16,
certainty 12, hallucination 0, citation correctness 87.3% (under its 90%), 6.5 h.** Without the rule the same drafts score 11
correct and 15 verdicts, assuming the judge ignores the badge, as the replay showed. It fired on 6: fixed wt_055 and wt_095, broke
wt_058's verdict, and changed nothing the labels count on wt_021, wt_044 and wt_061. **wt_061 is a false positive: "others found no
clear effect" describes a split, which is what `mixed` means.** It misses "not yet fully supported" (wt_082). The same 19 cases scored
12 and 14 correct in two Oct 26 runs, so 13 is no measured gain on them. No fresh draft said `supported`, though 14 had that ceiling.
**Reading all 19 (2026-11-07): 9 sound, 7 partial, 3 wrong (wt_002, wt_058, wt_087), and all three passed validation.** The judge's
3/5s are mostly right despite naming missing topics (wt_058, wt_087 right; wt_044, wt_082 partly; wt_090 wrong), so leave it alone; its
real flaw is false passes. **Extraction dropped part of the question in 5 of 19, and `query_understanding_accuracy` still reads 100%**:
"without diabetes" (wt_002, judged 5/5), "women" (wt_021), "528 Hz" (wt_055), "as retinol" (wt_090), "rosemary water" (wt_098). **An
activity or a pair as `product` searches as one literal phrase**: PubMed hits for hand-written alternatives, wt_025 0 → 147, wt_044 1 →
109, wt_058 3 → 105. So wt_058's 21 sources were all rightly off-topic, its article told readers the research does not support cutting
alcohol to lower blood pressure, and wt_025's template said "simply not been studied". **wt_087 said no large randomised trial shows
sunscreen prevents skin cancer while "Reduced melanoma after regular sunscreen use: randomized trial follow-up" (2011) sat in its pool,
outside the 22 sources the model was shown.** Traced 2026-11-07: second of 51 by similarity (0.863), 18th after the bonuses, 0.013 under
the top-12 cut, behind sun-behaviour reviews at ~0.78, because a review earns 0.15 to a trial's 0.08 and a 2011 paper no recency. Ranking
was left alone: one case. `claim_not_backed` now skips a sentence with "others" in it, which is wt_061's only change on replay.
**What followed changed extraction and every search for an unmapped name; its retrieval suites and one fresh pilot are measured (below).** Extraction now keeps who and what-versus in the claim, writes two things as "A or B", and uses a paper's short name (251 saved
questions, ~7 s each, no population invented). **PubMed drops the quotes from a phrase it has not indexed** and searches loose words,
unknown ones dropped: `"glazed-donut skin"` found 1,059,975 papers, on skin, and the singular/plural forms triggered it (`"simplified
skincares"`, 125,885). So names are tagged (`_name_term`): `"a b"[tiab]`, plus the words apart when two are 4+ letters, since a tagged
phrase alone emptied real topics ("whole-body vibration plate training": phrase 0, words 46, "glazed-donut skin" still 0; "one meal a
day": words 4,411, phrase 21). **OpenAlex was sent PubMed's field tags, which wreck its search**: "alcohol reduction" and blood pressure,
4 unrelated papers tagged, 8,407 plain, led by the right one; it gets them stripped now. **Measured 2026-11-08 with OpenAlex answering
on every case, the wider searches alone did not help the wellness suite**: passes 80 → 75 of 113, off-topic papers per case 2.26 → 3.57,
MRR 93.2% → 88.6%, recall@3 34.5% → 27.6% (the retrieval suite: 89 → 89, recall@3 33.0% → 39.1%, off-topic 0.62 → 0.69). PubMed's
off-topic papers fell 104 → 43, so tagging worked; the rise was OpenAlex (87 → 238) and Europe PMC (64 → 122). When a name is in no title
or abstract, PubMed and scoped Europe PMC rightly find 0, and the rest filled the list from full text: OpenAlex's plain `search` reads
full text ("bulletproof coffee": 497 works, gun adverts among them; 5 in title and abstract, all on the drink), and Europe PMC retried
unscoped ("under-desk treadmills": sit-stand desks). **So OpenAlex now searches `title_and_abstract.search` and Europe PMC never retries
in full text** (`providers.py`). Re-run on the 60 cases that had changed: wellness passes 24 → 35 of 44 (29 before the search change),
off-topic per case 6.84 → 1.98 (3.73), MRR 79.3% → 90.4%; retrieval suite off-topic 3.19 → 0.38. Gold papers ranked on those cases
6 → 6 and 4 → 3; wt_052 lost its gold trial ("Cold Showering") only because the name rule cut "with cold water" from "ending a shower
with cold water", leaving the subject "ending a shower", and full text had found it by luck. Both full suites need a re-run, since the
OpenAlex change touches every case. Still bad either way: wt_077 (ankle or wrist weights), wt_094 (glazed-donut skin). **Fresh 19-case pilot on all of it, 2026-11-08 (`--mode live`, report `20261108-093913`, no provider outages): correct answers 15 of 19 (78.9%, was 13), verdicts 16, certainty 14 (was 12), citation correctness 86.1% (under its 90%), hallucination 0, groundedness 97.9%, ~9 h with one timeout re-run.** Wrong: wt_023b (GLP-1 side effects, `weak`, judged 5/5), wt_082 (`mixed`, "not yet fully supported" still unread by `claim_not_backed`), wt_087 (sunscreen, the trial still outside the top 12), wt_098 (rosemary, `supported`). 85% needs two more. The wordings in `MESH_HINTS` for ice baths, alcohol
reduction, lemon water and glucose monitors were written on evaluation cases. **A prompt example gets copied**: "yoga before bed" turned
melatonin's "sleep latency" into "fall asleep faster" (basic_005), and a stretching one added an injury claim.
**OpenAlex's key buys $1 a day, 10 credits a search, so ~1,000 searches, reset at midnight UTC, and suites whose terms changed spend it
fast: read `provider_outages` before trusting a report.** Three suites on 2026-11-07 ran without it on 34, 41 and 94 cases, which read as a
regression in the search change. The pair's literal phrase led to name splitting and
`POPULATION_HINTS` (see "One field can hold several names"); this pilot's numbers predate them.
**A live case can pass `case_timeout_seconds` (1500) when little of it is recorded**: 5 of these 19 did
on first run, none of them caused by another run. Completed calls are recorded, so re-run them: delete
those traces, then `--resume`. `--rescore` also needs `--suite`, or it selects another suite's cases and
exits 2 with "no saved traces".

**A docstring in `domain/contracts.py` is prompt text.** Pydantic uses a class docstring as the
JSON-schema `description`, and `ollama_client.py` passes that schema in as the **generation
grammar** — so every word written there to explain a decision to a maintainer is read by the model
while it decides what to write. The docstrings argued for brevity, correctly and at length:
*"every bound here is a ceiling, not a target"*, *"optional and have no minimum — that is the
whole point"*, *"nothing in this system pads to length"*, and `sections` was described to the
model as *"Optional titled sections expanding on the evidence"*. Two of the first three articles
carried no sections at all.

Both audiences are legitimate and they are not the same one. The rationale stays in the
docstrings; `model_config = ConfigDict(json_schema_extra={"description": ...})` overrides what
reaches the model. `sections` still has **no `min_length`** and must never grow one — that would
be the padding instruction §6 refuses — but its description now states the prompt's own
evidence-to-count rule, whose low end is *none at all*, so thin evidence still yields a short
article. `test_the_generation_grammar_carries_no_developer_rationale` fails when a file name or a
brevity argument reaches the grammar again. This is the same lever as the `STUDY_TYPE_LABELS`
glosses: a local model mimics what it is handed far more reliably than it follows a rule about it.
The synthesis prompt itself opened with "You write short, evidence-checked articles" until
2026-09-29; the word went for the same reason. Its effect is not measured yet.

**All of the above was one root cause: `LLM_PROVIDER` was unset, so synthesis ran on
`llama3.1:8b`.** It was handed 13 systematic reviews, cited 2, wrote 110 words and said `weak`.
Synthesis is now `qwen2.5:7b-instruct`; extraction stays on the 8B (326 input / ~48 output
tokens, never the bottleneck).

**Sizing a local synthesis model on this machine: read `ollama ps`, not the model card.**
Measured 2026-09-07 against 17 GB of RAM at `SYNTHESIS_NUM_CTX` 32768:

| model | resident | processor | outcome |
|---|---|---|---|
| `qwen2.5:14b-instruct` | 15 GB | 27% CPU / 73% GPU | 7% memory free, ~580k pageouts, synthesis hit the 600s `ollama_timeout_seconds` without finishing |
| `qwen2.5:7b-instruct` | 6.4 GB | 100% GPU | ~20s per synthesis call |

**The PROCESSOR column is the signal.** Anything short of 100% GPU means the weights are
partially in system RAM, and the run does not fail in a way that names the cause — it times out,
and `ollama_client` reports it as "is `ollama serve` running?", which is the one thing that was
never wrong. `llama3.3:70b` (~40 GB) is not reachable here at all. If a bigger model is wanted,
drop `retrieval_top_k` to 10 and `SYNTHESIS_NUM_CTX` to 24_576 **together** — never one alone —
or move to the hosted path, which needs the workspace header fix in the config traps below.

**Articles are written for everyday readers in two steps: a prompt section, then a reader
edit** (2026-10-15/16, measured on the foam rolling article). The synthesis prompt's "Writing
for everyday readers" section alone took statistical shorthand ("SMD 0.74, 95% CI…") from 11
to 0, but left 15 of 19 sentences opening "A 2022 meta-analysis found" and dorsiflexion,
triceps surae and "chronic" unexplained: under seventy rules a local model grounds first and
writes plainly second. So SYNTHESIZE then runs the **reader edit**
(`pipeline/steps/reader_edit.py`, prompt `prompts/reader_edit.py`, `SYNTHESIS_READER_EDIT`,
on by default): one `edit_for_readers` call per paragraph (summary, beats, each paragraph of
each section), the article so far shown as context. Measured on the reference article: study-led
sentences 19 → 0, shorthand 11 → 0, 617 → 686 words, still 0 unsourced numbers. ~4–5 minutes
on llama-server (7–14 small calls). What to know:

- **A paragraph at a time, never the whole article.** Asked to edit the whole article under
  the synthesis prompt ("ceilings, not targets", "never pad"), Bonsai cut it from 588 words to
  204 and dropped a section. A paragraph cannot lose a section, and a refused one costs only itself.
  The paragraph goes in as numbered sentences and comes back as `sentences`: asked for a
  paragraph, the model reworded the first sentence and copied the rest.
- **It can never fail an article.** `paragraph_rejection` keeps the draft's paragraph when the
  edit changed its handles, added a number (even one the source has), moved a number beside a
  source without it, added a size or certainty word (`STRENGTH_WORDS`: it called an effect size
  of 0.53 "moderate", which neither draft nor source did), broke the sentence limit, cut more
  than 15% of the words, or left a stray square bracket. `article_rejection` checks the
  assembled whole, and VALIDATE then checks it as any draft.
- **One retry per paragraph, with the reason.** Refused: again from the draft, told why. Kept
  but still study-led or carrying shorthand (`leftovers`): again from the edit, told which
  sentences. The plainer kept edit wins (`score`); a worse one never replaces a better one.
- **"Explain once" is enforced in code** (`drop_repeated_explanations`): told, the model still
  explained "meta-analysis" atop every paragraph. It never drops a phrase holding a digit, so
  effect sizes stay. The summary is edited but not counted as explaining anything, since it is
  read apart from the body. The headline is not edited.
- **The guards cannot see a change of meaning.** One probe turned "interventions shorter than
  four weeks" into "shorter sessions" (the prompt now says a programme is not a session), and
  the edit reads a stated cause as the authors' interpretation ("found … due to" → "the authors
  think"), which the prompt asks for. The reviewer reading every article is the backstop.
- VALIDATE warns (`statistical_shorthand`, `evidence/readability.py`) on notation left in an
  article; a warning, never a failure or a revision, since the article is still correct.
- The eval records and replays it as tool `llm_reader_edit`, keyed by prompt, grammar and input.
- **End to end through the worker (2026-10-16)** the edit kept 8 of 8 paragraphs and took
  study-led sentences from 36 to 0 on a 1,318-word draft (+4% words, synthesis then ~19 min
  of a 32-minute run). The article still failed validation, on a misattributed year that the
  revision did not fix, which is Bonsai's known `unsourced_number` rate. Whether that first
  draft or the edit made the error is not recorded, so the guard was tightened from counting
  flagged numbers to comparing (number, source) pairs, which an edit can no longer add to.

- **5-case eval against the all-Bonsai baseline (2026-10-22):** study-led sentences 56/99 →
  11/108, shorthand 35 → 0, but validation 4/5 → 3/5. Both new failures were in the *first*
  draft, before the edit: the model wrote "0.36 hours" as "21 minutes" and two groups of 12
  and 11 as "23 trained men", correct arithmetic on numbers no source states. The prompt's
  example of "changes in everyday units" was the likely nudge; it now says to keep the
  source's unit and never do arithmetic. The edit also runs again after a revision, whose
  sentences had come back study-led.
  Re-run on the same 5 cases after both fixes: validation 5/5, hallucination flags 0,
  groundedness 0.94 (baseline 0.98), study-led sentences 11/89, shorthand 0. The eval's own
  number parser (`evaluators/text.py::numbers_in`) now reads spelled-out numbers through
  `source_numbers`, like VALIDATE: it had flagged "29 comparisons" against a source saying
  "Twenty-nine trial comparisons" as misattributed.

The number check learned two things on the way, both false positives that discarded a correct
plainer draft: compound numbers ("Twenty-six trials" reads as 26) and a hyphenated word before
the count noun ("26 high-quality trials" is a count of sources). That second rule let "-week"
count as the word, so "12-week trial" read as twelve trials and was never checked; a revision
then wrote "12-week trial" beside four sources that never said it, one of them a single-subject
study, and validated clean (2026-10-28, wt_079). `_COUNT_NOUN` now needs a space after the
number, and a time unit after it ("12 week trial") is no count either. The 107-case run's nine "N-week trial" phrases were all correct, so no new failures.

**A draft failing only on `unsourced_number` is repaired in place, not rewritten**
(`SynthesizeStage._repair`, 2026-10-23). Told to fix one sentence and copy the rest, the
whole-article revision cut a 12-source draft to 5 sources and a 15-source one to 4. The failure
names its sentence (with markers stripped: `numbers.plain_sentence` is the one form both sides
use), so only the paragraphs holding it go back, through the reader edit's call with the failure
as feedback. `repair_rejection` keeps a fix only if it moves no citation but the ones the failure
named, leaves no failed number in a sentence that cites nothing (the number check reads only
cited sentences, so dropping the marker would pass it), leaves no number beside a source without
it, and passes the reader edit's guards. A refused fix is tried once more with the neighbouring
paragraphs shown; after that the whole-article revision runs as before (`repair_failed` says
why). The reader-edit prompt says a repair note wins over "every number stays". Measured on six
trend articles: two repaired and passed (one sentence sat in two paragraphs; both are repaired
now), two still failed because Bonsai kept a number no source contains ("1.97 million", a 2025
for a 2026 paper) — a correct rejection.
When both tries fail, the failing sentences are dropped from the paragraph if it still cites what
it cited (`repair.sentences_dropped`); only then does the whole-article rewrite run.
**A repair is also refused when it leaves a finding cited to the wrong paper**
(`misattributed_sentences`, 2026-10-28). The final round only *warns* on
`misattributed_citation` (below), so the repair is the last place it can be stopped: in the
107-case wellness run, 4 of the 5 judged hallucinations were repairs that kept or introduced one.

**A cited sentence that shares few words with its sources is flagged, never failed**
(`low_source_overlap`, `validation.py::check_sentences_share_source_words`, 2026-10-24). The
number check cannot see a finding with no number cited to the wrong paper. Below
`LOW_SOURCE_OVERLAP` (0.35) of its content words in what it cites, after dropping digit-free
bracketed explanations, a beat-2 or section sentence goes to the reviewer as a warning.
Measured over 90 judged sentences: the one misattributed sentence scored 0.31, the lowest
supported one 0.44. One example set that threshold, so it stays a warning. `evidence/overlap.py`
is the one definition of the overlap; the eval's groundedness score imports it.

**A finding worded like another source than the one it cites fails, once**
(`misattributed_citation`, `check_citations_point_at_best_source`, 2026-10-26). The number check
is fooled when the wrong source happens to hold the number: wt_007 cited a memory study ("21
highlighted words") for an opioid trial of 10 + 11 patients. A cited sentence of at least
`MISATTRIBUTION_MIN_TERMS` content words fails when its own sources share under
`LOW_SOURCE_OVERLAP` of its words and another source shares at least
`MISATTRIBUTION_BETTER_SOURCE` and `MISATTRIBUTION_MARGIN` more. It is revisable and repairable
(`_REPAIRABLE`), and a warning in the final round, because word overlap is evidence, not proof.

**Article body is TipTap JSON, not markdown.** The model emits plain text with `[S1]` markers;
`services/tiptap.py::body_text_to_doc()` parses them into typed citation nodes at assembly.
Parse failure is a validation failure. **`CITATION_MARKER_PATTERN` in `domain/contracts.py` is
the only definition of a marker** — `CITATION_RUN_RE` beside it is built from it (the renderer
splits on it and SYNTHESIZE counts coverage by it), and `extract_handles()` uses it directly. Do not restate the pattern in either place: the parser and
the extractor disagreeing means `was_cited` is false for a source the article visibly cites, and
`check_beats_are_cited` flags a beat that is cited on the page. Both `[S1][S5]` and `[S1, S5]`
are accepted and collapse into one citation node; ranges (`[S1-S8]`) are not, inline. Anything
bracketed that is not a marker is a parse failure — checked by stripping valid markers and
looking for leftover brackets, because the old "not a valid marker" pattern missed unterminated
runs like `[S1, S5` and let them through as literal text. Validation walks the tree rather than regexing prose.
**Sources are full of square brackets, so the model is shown them round.** Measured 2026-09-29: a
draft copied `95 % CI = [-3.83 to -0.55]` out of an abstract into beat 2 and was discarded.
`render_source_block` turns every square bracket in a title, abstract or excerpt round (`_round`),
and the prompt says square brackets are for handles only. Only the rendering changes. The stored
text is the source's own. The parse runs in VALIDATE (`check_body_parses`), not only in PERSIST, so
`metrics.validate.passed` is true only for a draft that will actually be stored as
`pending_review`. Before this, PERSIST added the failure after VALIDATE had recorded `passed:
true`. The failure's `detail` holds the offending block's raw text, because a draft that does not
parse is stored with an empty document.

**The pipeline writes three block types and reviewers can add two more.** `paragraph` and
`heading` (always level 2, one per `ArticleBody` section — DESIGN.md §6) come out of
`body_text_to_doc`, plus the `image` node PERSIST hands it; a reviewer adds `image` and
`youtube` (§3.4b). Only the three beat paragraphs carry `attrs.beat`. Section paragraphs and
reviewer-added ones deliberately do not, which is what keeps `beat_text()` correct however much
sits between the beats — it addresses by `attrs.beat`, never by position.

**A new block type has to be declared in the console editor's `StarterKit`, or it is silently
destroyed.** TipTap drops nodes its schema does not know: with `heading: false` the editor
parsed a sectioned draft, discarded every heading, and the first autosave wrote an
`edited_content` with the article flattened — nothing about the saved document otherwise wrong.
This is the same failure `BeatAttribute.ts` exists to prevent for `attrs.beat`. The public
renderer needs a case for it too (`ArticleContent.tsx`), or it falls through and renders as a
paragraph.

**A retracted paper is refused, not downgraded — and `retraction_checked_at IS NULL` means
nobody asked, not "clean".** Three mechanisms, and mixing them up is how this breaks:
`is_retracted()` drops the paper in `RetrieveStage` before the cache — and since 2026-10-02
RETRIEVE also asks PubMed (`esummary`, via `PubMedRetractionSource`) about every paper with a
PMID that a *non-PubMed* provider supplied, because an OpenAlex record of a retracted trial is
typed `article` and was cited with a `supported` verdict; that screen fails open
(`retraction_screen_failed`); `classify_study_type`
returns `UNKNOWN` for one already cached; `scripts/check_retractions.py` re-checks cited sources
against PubMed + Crossref on a schedule, because retractions land *after* publication and the
ingest screen only ever catches what was already withdrawn when we first saw it. The schedule is
a host LaunchAgent, Mondays 07:00 (`./jobs/retractions.sh install`; log
`~/Library/Logs/youth/retractions.log`). Nothing ran it before 2026-10-28; the first run
checked the 106 sources cited by published and pending articles, all clean.

Two rules with teeth. **Retractions are checked *before* the publication-type map**, unlike
every other entry in `NEGATIVE_PUBLICATION_TYPES` — a retracted trial also carries `Randomized
Controlled Trial`, so under the normal "positive identification wins" rule it graded `rct`,
ceiling `supported`, and the cap existed only for papers it did not matter for. Don't move it
back. And **`RetractionVerdict` has three states**: a provider that failed, 500'd, or cannot
resolve an id leaves the paper *unchecked*, never clean. NCBI in particular returns an `error`
record rather than omitting an unknown id, and reading that as an empty `pubtype` list marked
unlookuppable papers verified clean — then wrote a timestamp that suppressed the next sweep.
Same principle as `retrieval/throttle.py`: a search that could not run must never look like one
that found nothing.

**A flagged article stays `published`.** The sweep sets `retraction_flagged_at`, which raises a
reader-facing banner and a console row. It does not withdraw. That is invariant #1 pointed the
other way — a human decides what the public sees, in both directions — and one retracted source
among several need not invalidate a conclusion. Nothing is deleted either: the source row may be
referenced by `article_sources`, and provenance outranks tidiness.

**Tokens are stored; cost is computed at read time and never written down.** The ledger is
`model` + four token columns on `pipeline_stage_runs` — that table is already one row per run ×
attempt × stage, which is the grain cost surprises actually have. `pipeline_runs` keeps a
rollup, written as an **increment** because `ctx` is rebuilt per attempt and assigning would
report the cheapest attempt as the whole run. Prices live in `llm/pricing.py` keyed by
**provider-namespaced** model id (`anthropic/claude-sonnet-5`), so a local model — genuinely
free — is never confused with a hosted one nobody has priced. Two rules that look like
politeness and are not: an unpriced model returns **`None`, never `0`** (they are both falsy
and mean opposite things, and the zero would be believed on the first run after someone edits
`SYNTHESIS_MODEL`), and a run total is all-or-nothing, because extraction alone is ~1% of a run
and would read as a plausible answer. Do not add a `cost` column — a stored price freezes
whichever number was current that day, with no record of which one it was.

**Tokens burned by a *failed* call are recorded too, and that write outlives the rollback.**
`LLMError` carries `usage`; the extract and synthesize stages record it before converting to a
`StageError`; `_abandon` writes it while rolling back everything else the stage did. The
rollback undoes our writes, not the provider's charge — and the case that matters is truncation,
which spends the entire 8K synthesis budget and produces nothing, so a zero here makes the
pipeline look cheapest exactly when it is burning the most. This is also why
`PipelineContext.record_usage` mutates in place instead of returning a copy: a raising stage
returns no context. `ctx.usage` is a **derived property** over `usage_by_stage`, not a field —
don't reintroduce a separately-summed one, the two drift the first time a call is added and
only one side is updated.

Embeddings are **outside** the ledger on purpose (~1% of a run, and widening
`EmbeddingProvider` changes its return type and every caller). Say "not measured", not "free".
**Generated images are outside it for a stronger reason**: an image is billed per render, and
this table prices four token columns. `IllustrateStage` therefore never calls `record_usage` —
a model id written against four zeros is exactly the "unpriced model reads as free" mistake
`pricing.py` exists to refuse. Count, seed, latency and model go into `metrics` instead, and a
run's `estimatedCostUsd` excludes image spend.

The two pre-existing runs were backfilled from the old `metrics` JSONB, and their stage rows sum
to ~328 fewer input tokens than the run rollup: extraction's *input* count was never in
`metrics` (only `"tokens"`, its output). Expected, not drift — don't "fix" it. Their `model` is
NULL, so they report an unknown cost rather than a guessed one.

**The theme is `styles/youth.css`; `styles/design-system.css` is vendored and read-only.**
The second is Modernist, copied from the Claude Design project — retune it *there* and re-copy,
because hand-edits are lost on the next sync and that project is what the readme and the
component pages render from. `youth.css` declares the `--ew-*` tokens components speak and
re-points Modernist's `--color-*` roles at them, so the focus ring and `::selection` follow the
active theme. Every Tailwind value resolves to a custom property: a hex in a component is a bug,
because it cannot follow the theme and is invisible to anyone retuning the system. Two
deliberate exceptions, both because the surface underneath is a photograph nobody controls —
feed-tile text and `.ew-tile-scrim` are fixed white on fixed dark, and the verdict *mark* is
dropped on an image tile because ink is invisible on a scrim and a white version would be a
second mark meaning the same thing. `features/console/controls.ts` carries the same pill-and-
rounded-field shapes as the public side, on purpose: a reviewer crossing between the two
surfaces should not have to relearn what a button looks like, and that file's own rule is that
those three controls staying consistent is a safety property.

**Frontend is a Vite SPA shaped for a cheap Next.js port.** Keep all data access in
`src/lib/api/*` framework-agnostic (plain fetch + typed contracts), keep `src/routes/`
mirroring a Next `app/` tree one-for-one, and keep `window` out of render paths above the
route level.

**Two route groups that share only `App.tsx`.** The public site (`/`, `/a/:slug`, `/about`,
`/join`, `/you`, `/contact`, `/privacy`, `/terms`) renders under `PublicLayout` — site bar, category drawer, mobile
tab bar. The review desk is at **`/review`**, is lazy-loaded, and renders `ReviewHeader`
instead. Nothing public links to it. That is chrome and discoverability, *not* access control:
a URL is not a secret, and what actually keeps readers out is `RequireAuth` plus the
router-level bearer check on every `/api/console/*` route. Public routes must never import
from `features/console`, and the desk must never render public chrome.

**The desk's "Show draft" preview imports the other way, on purpose.** `FeedPreview.tsx`
renders the *real* `features/feed/ArticleCard` among real published tiles at the feed's own
200px measure (`MasonryFeed`'s `columnWidth`), fed by `GET /console/articles/{id}/card` — which
runs the *same* `derive_card()` `approve()` runs. Both halves are deliberate: a console-local
tile clone, or a TypeScript port of `derive_card`, would be a second definition free to drift,
and a preview that drifts from what publishes is worse than none because it is reassuring
rather than useful. Console → feed is the permitted direction; the reverse is what the boundary
forbids. Every tile in the overlay is `inert` — `ArticleCard` renders a live Save button and
links to `/a/:slug`, which 404s until publication. The button flushes autosave before opening,
for the same reason Approve does.

**The review desk's "Need to be checked" tab is derived in the browser** (`features/console/
checks.ts`, rendered by `WhyItFailed.tsx`): the evaluation's per-case failure list, rebuilt from
what a live draft carries — validation report, source stances, and each cited sentence beside
its sources' abstracts (`ReviewSourceOut.abstract`, the only API addition). Retrieval precision
and the judge's hallucination call stay eval-only: they need a gold list and a model call per
sentence. Four things to keep: `article_sources` is one row per paper **per claim**, so checks
count *handles*, not rows; the "Not in the cited source" hint is a word match (numbers as whole
tokens, topic words by 5-letter stem, a stoplist for "found"/"after") — a pointer for the
eye, never a verdict; the "fixed" ticks are session-only reviewer bookkeeping keyed by sentence
text, so both tabs stay mounted (`hidden`) to keep them; and a new Tailwind colour token needs
`docker compose restart web`, because Vite does not reload `tailwind.config.ts`.
Failed-check text is `text-danger-ink` (`--ew-danger-ink`), the one red in the theme.

**Two accounts, two tables, two tokens.** `users` is the reviewer roster and
`articles.reviewed_by` is a foreign key into it, so a row there is a claim about who is
answerable for a published article. `readers` are self-service. One table with a role column
would put a correctly-set enum between a public signup and the review queue; two tables make
that unrepresentable. The tokens carry a `typ` claim (`reviewer` / `reader`) checked in
`decode_token_subject` **before** the account lookup, so a reader token presented to the
console is rejected as malformed rather than merely failing to resolve — the separation
survives a future change to either query. `lib/api/client.ts` picks which token to send from
the path, never from the caller.

**The browse drawer offers subject only.** It listed the four verdicts for a while and they
were removed deliberately: a standing menu of Supported / Mixed / Weak / No evidence is a
scoreboard, and offering it as a primary way into the feed invites browsing by score rather
than by subject — the framing the content rules exist to avoid. `?verdict=` is still a real
narrowing and still works from a shared link; the navigation just does not propose it.

**A reader account orders the feed; it never narrows it.** `FeedService.page` gains a leading
sort tier — 0 for a subject the reader picked, 1 for everything else — and the rest of the
feed follows below. Filtering instead would let an account quietly shrink the world, and it
would make every *unclassified* article (`subject IS NULL`) invisible to anyone with an
interest set. The cost is that the tiered sort cannot use `articles_feed_idx`; the tier is
therefore skipped entirely for a reader with no interests, which is every anonymous request.
Free-text search is `GET /feed?q=`, server-side over every published article's approved text
(`0001_initial.sql`, DESIGN.md §3.1): full text first, a trigram typo fallback only when that finds
nothing, labelled `match: "similar"`. The tsvector is built by the SQL function
`article_search_document`, called by `approve()` and by the migration's backfill — never in
Python, so the two cannot index differently. `_TYPO_TITLE` in `services/feed.py` must match
`articles_title_trgm_idx`'s expression exactly or the index goes unused.

**`subject` is reviewer-set and stays optional.** It is the product's one chromatic axis and
it cannot be derived: `product` is free text, and guessing would put a confident colour on an
unchecked classification. `PATCH /console/articles/{id}/subject` is deliberately separate from
the content autosave and legal *after* publication, because it drives a colour and a browse
listing rather than a word the reader was shown. An unclassified article renders in ink and
sits under "Everything" — the design's resting state, not a broken cell — so it must never
block publishing.

**Subjects are editorial categories, not kinds of object** (2026-10-01): Fitness,
Nutrition, Supplements, Sleep and recovery, Lifestyle, Preventive health, General health,
Wellness, Other. `other` is a reviewer's answer ("none of these fit") and is not `NULL`
(unclassified). They are the drawer's filter, the readers' interests and the tile hue, so a
new category is an enum value in `domain/enums.py`, a migration swapping the Postgres type,
and a hue in both themes of `youth.css` plus `tailwind.config.ts` and `subject.ts`. They no
longer choose the generated picture's props: `imagery/prompt.py` keeps its own `Motif`, and
`_SUBJECT_MOTIFS` maps only Supplements and Nutrition onto it.

**The feed tile's picture is derived, like its headline and excerpt.** `derive_card()` takes
the first `image` node from the approved body into `articles.card_image` at publish time, so a
tile cannot show a picture the article does not contain. `youtube` nodes are excluded on
purpose: a video thumbnail lives on a third-party host, and deriving one would put a YouTube
request on every feed render — the same tracking the article page's click-to-load facade
exists to avoid. `NULL` is the normal case and the feed draws a typographic tile.

**The one picture not literally in the body is the generated cover, and a pairing rule is what
keeps the rule above true.** `ILLUSTRATE` draws two frames from one prompt with one seed — a
landscape `lead`, written into `original_content` as an ordinary image node, and a portrait
`cover` for the feed tile, which lives only in `articles.generated_imagery` (the tile shapes in
`tileRatio()` are all 1:1 or taller, so `object-cover` on a landscape lead throws its sides
away). `derive_card` reaches for the cover **only while `lead.src` is still the document's first
image**. Replace that picture, delete it, or put another above it and the pairing breaks and the
card falls back to ordinary derivation. It fails toward "no cover", never "wrong cover".

Be precise about what the pairing buys, because two stronger claims are **false**. The frames
are *not* one photograph at two aspect ratios: measured 2026-08-24, the same prompt and seed at
two sizes give visibly different compositions, and the provider `auto` resolves to (`nscale`)
ignores the seed entirely, so even two identical requests differ. And they are no longer even
guaranteed to come from one *generation* — `POST /console/articles/{id}/illustration` takes a
`frames` list, so a reviewer can redraw either alone. What the rule still guarantees is the part
worth having: the tile's picture was drawn for this article under the same locked claim-free
prompt builder, and is discarded the moment the article stops carrying its partner. Neither
frame asserts anything, so neither can assert what the other does not — which is exactly why
redrawing one and not the other is safe. If they ever need to be provably the same photograph,
set `IMAGE_COVER_MODE=crop` — **the default since 2026-09-30**. It renders the landscape
lead once and centre-crops the 3:4 cover out of it (`imagery/encode.py::crop_to_ratio`). That
is one billed render instead of two, and in that mode everything above about differing
compositions stops being true. A Regenerate press in crop mode always redraws both frames and
counts as one render against `REGENERATE_BUDGET`. That predicate is also where the cover gets
its `MEDIA_SRC_RE` check: it is not in the document, so
`assert_media_is_ours` — which walks the document — never sees it.

**Because either frame can be redrawn alone, provenance lives on the frame, not the pair.**
`GeneratedImage` carries its own `prompt`, `negative_prompt`, `model` and `seed`; `Illustration`
is just `lead` + `cover`. They were shared fields while drawing either meant drawing both, and
after a one-frame redraw a shared prompt would be a true record of one picture and a false one
of the other — which is worse than no record, because it still answers when asked. Rows written
before the move keep their top-level fields; `Illustration`'s `model_validator` pushes them down
onto both frames on read, so nothing is lost and nothing has to be migrated. A frame's own value
always wins. Two consequences worth knowing: `IllustrateStage` reads `illustration.lead.prompt`
for its metrics (identical to the cover's on that path, one call and one seed), and the console's
spend budget counts **renders, not presses** — `REGENERATE_BUDGET` is 24 per ten minutes, the
same ceiling as the 12 two-render presses it replaced. A one-frame redraw on an article with no
imagery at all is a **409**, not a quiet upgrade to both: there is no other frame to keep, and
billing two renders to someone who asked for one is the mistake the budget exists to prevent.

**Image generation never fails a run, and that is the deliberate opposite of the throttle
rule.** A picture is decorative; an article without one is publishable and already renders as a
typographic tile. `IllustrateStage` catches every image failure, records a `cause`
(`not_configured` / `disabled` / `provider_error` / `unexpected`) in `metrics.illustrate`, and
returns the context untouched. Compare `retrieval/throttle.py`, which must fail hard because
recall lost to a 429 reads as a correct, cautious answer. The rule is *fail on the degradations
nobody can see* — a missing picture is visible on the reviewer's very next screen.

**The image prompt is assembled here, never by a model, and the verdict never reaches it.**
A picture of someone looking healthier beside a supplement is an efficacy claim no citation
backs, made in the one register readers do not read critically. The headline is excluded for the
same reason: it is a *claim sentence*, and an image model handed a claim tries to depict it.

**A person may be in the frame, on the one path where the subject is an activity.** The line is
depicting the subject versus depicting a *result*: a protocol **is** something someone does, so
a body mid-stretch on a mat or a forearm mid-lift is a photograph of what the article is about.
A body beside a jar, a bowl or a tube is the other case — nothing in frame is doing anything, so
the only thing the person can be communicating is an outcome. So people are gated on the motif
(`_PEOPLE_MOTIFS`, currently `PROTOCOL` alone) rather than switched on globally, and adding a key
there is a decision about claims, not styling. Two guards go with it. **Only a confident signal
opens the gate** — a motif matched against `product` itself (a reviewer's category maps to
no `PROTOCOL` motif, so it cannot open the gate on its own); a motif
inferred from the *topic* does not, because topics name outcomes and "magnesium for sleep"
matches `sleep`. And `EXCLUSIONS_WITH_PEOPLE` replaces the body ban rather than dropping it: the
clinic, text, branding and before-and-after clauses are **identical** in both, because none of
them was ever about whether a person was present. What is newly forbidden is the body being
*displayed* — a physique, a transformation, eye contact with the camera — which is the form the
efficacy claim takes when a person makes it.

**`imagery/prompt.py` splits on one line: treatment locked, composition free.** `TREATMENT`
(palette, matte surfaces, register) is fixed and applies to every render, both paths — that is
what makes twenty articles read as one publication, and it is also what stops the people renders
from becoming stock fitness photography. Framing, arrangement, light and surface are chosen by
the **seed** from a per-path vocabulary (`_FRAMINGS`/`_ARRANGEMENTS`/`_SURFACES` for objects,
`_PEOPLE_FRAMINGS`/`_POSES`/`_PEOPLE_SETTINGS` for a person, `_LIGHTS` shared), giving 1,296
compositions either way. Do not add a composition clause to `TREATMENT`; that is exactly how this
module once produced the same beige photograph for every article, with three words of eighty
varying — and the genre word (`Still life` / `Unposed documentary photograph`) was moved out to
`_Path.genre` for that reason, being composition hiding in the treatment string.

**The motif is a fifth axis, and swapping it is the only change with real amplitude.** Each
subject held one string, so every article the hint table called a `SUPPLEMENT` — most of a
wellness feed — rendered the same jar of capsules, and the composition axes could only
re-photograph it: five hundred angles on one still life read as one picture. `_MOTIFS`,
`_PEOPLE_MOTIFS`, `_DEFAULT_MOTIFS` and `_DEFAULT_MOTIFS_UNNAMED` now hold `_MOTIF_RADIX` (4)
variants each — **every tuple the same length, checked at import**, or the digit stops being
uniform across subjects — for 5,184 prompts (`_PERIOD`). It is deliberately the **slowest**
digit (`_MOTIF_STRIDE` is past all four composition axes), so neighbouring seeds move the camera
and distant ones change the objects; a seed is an article's stable composition, not a reroll.
The noun also moved to the head of the sentence (`Still life of Ashwagandha KSM-66: …`) from a
trailing `, suggesting <noun>` — arriving after three concrete object nouns that had already
specified the frame, it was doing almost nothing. The default-motif path passes no head noun,
because that scene already names it.

**A product that names no motif gets a scene for what the article is about**
(`_OUTCOME_SCENES`, 2026-10-23): ten areas (gut, back, heart, brain and mood, sleep, skin,
joints, longevity, weight and blood sugar, immunity), each `_MOTIF_RADIX` object-only variants,
picked by hint words in `product`, then in the claims (`build_prompt(outcome=...)`), never the
topic. Only the scene's own words reach the prompt, never the claim, and no head noun is used,
since "cancer survivorship care" at the head invites a clinical picture. Before it, "Back pain"
fell through to "objects associated with back pain" and FLUX drew pharmacy bottles with garbled
labels. A product motif still wins (a magnesium article keeps its jar).

**Tonal range comes from `_SURFACES` and `_LIGHTS`, never from hue.** Surfaces vary material
*and value* (pale paper through dark oiled wood); lights run from even overcast through hard
directional sunlight to near-dark. The original five pale neutrals under four flavours of soft
daylight were nearly a constant at feed-tile size on a four-step distilled checkpoint. This is
the only range available once the palette is locked — which it stays.

**The strides are a mixed radix, not coprime numbers.** Each is the product of the axis lengths
*before* it (`_STRIDES` is derived from `_AXIS_RADIX`, so it cannot drift when someone adds a
seventh framing — widening `(5, 5, 4, 5)` to `(6, 6, 6, 6)` touched no stride), which makes the
axes exact digits of `seed % 5184` — every combination once per 5,184 consecutive seeds. The previous `(1, 7, 53, 401)` was chosen for coprimality between the
strides, which buys nothing: stride 7 against a 5-option axis gives
`(seed % 5, seed // 7 % 5) == (r % 5, r // 7)` for `seed = 35q + r`, so 10 framing×arrangement
pairs came up exactly twice as often as the other 15 (χ² 49073 on 16 dof over 400k seeds; 511 on
499 after). The quantity that matters is each stride against the product of the preceding axis
lengths — which is also why both paths must keep the same option counts, checked at import, and
why the motif digit's stride is the product of all four axes rather than a number that looked
big enough. Sampling seeds is no longer an adequate content check at 5,184 combinations, so
`test_no_option_in_any_vocabulary_names_a_claim_shaped_thing` checks every string in every
vocabulary directly: it fails when a banned word is *written*, not when a seed selects it.

**The seed selects the prompt, not the sampler, and that distinction is the whole design.** The
provider ignores the seed it is sent (measured — see `seed_for_run`), so randomising harder
cannot help: those identical renders already came from different noise. The prompt was the
narrow thing. `seed_for_run` therefore gives an article a stable composition across retries and
`fresh_seed` makes Regenerate genuinely recompose, both by our own selection.

Three traps when editing. `subject` is `None` on every pipeline call (reviewer-set; the article
row does not exist yet), so `infer_motif_subject` — a keyword map over product, then topic and
ingredients — picks the motif. **That is not a classification**: it chooses which objects get
photographed, is never shown to a reader, and never writes `articles.subject`, which stays
reviewer-set because a wrong subject is a false statement on the page while a wrong motif is a
slightly odd still life. **Its arguments are in trust order and it stops at the first field that
matches**, which is load-bearing rather than tidy: it used to join product + topic + ingredients
into one haystack, so a vague word in a low-trust field outranked a precise one in a high-trust
field. `SUPPLEMENT` is checked last and `PROTOCOL` holds `sleep`, `exercise`, `training` and
`therapy` — which is how articles name their *outcome* — so "Magnesium glycinate" / "magnesium
for sleep quality", "Melatonin" / "melatonin for sleep" and "Whey protein" / "protein for muscle
after exercise" all drew a towel and a timer. Since topics routinely name an outcome that was
the common path for supplements, not an edge case. Within-list order cannot fix it; only the
field tiering can. **`negative_prompt` is close to a no-op on FLUX.1-schnell** — it is
guidance-distilled at `guidance_scale=0.0`, so the exclusions ride in the *positive* prompt and
must stay there; the tests fail if they are "tidied" into the negative channel. And the
exclusions are deliberately narrower than they look — *branded* packaging, and no ban on
numbers — because several motifs name a jar, a tube or a timer, and a blanket ban contradicted
them on every render.

**Trend discovery proposes; it never enqueues.** `discovery/scan.py::run_scan` counts MeSH tags on
newly-indexed PubMed records, ranks substances by acceleration, and writes `discovery_candidates`.
A reviewer promotes one from the console and *that* creates the run. This is invariant #1's
reasoning moved one step earlier — a human decides what gets written, not just what gets published
— and it is why there is no `--auto-enqueue` flag. The scan itself makes **no model calls at all**,
so it costs nothing; the spend is entirely in what a reviewer chooses to promote, and
`pipeline_runs.origin` is what makes that spend one `WHERE` against the token ledger.

**Two callers, one scan.** `scripts/scan_trends.py` is argparse and printing around `run_scan`;
`POST /console/discovery/scan` is a background task around it, driven by the desk's Run scan
button. The endpoint must **not** shell out to the script — `scripts/` is deliberately outside the
deployed image ([.dockerignore](backend/.dockerignore)), so that button would work on a laptop and
500 in a container. Exposing a manual trigger at all is safe only because of the paragraph above:
a scan proposes, so the button spends PubMed requests and nothing of ours. `discovery/manual.py`
holds the runner, and three things about it are deliberate — it is **single-flight per process**
(two concurrent scans double this process's draw on NCBI's per-IP ceiling, which the worker shares,
to compute the same answer twice; the ledger's idempotence is what makes the per-*process* scope
merely wasteful rather than corrupting), its state lives **in memory, not in `discovery_scans`**
(that table is the window ledger — `next_window` reads only its succeeded rows — so a press must
not write to it), and a scan that raises lands on that state as an error string rather than as an
unhandled task exception. A restart therefore reports `idle`; `last_scan_at`, read from the ledger,
is what still answers "when did one last finish".

**The scan runs on a timer, and the button is now the exception rather than the only way.**
`discovery/schedule.py` starts a task in the **API process's** lifespan that fires
`manual_scan.start()` when the ledger's last *succeeded* scan is older than
`DISCOVERY_SCAN_INTERVAL_HOURS` (24). Automating this side is safe for exactly one reason, and it
is the paragraph above: **a scan proposes and never enqueues**, so the timer spends PubMed
requests and no tokens at all — the whole generative bill still sits behind a reviewer pressing
Generate draft. Automating the *promotion* would be a different decision entirely, and there is
still no `--auto-enqueue`. Four things about it:

- **It goes through `manual_scan`, not a second path.** One runner means the single-flight guard
  already covers press-versus-timer, and the desk's status line describes whichever scan is in
  flight without knowing what started it. A `ScanAlreadyRunning` from a race is caught and the
  tick does nothing.
- **Due-ness is read from the ledger, never from a timer since process start.** So a restart does
  not scan, a deploy loop does not scan per deploy, and a slot missed while the process was down
  heals on the next tick. Same shape as `discovery_overlap_days`.
- **The loop cannot die on a bad tick.** The symptom of a dead scheduler is *nothing happening*,
  which reads exactly like a quiet fortnight — so every tick is wrapped, and a naive `finished_at`
  from an older row is coerced to UTC rather than raising.
- **It lives in the API process because `manual_scan` does.** Running more than one API replica
  would give each its own scheduler and its own single-flight scope: wasteful, not corrupting,
  because the observation ledger is idempotent — but scaling out wants an advisory lock first.

**A suppression is explained on the desk, not in a terminal.** `ScanReport.suppressed` carries
every held-back topic with its reason, and `ManualScanState` used to drop it, leaving the note to
end "Run the CLI for the per-topic list" — a dead end on a deployed stack, since `scripts/` is
outside the image by design. The one screen that needed the list was the one screen that could not
have it. It now renders under the scan note: "0 proposals" and "0 proposals, and here are the three
I am holding back" are different screens, and only the second is trustworthy when it is empty.

Six things about it that are load-bearing:

- **Counts are `COUNT(DISTINCT pmid)` over a ledger, never incremented counters.** MeSH indexing
  lags PubMed entry by days to weeks, so every scan must re-read weeks a previous scan already
  covered — `discovery_overlap_days` (21) against a 14-day window. `discovery_observations` is
  keyed `(descriptor_ui, pmid)` and written `ON CONFLICT DO NOTHING`, which makes that overlap
  free, makes a missed cron slot self-heal, and makes the whole script idempotent. Verified
  against the live index: a second `--apply` run writes 0 observations and refreshes the same
  candidates. Records bucket on `entrez_date`, not on which scan found them, so a backlog cannot
  read as a surge.
- **The window is measured on `datetype=edat`, and the harvester is deliberately not a
  `ScholarlyProvider`.** `ScholarlyProvider.search` is contractually required to drop records with
  no abstract, and discovery wants exactly those — a MEDLINE record with no abstract carries its
  full heading list, and there were **21,689** such records in the supplements and vitamins nets
  when this was built. `discovery/harvest.py` therefore has its own esearch/efetch parse and shares
  only the *throttle* — which it must, because NCBI's ceiling is per IP and shared with the worker.
  Same shape as `scripts/check_retractions.py`. It also uses `sort=date`, not the pipeline's
  `sort=relevance`; putting either on a shared `SearchQuery` would let a future caller silently
  degrade every article's evidence base.
- **When the desk looks empty, the floor is the suspect, never the cap.** `rank_candidates` is
  asked for `limit * 3` precisely so suppression can drop already-decided topics and the next-best
  ones get pulled up — that behaviour has always been there. `discovery_max_candidates` (6) is a
  reviewer-flooding ceiling and is almost never what binds. Measured 2026-09-07 with the floor at
  4: the substance counts over a live 14-day window ran 9, 5, 5, 4, then 3, 3, 3, 3, then a long
  tail at 2 and 1 — so **four substances in the whole corpus** cleared it, three of their angles
  were already promoted, and a desk of 8 showed 3. At a floor of 3 the pool is ten and the desk
  fills. The dry run's "N of M above the floor" line is the number to read before touching either.
  Lowering it also makes the gaps in the hand-maintained `STOPLIST_UIS` visible — the first scan at
  3 proposed `antiviral agents for antioxidants`, two category abstractions pointed at each other.
- **No candidates until the baseline is deep enough.** Below `discovery_min_baseline_windows` (4)
  the scan writes observations and proposes *nothing*, with no volume-ranked fallback — a first
  scan can only rank by raw volume, which proposes vitamin D, creatine and omega-3, i.e. the three
  things a reviewer would have named unaided, arriving with the authority of a ranking.
  `--bootstrap --apply` builds it (~3 min, 6 months; the scorer only reads
  `discovery_baseline_windows` buckets, so a deeper backfill writes rows nothing reads).
- **`score = log2(lift) × log2(1 + n) × quality_weight`, and the product is the point.** Lift alone
  is dominated by tiny denominators (1→4 beats 30→90); volume alone re-proposes vitamin D forever.
  Every constant was guessed before any data existed, which is why the console shows the arithmetic
  ("17 papers, usually 3") and not the score.
- **A descriptor a seed query *names* can never be a finding**, and those anchors live on the seed
  in `discovery/seeds.py`, not in the stoplist — so editing a seed's terms cannot leave its own
  subject topping its own ranking. Measured: before this, "Plant Extracts" ranked first among
  botanicals with 146 of 665 papers. Substances are separated from biomarkers by MeSH *intervention
  qualifiers* (/administration & dosage, /therapeutic use…) plus `<ChemicalList>` membership, with
  `BOTANICAL_UIS` and `BIOMARKER_UIS` covering what that rule cannot see. Category abstractions
  (`Antineoplastic Agents`, `Capsules`) pass the rule cleanly and are stoplisted by hand; the real
  fix is a MeSH tree lookup against D27, which is a network hop per descriptor.
- **One proposal per (substance, *angle*), not per substance.** "Omega-3 for muscle
  recovery" and "omega-3 for skin" rest on different papers and reach different verdicts, and
  under the old identity promoting either silenced the substance so the other was never offered.
  Three things keep this from becoming "one substance, eight ways":
  `discovery_max_angles_per_substance` (2) caps exposure; an angle must clear
  `discovery_min_papers_per_angle` (3 — it sat below the substance floor until that floor came down
  to 3 on 2026-09-07, and equal is safe: it only means a substance at exactly the floor needs *all*
  its papers on one outcome to earn an angle, and otherwise falls back to a bare topic. Never set it
  *above* `discovery_min_papers`, which is unreachable by construction); and **`is_usable_angle`
  requires the outcome to
  map into `OUTCOME_HINTS`**, because everything not identified as a substance falls through to
  "outcome" and that is far too permissive for naming an article. Real proposals it now refuses:
  `vitamin d for cross-sectional studies` (a study design), `vitamin d for vitamin d deficiency`
  (tautological), `catechin for tea`. A refused angle is not a refused substance — it falls back to
  a bare topic, exactly as before angles existed.
- **Angles are read from the ledger over `discovery_angle_lookback_days` (90), not from the scan's
  own window.** Two different questions with two different timescales: "is omega-3 surging?" is a
  fortnight's question, "what is omega-3 studied for?" is a slow fact. Measured 2026-09-06 — a
  21-day window yielded four usable angles across the whole corpus, all on two papers; sixty days
  yielded caffeine against strength, endurance, cognition and heart rate. A sub-topic is a slice of
  an already-small count, so a short window structurally cannot see one. Costs no API requests.
  An angle's outcome must be a descriptor whose current `kind` is `outcome`, not merely an
  observation with `is_substance` false: stoplisted descriptors carry that flag too, and
  "Antioxidants" became "dietary fiber for antioxidants" that way.
- **The trend is counted over the trailing `discovery_window_days` only, not over everything
  harvested.** The harvest spans the overlap so late-indexed records reach the ledger; counting
  them into `current` compares a 21-to-35-day span against 14-day baseline buckets and inflates
  every lift by the ratio. It was doing exactly that until 2026-09-06 — vitamin D read as flat
  (1.0x) when it was in fact declining (0.8x). Both numbers look reasonable alone, which is why it
  survived a live run and a full test suite.
- **`discovery_candidates_one_live_per_angle` is the anti-repeat guarantee.** A trend that
  emerges keeps clearing the bar, so the scan writes `ON CONFLICT … DO UPDATE` and a re-detected
  trend *refreshes* its row rather than appearing three times at three scores. The index is partial
  on `proposed` so history is unconstrained — which is what `suppression_reason` reads: a promotion
  that produced an article suppresses permanently, a dismissal until **both** the cooloff elapses
  and the paper count has doubled. Time alone would let a slow-burn topic nag every fortnight.
  Suppression is keyed on the angle's *spoken keyword*, not its descriptor UI: MeSH is granular
  enough that "Skin Aging", "Skin Physiological Phenomena" and "Skin Absorption" are three UIs for
  one question, and a per-UI rule would let a dismissed angle return under six names. The index
  keys on the UI because that is the duplicate row it can see; the keyword is the editorial
  judgement on top.
- **Zero proposals is the ordinary outcome, and the scan now says why.** A floor, a quorum and a
  suppression rule exist to make most fortnights propose nothing — but on the console "0 proposals"
  read exactly like a scan that had failed. `_suppression_note` folds the count into
  `ScanReport.notes`, which the desk already renders. Without it the most common cause of an empty
  desk was the one cause the desk could not display.

The sharpest risk is `UnanchoredQuery`: a promoted topic that leads extraction to return an empty
`ingredients` and a vague `product` dies at a **non-retryable** stage failure, having spent the
extraction call. Topics are composed as `"<substance> for <outcome>"` with the substance bare and
first for that reason, and `tests/unitTest/test_discovery_topics.py` pins that every botanical still anchors
a query. It fails *visibly* — the run shows `failed` — so it costs one call, not a wrong article.

**The research agent (`app/research`) is the second proposer, and it proposes too.** The MeSH
scan asks what researchers are publishing more of. The research agent asks what *people* are
getting interested in, and since 2026-10-12 it is two halves in one LangGraph run
(`research/graph.py`):

- the **Trend Search Agent** (`research/trends/`) collects from publisher RSS feeds, Google
  Trends, GDELT, news search and web search (and YouTube when keyed), folds
  duplicates and syndicated copies into stories, clusters topics, scores them on momentum,
  cross-source coverage, audience, freshness and editorial fit, and ranks them — **with no
  scientific evidence anywhere in the score**;
- the **evidence check** (`research/evidence.py`) asks PubMed and Europe PMC about the trend
  agent's best topics only, and is the only thing that can mark a topic `selected`.

It writes `research_candidates` and never enqueues: "generate 4–6 articles weekly" was
specified, and the decision taken was automatic research with a human pressing Generate draft.
DESIGN.md §4b has the design. What to know when editing:

- **One entry point, two triggers.** `research/service.py::start_research_run` is called by
  `POST /console/research/runs` (the desk button, reviewer JWT) and
  `POST /api/automation/research/runs` (n8n, `X-Trigger-Token`). n8n holds the schedule, the
  parameters (`RESEARCH_WEEKLY_PARAMS`) and the notification (`RESEARCH_NOTIFY_URL`), and
  nothing else. Do not put research logic in a workflow, and do not route the desk button
  through n8n: it must work while n8n is down. `mode` (weekly/manual) is metadata only. **No
  request parameter can switch the evidence check off**; a trigger may narrow sources and raise
  thresholds, never widen or lower them (`ResearchConfig.resolve`).
- **Single-flight is an index now** (`research_runs_one_active`). The SELECT
  check in `start_research_run` names the run in the way; the partial unique index is what
  holds when two triggers race past it, caught as `IntegrityError` inside a savepoint → 409.
- **Runs execute in the pipeline worker**, sharing NCBI's per-IP ceiling with article runs, so
  a queued draft waits behind a research run (6.4 minutes measured end to end; collection is
  bounded at 5 and enrichment at 4 — see "Exercised" below).
- **The trend phase never sets `selected`.** The desk polls every 10 s and renders Promote
  buttons for `selected` rows; a trend pick marked selected before the literature check would
  have been promotable for minutes. Trend-ranked topics stay `candidate` with
  `in_evidence_pool`; `research_science` makes survivors `shortlisted`; only the evidence
  check's `select` makes them `selected`. `test_nothing_is_selected_before_the_evidence_check_has_run`
  pins it. **Backfill routes to `research_science`**, not `deep_research`: backfilled topics
  have no stage-1 counts.
- **The trend output is frozen at the end of `select_trends`** (`state.trend_summary` →
  `research_runs.summary`) and served by `GET …/runs/{id}/result` in snake_case — a documented
  exception to the CamelModel convention, because it is the agent's output schema
  (`TrendSearchResult`). Read time overlays each topic's current status and the evidence gate's
  outcome as a *status* (`downstream.evidence_gate`); every topic says
  `scientific_assessment: "not_assessed"`. `test_trend_output.py` walks the result and fails on
  any evidence-score key.
- **`research/trends/` must not import science or generation code.** `test_trend_scope.py`
  checks direct imports (PubMed, Europe PMC, the query builder, the pipeline, `domain.contracts`,
  `evidence.py`); transitive ones are allowed, since the throttle imports retrieval's base types.
- **The trend score (`trends/scoring.py`, `trend-v1`) is pure and reproducible.**
  `score_from_metrics(TopicMetrics, weights)` reads nothing else, and both are stored on the
  candidate, so a stored score can be recomputed. Weights are integers summing to 100
  (validator), so "60% measured" is exactly 0.6, never 0.6000000001 short.
  - **Missing data is reweighted, with a floor.** A dimension with nothing measured is left out
    and the rest renormalised; `data_coverage` is the measured share of the full score,
    counting sub-signals *within* each dimension. Below `research_min_data_coverage` (0.6), or
    with no momentum at all, the topic gets **no score** and is discarded "insufficient data".
  - **A sub-signal only counts when it was applicable**: its source enabled and configured
    (`TopicMetrics.applicable`). A deployment without a YouTube key is not marked down forever;
    a source that was configured and *failed* does lower coverage, which is the point.
  - **Count growth uses a prior of 5 on both windows** (`count_growth`): 0→2 articles is 62,
    not a surge. Trends' 0-100 index keeps the old +1 smoothing.
  - **Unknown is never zero**: a page that filled up leaves the earlier window `None`;
    coverage is `coverage_measured=False` (not 0 outlets) when no news-like source answered;
    undated items count toward coverage and are left out of freshness.
- **Independence is counted over stories, never items** (`trends/documents.py`): exact URL →
  canonical URL (named tracking parameters only — `?id=` and `?p=` identify articles) →
  headline → near-duplicate headline at Jaccard **0.8**. It was 0.6 for a day and merged
  "Readers ask whether *creatine* works" with "…whether *ashwagandha* works": templated
  headlines differ in exactly the subject. A story's outlet is the *topic's own* earliest item
  in it, not the story representative, which may be another topic's copy.
- **Headlines reach triage as phrases, not sentences** (`trends/phrases.py`): 1–3 words
  naming something specific (a non-generic lexicon word, or "vitamin d"-style qualifier).
  **One outlet is enough to be considered** (`research_phrase_min_publishers`): measured
  2026-10-12, a week of 356 feed stories from 12+ outlets had no specific wellness term in two
  outlets' headlines, and a two-outlet floor found nothing. Cross-source is enforced at
  selection (≥2 independent outlets after enrichment), where it measures coverage, not wording.
  A specific *seed* term ("creatine") is kept for triage to angle; a generic one ("sleep") is
  the category.
  Triage was measured on short queries in batches of 25; whole headlines would re-create the
  "General Wellness and Health" lumping. The prompt changed minimally ("rising on Google, and
  phrases from recent news headlines"; "news events *with no health question*") and gets two
  example headlines per group as context.
- **What triage left out is asked again once, then set aside** (`triage_or_fallback`).
  Measured 2026-10-12: qwen2.5:7b omitted 16 of 75 groups, and on the raw query "soulja boy
  running for president" (seed "running") ranked fourth. A group from a batch whose *call*
  failed keeps the deterministic fallback — it was never judged.
- **After triage, three deterministic guards** (`trends/cluster.py`, `trends/graph.py`), each
  from the same live run:
  1. a candidate counts only items behind queries that **share a word with its subject or
     outcome** — triage handed "coneflower for immune support" a phrase about carbs, and its
     supporting sources were two articles about carbohydrates;
  2. a bare subject naming a category (a seed, or only `GENERIC_WORDS`: "protein", "strength
     training", "weight loss") with no outcome is **too broad for one article**;
  3. products and shops are shopping intent (`shoes`, `headphones`, `store`, `brand`… in
     `normalize._NOT_A_TOPIC`): "running shoes" ranked second.
  A topic triage filed under an unrequested category is kept under its **seed's** category
  when that one was requested (narrowed to supplements, triage filed creatine under nutrition).
- **A topic bundling two subjects keeps only its own queries** (`triage._accepted`, 2026-10-23):
  Bonsai filed "zone 2 running" under "fish oil" and "Alzheimer's drug trials" under
  "diabetes". Groups sharing no word with the subject go back unclaimed (and are re-asked),
  and the topic is renamed "<subject> for <outcome>". Several claims about one subject all name
  it and stay together; a topic none of whose groups names its subject (a synonym) is kept.
- **Google Trends' rising lists carry spam** (measured 2026-09-30: "learn golang", "online
  banking review" at +7,000% under "fitness"). The defences, in order: `normalize.is_on_topic`
  and the shopping words; triage in batches of 25 that cannot make a topic out of more than 6
  groups or with the subject "various"; the guards above; the trend thresholds (≥2
  independent outlets, `research_min_independent_sources`); and last the evidence gate.
  **Do not rely on the evidence gate alone**: deep research gave "online banking review"
  *moderate* evidence, because the extraction model invented plausible claims.
- **Sources** (`trends/sources/`, one `TrendSource` protocol, built in `research/factory.py`):
  every adapter raises `ProviderUnavailable` rather than returning `[]` for a failure, with
  `permanent=True` for a rejected key or a spent quota. Per run, a source gets
  `research_max_requests_per_source` calls, a phase deadline, and a circuit breaker (three
  failures in a row and it is not asked again). Status `skipped` means not enabled or out of
  budget — unmeasured, never zero.
  - **Google News RSS is off by default.** Its feed states it is "for personal, non-commercial
    use. Any other use of the feed is expressly prohibited." It is built and tested; enabling it
    (`RESEARCH_ENABLED_SOURCES`) is a decision about this deployment's use, not a code change.
  - **GDELT `{}` is unmeasured, not zero.** It answered `{}` for "creatine" over a week while
    Google News listed 60 articles, in the middle of a run of 429s with no `Retry-After`. Its
    plain-text errors arrive with status 200. The retry wrapper waits at least 10 s after a
    refusal (`THROTTLE_WAIT`).
  - **RSS replays its stored items on a 304** (`research_feed_state.items`); without them
    "unchanged" would read as "empty". Feeds that refused a reader (NIH, Mayo Clinic: 403) are
    left out, not fetched around.
  - **The YouTube key goes in `X-Goog-Api-Key`**, never the URL: `logging_setup` masks
    `api_key=` and `token=` but not `key=`. Lifetime views are never momentum.
  - **Reddit was removed (2026-10-13)**: its Data API is approval-gated, and access did not
    work for this project. The `TrendSource` protocol is the seam if it is ever re-added.
  - **DuckDuckGo's "No results found." is an empty answer** (`web_search._no_results`): ddgs
    raises that exact message only when every engine answered with nothing, and treating it as
    a failure marked web search `failed` on narrow topics.
- **Trend history** lives in `research_topics` (identity by `subject|outcome`, plus an
  embedding match within a category for drifted wording) and `research_topic_observations`
  (one row per topic per run, written once in `service.finish` as an upsert, and only when the
  trend phase completed). **No incremented counters**: how often a topic was seen or proposed
  is a count over observations. `resolve_topics` upserts and commits *before* the next
  checkpoint, because candidates reference those ids. Status `preliminary` whenever nothing
  compared two periods; repeat suppression holds a topic proposed within 28 days unless its
  stories doubled or momentum ≥ 65.
- **Research categories are the article categories.** `research/categories.py::Category`
  (re-exported from `contracts.py`) lists exactly `Subject`'s values, checked at import, so the
  desk's filter and the feed drawer cannot drift. It is a separate class, not an alias, and has
  **no docstring**: `TriageOutput`'s schema is the triage model's grammar. The same goes for
  every enum in `trends/contracts.py`. `other` has no seeds; asked for alone it searches every
  seed and sweeps every feed.
- **Deep research reuses the article pipeline's extraction and `TemplateQueryStrategy`**, so an
  `UnanchoredQuery` topic is discarded before anyone can promote it.
- **pytrends is archived and its `retries=` option crashes under urllib3 2**
  (`method_whitelist`). It is created with `retries=0`, and `GoogleTrendsProvider` does its own
  pacing (4 s) and 429 backoff (15 s, 45 s). Only that file imports pytrends.
- **`python -m scripts.trend_search`** runs the trend half live, dry by default (in-memory feed
  state and history, no evidence check, no writes). Use it before touching scoring,
  clustering or a source; `--apply` queues a real run as the desk does.

**Social listening and product discovery (`app/social`, `app/products`, `app/intel`) are two
more proposers, beside the trend search and the MeSH scan, not inside them** (2026-11-04,
migration `0002_social_products.sql`). Nothing in `app/research` or `app/discovery` imports
them (`test_social_scope.py`), the trend search's output fields and `trend-v1` weights are
pinned by that test, and neither module ever enqueues an article. What to know:

- **YouTube Data API only; no scraping, by decision.** Instagram is reported `NOT_CONFIGURED`
  (needs a Business account and Meta app review for Public Content Access; 30 hashtags a
  week) and TikTok `UNAVAILABLE` (its Research API is academic-only) on every run, with the
  reason. A new platform is a client beside `social/youtube.py` plus an entry in
  `collect.py::platform_logs`; scraping vendors were considered and refused.
- **Collection runs are a queue the worker claims** (`collection_runs`, kinds
  `social_daily | social_weekly | product_discovery`, one active per kind by a partial unique
  index), after research runs and before article runs, with the research run's heartbeat and
  stale sweep. In the worker because the reader's model calls must never share the GPU with a
  pipeline run. `social/schedule.py` runs in the API lifespan and only *enqueues*: due-ness
  from the last completed run of each kind (23 h, ~7 days), and a failed kind is not retried
  for 2 h. `POST /console/collection/runs` and `POST /automation/collection/runs` (n8n token)
  queue the same rows.
- **Statuses: `SUCCESS | EMPTY | UNAVAILABLE | RATE_LIMITED | ERROR | NOT_CONFIGURED`**
  (`social/contracts.py::SourceLog`), per platform or source, on `collection_runs.collection_status`.
  Only SUCCESS and EMPTY count as a day of coverage. `detail` says why a source was never asked
  and is dropped once it was; a product run's web and openFDA read EMPTY, "not asked", when no
  product needed them, since NOT_CONFIGURED would suggest a missing key. A spent quota or rejected key stops the
  source for the run (`log.stopped`).
- **The daily run makes no model calls**: refresh counters of posts published within
  `social_track_days` (`videos.list`, 1 unit/50; a video the API no longer returns is deleted),
  search a rotating watch list (research seeds + product categories + recent social topics) at
  `social_youtube_max_searches_per_day` (100 units each), tag new posts with known topics by
  their words, prune. The rotation is stateless (the day picks the
  slice) and every search covers the whole `social_track_days` window, newest first: a look-back
  sized to the rotation left gaps whenever the watch list changed length and shifted the slices,
  while those days still counted as covered. **That quota is
  the trend search's too**: if social spends it, the trend search's YouTube source fails and its
  data coverage drops — a trend-score change caused from outside. Keep the sum well under 10,000.
- **Measured live 2026-11-04/05:** broad seeds fill the 50-result page, so their upload counts
  are floors; the run records `truncated_queries` and the result says so. On 2026-11-05 **all 15
  searches filled it** (738 new posts, most from the last 36 h): counts are a sample of each
  word's newest uploads, not totals. Daily run 31 s, ~1,515 quota units; weekly and product
  discovery ~27 min each, all of it the reader (10 batches at ~2.8 min).
  **The reader's budget sets the run's length, not the search count**, and it reads ~200 of
  ~5,000 posts a week. That is sampling by design: the word tagger still counts unread posts
  for known topics, so the reader's job is finding new topics and products. To shorten a run,
  lower `SOCIAL_MAX_READING_BATCHES`.
- **Every reading of a post's counters is a new `social_post_observations` row**; NULL is "the
  platform did not say" (hidden likes, comments switched off), never 0. **YouTube allows 30 days
  of storage**: posts not refreshed for `social_retention_days` (28; validator: 21–28, since the
  prune runs only with the daily collection and at 30 a post could reach almost 31) are deleted
  and cascade to observations, topic links and product mentions; `text_readings` go too.
  Topics, products and candidates' computed numbers stay. The prune runs **first** and commits alone, so a
  crash later in the run cannot skip it day after day and turn a bug into a storage-policy breach.
- **One model call reads texts for both modules** (`intel/reader.py`): the wellness topic and
  any named products, in batches of 20, at most `social_max_reading_batches` per run, cached
  in `text_readings` by sha256(`READER_VERSION` + text) so an unchanged text is never read
  twice, and **committed after each batch**, so a restart loses at most the batch in flight
  rather than up to ~27 minutes of model time. Posts are offered **newest first**: at default volume (hundreds of new posts a day,
  ~200 read a week) an unordered query spent the budget on three-week-old posts. Checked, never
  trusted: an id must be one sent; a topic's subject must share a word
  with the text; a product's quote must be copied from the text and contain the brand. A word
  filter (`worth_reading`) drops texts with no wellness or product words before the model.
  `ProductCategory` and `MentionKind` have no docstrings (grammar). Measured 2026-11-04 on
  Bonsai, 14 labelled texts: 14/14 on every metric, 130 s — a small set written by the
  implementer, so it shows the reader works, not how accurate it is.
- **Analytics are pure** (`social/analytics.py`, `social-v1`): three equal 7-day windows ending
  now; a window counts only with ≥5 days of SUCCESS/EMPTY daily collection, else every count
  in it is None. Popularity (median views a day) and growth (uploads and *distinct creators*,
  `(c+5)/(p+5)`) are separate fields; velocity needs two readings ≥12 h apart; engagement
  leaves out posts with a hidden count and reports how many it rests on. Momentum
  (`rising | steady | declining | insufficient_history`) is an assessment stored apart from the
  metrics, and there is **no composite social score**. The first two weeks of a deployment
  are `insufficient_history` by design; nothing is backfilled.
- **The weekly run** reads new posts, links topics (`social_topics`, keyed by the trend search's
  own `topic_key`, kept separate from `research_topics` so social activity cannot touch the
  trend search's history), records product mentions, **re-tags every post in the window by the
  word rule** (`tag_known_topics`), measures, and proposes topics with
  ≥`social_min_posts` this week. The re-tag is what makes weeks comparable: daily tagging only
  knows topics that already exist, so without it a topic found this week counted every matching
  post from then on but only the model's sampled reads before, and read `rising` in its second
  week (`test_a_topic_found_this_week_counts_last_week_by_the_same_rule`). **A category we search for is never proposed** (`weekly.py::too_broad`, listed in the run's
  notes): measured 2026-11-05, 6 of the top 10 were our own search words ("Protein", "Stress",
  "Sleep for sleep"), because the tagger hands such a topic every post that search found, so the
  ranking measured our search budget. A subject inside a seed or `CATEGORY_INFO` phrase, or all
  `GENERIC_WORDS`, needs an outcome that adds a word beyond the subject and `_EMPTY_OUTCOME_WORDS`.
  That list stays tiny: `GENERIC_WORDS` has "muscle" and "heart", and "protein for muscle" is a
  real angle. It is word-exact, so "Sleep tracking for sleep" still passes. **Suppression is novelty only**: promoted never returns
  unless its draft failed or was cancelled (`weekly.py::_draft_failed`: no article exists to duplicate, and this is
  the only way such a topic is tried again, as for the MeSH scan; queued and running still hold it),
  dismissed not within `social_dismiss_cooloff_days` unless independent creators doubled, and back after the cooloff either way; held
  topics are listed in the run's notes. The top `social_evidence_top` get PubMed counts and the
  research agent's `evidence_status` as `social_candidates.evidence`, beside — never inside —
  the metrics. Promote creates a pipeline run with origin `social`.
- **A proposed topic is shown and generated as a question** (`social/questions.py`,
  `social_topics.question`, 2026-11-05): one call per weekly run over each new topic's post
  titles (57 s for 10), written once per topic, kept only if it is a short yes/no question naming
  the subject (`usable_question`); otherwise NULL and the label stands in. A failure costs the
  question, never the run. **Promote sends the question to the pipeline, which is why this is
  more than display**: measured once on Bonsai, the label "Smart ring for sleep" came back from
  extraction as `product='Smart ring for sleep'`, a quoted phrase PubMed will not match, while
  "Can smart rings detect sleep apnea?" gave `product='smart rings'`. First full article from a
  question, 2026-11-05 ("Do smart rings accurately detect sleep apnea?", PubMed down): 21.6 min,
  validation clean, 11 of 12 sources cited, a `mixed` verdict that answers the question. But
  APPRAISE labelled 11 of those 12 `off_topic`, and the draft gave some a section of their own
  ("What the other sources measured": VO2max, pregnancy): the coverage re-prompt asks for every
  source, and a narrow question leaves more of them off the point. The reviewer trims those.
- **The literature counts ask what the question's article would search**
  (`weekly.py::assess_evidence`, as the research agent's `_deep`): extraction on the question,
  `TemplateQueryStrategy`, counted per claim, the best claim sizing the topic; `evidence.query` is
  the search counted. A question with no searchable subject says so before anyone promotes it.
  **Europe PMC answers when PubMed is down** (`source: europe_pmc`, measured 2026-11-05 during an
  NCBI `esearch` outage), in titles and abstracts only (`profile_terms`), since unscoped it
  searches full text and over-counts ~40x. Its review tag is coarser, so `reviews_or_meta` and the
  status can read higher than PubMed's. 0 papers is real information: "Does a home gym replace a
  commercial gym?" counted 0 and would likely draft `no_evidence`.
- **A search that found 0 papers goes to the bottom of the weekly list, with a bold line on the
  desk** (`weekly.py::found_no_papers`, the reviewer's request, 2026-11-05). It is the only way a
  count touches the order, and only a search that ran: `not_checked` is unknown, not zero, and
  keeps its place. Only the top `social_evidence_top` are counted, so an unchecked topic can sit
  above a counted zero. **Topics asking one question are proposed once**
  (`without_repeated_questions`, `questions.py::same_question`, Jaccard at the trend search's 0.8
  over the words without the wording); the better-ranked stays and the run's notes name the other.
  **That covers decided topics too** (`weekly.py::_decided_topics`, 2026-11-06): every promoted one
  and those held back this run. Suppression works per topic, so holding back "Sleep tracking for
  sleep" would otherwise have freed its twin, "Smart ring for sleep", to ask the same question.
  Questions are written for twice `social_max_candidates`, and the list is cut after the repeats
  go, so the next-best topic takes a repeat's slot (a run with 33 eligible had proposed 8 of 10).
- **The other two proposers show questions on the desk, and still generate from their labels**,
  because the label is what their evaluations measured. The MeSH scan's is a template at read
  time (`discovery/topics.py::topic_question`, "affect" because the scan does not know a direction).
  The trend search already wrote one: triage's `signals.reader_question`, now the row's headline;
  nothing in `app/research` changed. It is open-ended ("What are the health benefits of Pilates?"),
  not the yes/no form social topics get.
- **Products** (`products/`): identity is normalised `brand|name` (case, ™/®, brand repeated,
  "Gen 4"/"Gen4"/"4th generation" → "4"); **a brand alone is no product** (`product_key` → None), which
  also drops single-name products like "Pulsetto" — precision over recall, revisit if it bites.
  Status climbs and never falls: `uncertain` → `corroborated` (2+ independent publishers or
  creators) → `confirmed` (a web result on the brand's own domain, `is_official_url`, strict on
  purpose, whose title or snippet names the product, since a rumoured model also finds the brand's homepage;
  or an openFDA 510(k) whose applicant is the brand, asked only for `FDA_CATEGORIES`).
  **Product discovery offers the reader news before feed items** (`discover.py`): the feed backlog alone
  filled the budget, so in dev no news text had ever been read. Two dev runs now; on 2026-11-07, 12 of the run's 16 mentions came from news. **An official page must also hold, in its URL path, every word of the name that says which product it is** (`resolve.is_product_page`, `_KIND_WORDS`, 2026-11-07): that run confirmed "Samsung Galaxy watches", "Samsung Galaxy wearables" and "Meta smart glasses" on category pages whose titles named them, 3 of its 5 new confirmations. The cost: a name of only brand and kind ("Aina Ring") or an opaque path (`/p/123`) stays unconfirmed. Replayed on the 8 confirmed rows, not yet on a live run. Those three were reset by hand in dev, since status never falls; Aina Ring keeps its status.
  **The reader's category stands only if the source's own text uses one of the category's
  `CATEGORY_NAMES`** (`resolve.checked_category`, applied in `record_mentions` because readings
  are cached), else `other`: Bonsai filed watches and earbuds as smart rings, milk as a massage
  gun and a home gym as a glucose monitor, which would ask openFDA. It reads the whole text, since
  a quote is often just the name ("Dexcom Stelo"); a news item's text is only its headline, so a
  real device can fall to `other` and miss the FDA lookup. A category is set once, at insert.
  **A mention is not a launch**: one source saying it launched is `claimed`, two `corroborated`,
  only a *first* FDA clearance within a year `confirmed`, since updates get clearances of their
  own (Dexcom G7: 2022, and again 2026-06-08; `lookup_510k` sorts oldest first); "announced" or
  "coming soon" never counts (`LAUNCH_KINDS`). **Lookups go to the unconfirmed products checked
  longest ago, whichever run found them** (`discover.py::_confirm`,
  `products.confirmation_checked_at`, 2026-11-06). They used to go only to products that run
  touched, so anything named only on YouTube, 30 of 34 in dev, was never looked up, and with a
  cap of 10 and nothing recording who was tried, the same ones came back. A lookup that could not
  run keeps the product's turn. Product discovery reads the feed items the trend search
  already cached (`research_feed_state.items`, read only) plus one news search per category.
  The openFDA query and field names were checked against a live response on 2026-11-06.
- **Relations are computed at read time, never stored** (`intel/related.py`, which the scope
  test keeps free of writes): same `topic_key` or subject key across `research_topics` and
  `social_topics`, a MeSH substance whose normalised name is the subject, products named in a
  topic's posts, and product categories whose `CATEGORY_INFO` words appear in a topic. Each
  related item carries *its own system's* numbers (trend score, MeSH paper count, product
  status), untouched. No link table, so nothing can go stale or outlive what it links.
  Neither `/console/products` nor `/console/social/result` carries relations: per row they cost
  ~24 queries, and the desk loads the result on every visit, so the panel asks `/intel/related`
  for one item when the reviewer presses Related.
- **`GET /console/social/result` is snake_case** (`SocialListeningResult`), like the trend
  search's result; the desk's "Social & products" panel (`SocialPanel.tsx`) renders momentum,
  popularity, literature counts and limitations as separate lines. **By default it serves the
  latest *completed* weekly run** (`latest_weekly_run`). A failed one proposed nothing, and
  serving it told the reviewer "nothing proposed this week" while hiding last week's undecided
  topics. The panel says so instead, read from the runs list.

## Invariants — do not route around these

The four are stated with their enforcement mechanisms in [DESIGN.md](./DESIGN.md) §1. What
matters when editing:

1. `ReviewService.approve()` is the **only** assignment of `published` in the codebase.
   `tests/unitTest/test_publish_path.py` greps the AST and fails when a second one appears. If a change
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

## Config traps

- **`.env` is read from two places, and compose must name both.** `config.py`'s
  `env_file` is `(".env", "../.env")` *relative to the process's cwd*, so a host venv
  running from `backend/` picks up `backend/.env` **and the repo-root `.env`** — and this
  project's keys live at the root. Compose named only `./backend/.env`, which does not
  exist, so every container booted with none of them. Nothing errored, because every
  setting has a working default: imagery went `not_configured`, `EMBEDDING_PROVIDER` fell
  back to `ollama`, and `ENABLED_PROVIDERS` quietly dropped OpenAlex. Both paths are listed
  now, both optional. **When a value looks wrong in a container, compare it against the
  file before debugging the code** — and compare it by *hash*, never by printing it.
- **Editing `.env` does nothing until the containers are recreated.** Docker resolves
  `env_file` at container **creation**, so `docker compose restart` re-runs the process with
  the old environment still baked in. After rotating a key this presents as a `401
  Unauthorized` from a provider against a key you can see is correct in the file — the
  container is holding the revoked one. `docker compose up -d --force-recreate backend
  worker`. The same applies to any `.env` edit, not just secrets.
- **The Anthropic key is identity-linked and needs a workspace id.** Auth itself succeeds
  (a `count_tokens` probe returns 400, not 401), but the API answers
  `anthropic-workspace-id is required when authenticating with an identity-linked API key`.
  `anthropic_client.py:294` constructs `AsyncAnthropic(api_key=…)` with no workspace header,
  so **`LLM_PROVIDER=anthropic` will fail on the first call** until that is passed. Invisible
  today only because the provider defaults to `ollama`.
- **Model calls run locally by default.** `LLM_PROVIDER=ollama` and
  `EMBEDDING_PROVIDER=ollama` (see [.env.example](backend/.env.example)) — free, no key,
  needs `ollama serve` plus `ollama pull llama3.1:8b mxbai-embed-large`. The hosted clients
  are fully wired and one setting away: `LLM_PROVIDER=anthropic`, `EMBEDDING_PROVIDER=voyage`.
  Providers are selected in [llm/factory.py](backend/app/llm/factory.py) and
  [llm/embeddings/factory.py](backend/app/llm/embeddings/factory.py); nothing else knows
  which one is live.
- **`LLM_PROVIDER=llamacpp` runs every generative call on one `llama-server`** (Ternary-
  Bonsai-2-27B, PTQ1_0, PrismML's llama.cpp fork; `llm/llamacpp_client.py`). Embeddings stay
  on Ollama. Measured 2026-10-14/15 on the 16 GB M2 Pro: better articles than qwen2.5:7b
  (groundedness 0.92 vs 0.74 over 18 cases) and better appraisal than llama3.1:8b on
  identical inputs (77.0% vs 67.8% exact over 121 hand labels, 0 flips vs 5), at ~10–40
  minutes per run. Four traps:
  - **It cannot share the GPU with an Ollama *language* model, in either direction.** Both
    resident: Ollama answers with empty bodies, and llama-server fails with `Compute error`
    and then fails *every* later request until restarted, while `/health` still says ok.
    The embedding model fits beside it. That is why it is all three calls, not just
    synthesis, why the client unloads Ollama's language models before each call, why
    research triage runs on llama-server too (`LlamaCppTriageClient`), and why the eval
    turns its judge (an Ollama model) off unless `--rescore`. Triage was skipped on this
    provider until 2026-10-23, and the raw queries became topics: that run selected
    "care" and "cognitive" and ranked "benefit" first. With Bonsai triage the next run
    selected six real topics, all labelled wellness, in 16 minutes (3 batches, ~3.5 min
    each). `python -m evaluation.trend_check` checks a run's links and topics.
  - **A dead server looks healthy.** Check it with a real completion, as the eval's
    preflight does, never `/health`. It runs under launchd with a watchdog
    ([llama-server/](llama-server/README.md), `./manage.sh install`) that probes with a
    completion every 2 minutes and restarts it after two failures — the worker's heartbeat
    cannot see this failure, because the worker is fine. Do not start it by hand while the
    LaunchAgent is loaded (port clash); `./manage.sh stop` before anything that needs an
    Ollama language model, such as the eval judge.
  - **It overruns the 8-sentence section cap on long source lists** (one sentence per source)
    and quotes numbers the cited source does not contain (`unsourced_number`); both are
    caught, both cost repair calls. It also occasionally loops to the 8,000-token cap
    (PrismML known issue), reported as a truncation.
  - **`LLAMACPP_REASONING_EFFORT=none`** is what was measured. Bonsai's template rejects
    `high` with a 500, `low` thinks as long as `xhigh`, and a budget of 0 does not turn
    thinking off.
- **Ollama's `format` is a decoding grammar, not a validator.** It guarantees JSON of the
  right shape and nothing else — the Pydantic validators in `domain/contracts.py` (sentence
  ceilings, 12-word headline) are still checked in Python, and small local models break them
  often, so `ollama_client.py` retries once with the errors fed back, telling the model to change
  only the fields named: an unscoped "shorten" invites a small model to cut the whole article to
  fix one over-long section (reasoned, not measured). Also: `num_ctx` is set
  explicitly on both calls because the server default is 4K and overflow silently drops the
  front of the prompt — for synthesis that means sources vanish while the instruction to cite
  them survives.
- **`EMBEDDING_DIM` is load-bearing, and matching widths are a trap, not a safety net.**
  The migration templates the vector column width from it, so changing the *dimension* needs a
  migration. Changing only the *provider* does not — and that is the dangerous case:
  `voyage-4` and `ollama/mxbai-embed-large` are both 1024-d, so the column accepts either and
  pgvector will compute a cosine distance between them without complaint. The number is noise.
  A provider switch therefore degrades retrieval **silently**, with `sources.embedding_model`
  as the only signal. `SourceCache` repairs rows it touches (`had_embedding` compares the
  recorded model to `embedder.model_id`), so a re-retrieved paper fixes itself; everything
  else stays stale. Run `python -m scripts.reembed_sources` (dry by default, `--apply` to
  write) after any provider or model change. This is not hypothetical — the cache ran with
  288 `voyage-4` rows against 65 `ollama` ones until 2026-09-04.
- **`claude-opus-5` rejects `temperature` / `top_p` / `top_k`.** Steer behaviour by prompt only.
  `max_tokens` caps thinking **plus** output, which is why synthesis is sized at 16K for a
  ~700–950-word article; `_check_truncation` names that cause explicitly, because a truncated
  structured response has no parsed output and otherwise reads as a schema failure.
- **`retrieval_top_k` and `SYNTHESIS_NUM_CTX` move together.** Top-k is 12 ranked sources *per
  claim*, deduplicated across claims into the synthesis prompt, and it is the one knob deciding
  how many sources an article can cite. Raise it and Ollama's context window (32K, sized for
  it) has to follow: overflow there is **silent** — the front of the prompt is dropped, so
  sources vanish while the instruction to cite them survives, and the run fails as
  `hallucinated_handle` with nothing pointing at the real cause. A 32K window on an 8B model is
  also several GB of KV cache; if local runs crawl, drop to top-k 10 and 24K rather than
  leaving them mismatched. **`full_text_max_papers` draws on the same window**: six papers at
  two ~300-word excerpts is about 5,000 tokens. Measured 2026-09-29, the largest synthesis
  prompts ran ~15K input tokens, so with 8K reserved for output six still fits; raising it, or
  top-k, eats the margin. **Since 2026-10-25 SYNTHESIZE refuses to overflow it**
  (`synthesize.py::_fit_context`, `metrics.synthesize.context_fit`): past
  `SYNTHESIS_CONTEXT_TOKENS` less `SYNTHESIS_RESERVED_TOKENS` (estimated at
  `CHARS_PER_TOKEN` 4.2; Bonsai measured 4.55-4.84) it drops excerpts, then sources, worst-ranked
  first, never the last source, and logs what went. Measured on broad questions before the
  4-claim cut, prompts reached ~24K tokens. `SYNTHESIS_CONTEXT_TOKENS` is a constant that must
  match llama-server's `-c` (32768).
- **`thinking` is passed explicitly on both Anthropic calls, and must stay that way.** Whether
  an omitted `thinking` means "think" varies across the range (Sonnet 5 / Opus 5 do, Opus 4.8 /
  4.7 do not), and the model is config — leaving it implicit lets `EXTRACTION_MODEL` silently
  decide whether the token budget is spent on thinking. It also means **extraction cannot run on
  `claude-haiku-4-5`**: it is pre-adaptive-thinking, so the argument 400s. Both models therefore
  default to `claude-sonnet-5` — the other reason being that Haiku left `ingredients` empty in 9
  of 10 samples, which silently unanchors the PubMed query.
- **The `cache_control` breakpoint is a no-op on extraction.** The minimum cacheable prefix is
  per-model and not monotonic (512 on Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5), and a prompt
  under it fails to cache silently — `cache_creation_input_tokens: 0`, no error. Measured with
  `messages.count_tokens`: extraction is **306 tokens** and caches on nothing; synthesis was
  **1285**, caching on Sonnet 5 with only ~260 tokens of headroom. The section and specificity
  rules roughly doubled the synthesis prompt (~1,390 words as of 2026-09-04, not re-counted
  against `count_tokens`), so that headroom is now comfortable rather than marginal — growing
  this prompt is safe, and it is *trimming* it that would break caching without saying so.
  Confirm against `usage.cache_read_input_tokens` on a real call rather than assuming.
- **Do not compress the synthesis prompt. It was tried, measured, and reverted.** A rewrite to
  1,302 words from 1,503 kept all 70 rules present — verified by probe, nothing dropped — and
  still cost output quality on `llama3.1:8b`: inline citations fell from 5.0 to 3.3 handles per
  article and sections from 3.0 to 2.0, with 43% of runs emitting no sections at all against
  17% before (n=14 compressed vs n=6, same 12 sources). The redundancy and the stated rationale
  are load-bearing on a small model, not padding. And the trade is bad even before quality:
  the system prompt is only ~13% of a synthesis call (the 12 abstracts are the rest), so a 13%
  trim of it saves **2% of the call**. If this needs revisiting, the lever is `retrieval_top_k`
  or a larger model, never these words.
- **Pointing a model setting at something new needs a price-table entry.** Nothing breaks
  without one — the run just reports `estimatedCostUsd: null` forever, which is honest and
  easy to miss. `test_cost_accounting.py` asserts the clients' four default models are all in
  `PRICES`, so the defaults are covered; a value set only in `.env` is not.
- **Credentials in logged URLs are masked** by `app/logging_setup.py`, which both the API and
  the worker configure logging through. httpx logs every request URL at `INFO` and NCBI takes
  its key as `api_key=` in the query string, so every PubMed call used to write the key in plain
  text. The filter sits on the root *handlers*: a logger-level filter would not see records
  propagated from `httpx`. A new entry point that calls `logging.basicConfig` itself loses it.
- **Both LLM clients log full prompts at `INFO`.** Useful while the prompts are unexercised,
  but it puts every system prompt and source block in the log stream — reconsider the level
  before anything ships where logs are retained.
- **`WORKER_STALE_AFTER_SECONDS` must be ≥ 3× `WORKER_HEARTBEAT_SECONDS`**, enforced by a
  `model_validator` in [config.py](backend/app/config.py) that refuses to start otherwise. Set
  closer, one slow `UPDATE` declares a live run abandoned and the sweep starts a second
  concurrent execution of it. Per-provider request *rates* are not settings — they are
  constants in `retrieval/factory.py` (scholarly APIs) and `research/factory.py` (News API and
  the trend sources: GDELT one per 6 s, Google News, RSS, Brave, YouTube), being facts
  about each API rather than knobs; only the retry counts (`PROVIDER_MAX_RETRIES`,
  `PROVIDER_MAX_RETRY_WAIT_SECONDS`) and the trend sources' per-run *budgets* are
  configurable.
- Password hashing is Argon2id via `argon2-cffi` — chosen over bcrypt to avoid 72-byte
  truncation. **The login throttle must stay ahead of the hash.** Argon2 costs 31ms per
  attempt, so `/auth/login` saturates a core at ~32 req/s of junk, and `equalise_timing()`
  makes nonsense input cost the same as a real try. `security/login_throttle.py` is checked
  before the user lookup for that reason; moving it after would still pass every behavioural
  test and leave the process exposed. **There are four separate instances** of it —
  reviewer login, reader login, the contact form, and reader signup (`api/deps.py`; signup
  hashes a password on every call, so it counts every attempt, and hashes in a thread
  rather than on the event loop). One shared counter
  would make the endpoints each other's denial of service: readers and reviewers arrive from
  the same office NAT, and enough failed reader logins would lock the console out of its own
  IP budget. Failures are counted for unknown emails too, or the 429
  becomes the account-enumeration oracle the equal timing exists to prevent. Unlike
  `retrieval/throttle.py` it rejects instead of sleeping — a delay only slows a client that
  chooses to wait, which is the honest user and not the attacker.
- **Image bytes live in Postgres, in `media_objects` — there is no `MEDIA_ROOT`.** They were
  on local disk until that turned out to be the one piece of published state a database backup
  did not cover: images generated by a host-venv run were served as 404s the moment the stack
  moved into containers, because the compose volume was its own storage and had never seen
  `backend/var/media`. Every path in `articles.generated_imagery` pointed at a file the API
  could not reach. One store, one backup, one restore. That directory and the one-time
  `import_media` script that emptied it are both gone — nothing reads or writes local disk.
  - **The URL did not change.** `/api/media/<2 hex>/<62 hex>.<ext>` is still what a document
    holds and what `MEDIA_SRC_RE` matches, so no stored JSON was rewritten. The two-character
    shard is now decorative — there is no directory — and stays because rewriting stored
    documents to drop a slash would be a migration with nothing to gain.
  - **A column on `articles` cannot work**, and the `media_objects` section of
    `migrations/0001_initial.sql` records why: ILLUSTRATE stores pictures at stage 5 and the article row is not created until
    PERSIST at stage 7, uploads are deliberately not article-scoped, and content addressing
    dedups across articles. Keyed by digest, related to nothing.
  - `store_image` is now `async` and takes a **session, not a path**, and **does not commit** —
    so ILLUSTRATE's pictures land with the orchestrator's stage commit and roll back with a
    failed stage, instead of surviving on disk as orphans. `prepare_image` is the pure half
    (sniff, digest, path) and is what `test_media.py` exercises without a database; the INSERT
    is covered in `test_db_invariants.py`.
  - `services/media.py` still decides what a file is from its first bytes (never the filename
    or Content-Type) and **SVG is still refused** — it is a script host, and this is served
    from the app's own origin. A document may only reference media the store wrote; checked on
    autosave *and* again in `ReviewService.approve()`. Do not relax either check to make a
    paste work. `media.py::image_node` builds the node the pipeline writes and
    `tests/unitTest/test_media.py` pins that `assert_media_is_ours` accepts it, because a writer and a
    checker that disagree produce an article whose *first* autosave is refused.
  - `store_image` still has **no size gate**: the upload route enforces `MEDIA_MAX_BYTES`, and
    a generated image never passes through it, so `services/illustration.py` is the only
    ceiling on that path.
  - Serving is `api/public/media.py`, not `StaticFiles`. It answers **404 for every miss** —
    malformed path, unknown digest, wrong extension — because a 422 on one and a 404 on
    another tells a prober which half was wrong. It also fixed a live bug in passing: on a
    stock Python install `.webp` is absent from the `mimetypes` table, so every generated
    illustration was served as `application/octet-stream`, and only browser sniffing in
    `<img>` kept it rendering.
- **Four things prune themselves, one does not.** Measured 2026-09-30, the schema was lean —
  almost every column is read — but three tables grew without bound, and the trend search
  (2026-10-12) added a fourth. Now:
  - A succeeded MeSH scan deletes ledger rows older than
    `DISCOVERY_OBSERVATION_RETENTION_DAYS` (120). A validator keeps that at or above what the
    baseline windows and the angle lookback read. Shortening it past them would quietly shrink
    every baseline and turn the next window into a false surge.
  - A finished research run deletes candidates nobody decided on from runs older than
    `RESEARCH_RETENTION_DAYS` (60). Promoted and dismissed ones stay, because novelty reads
    them. A discarded candidate is stored without its example results.
  - A finished research run deletes trend observations older than
    `RESEARCH_HISTORY_RETENTION_DAYS` (180) and topics nothing refers to any more. A validator
    keeps it above the repeat cooloff, the novelty lookback and the trend-status lookback:
    pruning inside them would make a topic look new again. `research_feed_state` holds one row
    per feed and needs no pruning.
  - Orphaned images do **not** prune themselves. Every Regenerate orphans the old frames, so
    run `scripts.prune_media`. It deletes only what no article column mentions, after a grace
    period.
- **`RESEARCH_TRIGGER_TOKEN` must be the same for the backend and n8n**, and it lives in the
  repo-root `.env`. Unset, `/api/automation/*` answers 404 throughout, so n8n cannot start a
  run and nothing is exposed. It must be 32+ characters (validator). The n8n container gets
  that variable, not the whole `.env`.
- **OpenAlex needs `OPENALEX_API_KEY` (free) for real volume.** Keyless requests share a small
  daily budget per IP; measured 2026-10-01 it ran out after ~100 searches, and every later call
  was a 429 with `Retry-After` of hours, until midnight UTC — runs carried on with two providers
  and less recall, and nothing flagged it. `OPENALEX_MAILTO` no longer buys the polite pool on
  its own. `ThrottledClient` now treats a `Retry-After` of an hour or more as a spent quota:
  it fails the call at once and sends that provider nothing more for the rest of the run.
- **The trend sources' keys are optional, and each is skipped without one**, with the reason
  in the run's provider status: `BRAVE_SEARCH_API_KEY` (web search through Brave's documented
  API; ddgs otherwise), `YOUTUBE_API_KEY` (~1,010 of the default 10,000 daily quota units a
  run, `YOUTUBE_MAX_SEARCHES_PER_RUN`). Keys travel in
  headers, never in a logged URL. `RESEARCH_ENABLED_SOURCES` lists what a run may use;
  `google_news` is deliberately not in the default (see the research section).
- **`NEWS_API_KEY` is optional, and its free plan is for development only**: 100 requests a
  day, delayed 24 h. A run uses up to 50. When it is unset or out of quota, DuckDuckGo news
  answers instead, and the candidate's `signals.news.source` says which one.
- **Testing phase (since 2026-10-01): image generation and News API are switched off in the
  root `.env`, keys kept.** `IMAGE_PROVIDER=none` makes drafts text-only (illustrate cause
  `disabled`; the console's Regenerate answers 503). `NEWS_API_ENABLED=false` makes research
  use DuckDuckGo news. To turn either back on, flip the line and `--force-recreate` (below).
- **Every "Let us know" message is also emailed to `CONTACT_NOTIFY_TO`** (`services/notify.py`,
  Gmail SMTP by default; `SMTP_USER` + an app password in `SMTP_PASSWORD`). An empty password
  sends nothing, and the address is a placeholder (`editorial@example.com`) until the inbox
  exists. It is a `BackgroundTasks` job after the commit, so it never fails or delays a
  submission and never describes an unsaved row. **A mail outage is only a log line, and
  the desk has no inbox screen**: `frontend/src` never calls `/api/console/contact`, so
  while email is broken (a revoked app password, a wrong `SMTP_HOST`) new messages sit
  unseen in `contact_requests`, readable only through that endpoint in `/docs`. The
  sender's text stays in the plain-text body, and line breaks are collapsed out of the
  subject, so a name cannot add a `Bcc:` header. `Reply-To` is the sender.
- **`IMAGE_GEN_KEY` must be a fine-grained HF token with "Make calls to Inference Providers".**
  A plain read token authenticates and then 403s on the first render, which reads nothing like
  a permissions problem until you see the message. No key at all is a supported state, not a
  broken one — `imagery/factory.py` returns `None`, the API still boots, runs still succeed,
  and drafts come out text-only. Two dependencies come with this (`huggingface-hub`, `pillow`);
  both were already present *transitively* via `voyageai`, so an undeclared version works in
  every local venv and fails only in `docker compose up backend`, which installs from
  `pyproject.toml`. Same trap as `python-multipart`.

## Deliberately out of scope

Full text for paywalled papers, caching full text, the agentic query-refinement loop, and the AWS
deployment are deferred (DESIGN.md §11). Open-access full text is fetched per run for up to six
papers (see Retrieval above). `LLMQueryStrategy` is a declared seam that raises —
`TemplateQueryStrategy` is what runs. A `source_passages` table for cached full-text passages
sits commented in `0001_initial.sql` so the FK direction is already settled.

**What has and has not met a real response** (checked 2026-08-21; keep this honest, it is what
tells you which code to trust):

- **Exercised.** PubMed and OpenAlex parsing, and the full pipeline as it then was (six stages,
  before `illustrate`) — two runs on 2026-08-20 produced 92 cached sources and two articles.
- **Exercised, contrary to what this file said until 2026-08-21: Voyage.** 288 rows carried
  `sources.embedding_model = 'voyage-4'` with populated 1024-d vectors. That string is written
  from `VoyageEmbeddingProvider.model_id` and from nowhere else; the Ollama provider namespaces
  its own as `ollama/…`. So a real Voyage call was made, with a real key, and the note claiming
  otherwise was wrong. **Superseded 2026-09-04: embeddings are now Ollama and the cache is one
  space** — all 353 rows are `ollama/mxbai-embed-large`, re-embedded by
  `scripts/reembed_sources.py`. Voyage stays fully wired and one setting away
  (`EMBEDDING_PROVIDER=voyage`), but switching back is a re-embed, not a config edit; see the
  `EMBEDDING_DIM` trap above.
- **Exercised 2026-09-04, closing the note below: FLUX.1-schnell.** The 403 was a token
  permission, as predicted. A fine-grained token with "Make calls to Inference Providers"
  renders: verified from inside the backend container against the live endpoint, decoded and
  encoded to WebP. So `_encode_webp` and the dimension read-back have now seen a real response.
- **Unresolved: which generative provider those runs used.** Both defaults said `ollama` by
  2026-08-20, but the embedding default said `ollama` too and was plainly overridden, so the
  defaults prove nothing about the environment. Each run finished in ~56s including retrieval
  and 73 embeddings, which is fast for local synthesis of a 10k-token prompt but is not
  evidence. Nothing in the database records it — which is precisely the gap the token ledger
  closes going forward, since `pipeline_stage_runs.model` now names the model per call. The
  two historical runs are backfilled with tokens and a **NULL model**, so they report
  `estimatedCostUsd: null`: inventing an id would have priced them against a model they may
  never have run on.
- **Exercised 2026-09-29: Europe PMC full text.** The open-access lookup and the JATS parser
  ran live against real papers (an ashwagandha RCT parsed to Introduction / Methods / Results /
  Discussion), and `FullTextStage` ran end to end on cached sources with the live Ollama
  embedder. Not yet inside a full pipeline run.
- **Exercised 2026-10-12/13: the Trend Search Agent and the split run, live.** A default run
  through the worker (all categories; RSS, GDELT, Google Trends, news, web) took **6.4 minutes**:
  448 items (384 from 21 feeds), 38 exact-URL and 8 title duplicates folded, 31 phrases, 12
  candidates enriched, 9 eligible, 7 evidence-checked (PubMed and Europe PMC answered every
  count), **5 selected**, one discarded for no literature. Confidence read *low* on all five,
  correctly: Google Trends was refusing this IP after a day of test runs, and GDELT throttled,
  so ~64% of each score was measured. Google News RSS answered every request (off by terms).
  **YouTube and Brave, 2026-10-13**: a trend run on RSS, news, Brave and YouTube made 8
  searches each (YouTube ~808 quota units; 176 videos; Brave 123 results), raised data coverage
  to 85% and gave every ranked topic four kinds of source; neither key appeared in the logs or
  the result. YouTube's `relevanceLanguage` is a hint, not a filter: Hindi and German videos
  came back. n8n 2.41.4 imported both new
  workflows in a scratch instance, and the manual webhook passed 202, 409 and 422 through.
- **Exercised 2026-09-30: the research agent end to end.** Google Trends (pytrends), DuckDuckGo
  web and news, PubMed and Europe PMC counts, qwen triage and deep research ran live in the
  worker: 22 seeds returned 254 rising queries in ~4 minutes and the run took ~8 minutes. n8n
  2.41.4 imported both workflows, published them, and its manual webhook reached the host API
  through `host.docker.internal`. News API has not been called: no key is configured.
- **Measured and not built 2026-09-30: local FLUX (mflux, 4-bit schnell).** On the 16 GB M2 Pro
  it took 198 s and 18.4 GB of peak GPU memory for one 1216×832 image, with swap growing from
  6.2 to 16.8 GB. Hosted FLUX with crop mode (one render per article) was chosen instead.
- **Not exercised.** Anthropic: the client and its token accounting are written against
  documented response shapes only. Semantic Scholar is wired as a retrieval provider but has
  not been enabled in a run.
- **Exercised 2026-10-01: Europe PMC as a search provider** (`ENABLED_PROVIDERS` is
  `pubmed, openalex, europe_pmc` in the root `.env`). On "Ashwagandha for stress" it returned
  50 hits, most of them papers PubMed had already found (59 duplicates merged across the three
  providers), and added 3 new ones. Its queries are the pipeline's PubMed terms with the field
  tags stripped and scoped to `TITLE_ABS` (`providers.py::europe_pmc_query`). Unscoped, Europe
  PMC searches open-access *full text*: creatine plus sleep matched 13,693 papers, led by
  brain-imaging studies where creatine is a measured metabolite, against 336 in title and
  abstract. It no longer falls back to full text when the scoped search is empty (it did for
  "zone 2 training", 0 against 20, until 2026-11-08; that topic now finds 13 scoped, and the
  fallback filled ranked lists with passing mentions). Its study-type tags are coarser than PubMed's, so a paper only it
  finds can grade lower. A paper PubMed also returns cannot be lowered by it:
  `dedup.merge_group` keeps the strongest study type any provider gave.
- **Resolved 2026-09-04: FLUX.1-schnell.** It was blocked on a token permission, not code —
  `403 Forbidden: This authentication method does not have sufficient permissions to call
  Inference Providers` against a correctly routed call (HF's router reached `nscale`). A
  fine-grained token with "Make calls to Inference Providers" renders. Recorded here because
  the 403's *sibling* is the one to recognise next time: a **401 `Invalid username or
  password`** from the same endpoint is not a permission problem at all — it is a revoked key
  still baked into a running container, because Docker resolves `env_file` at container
  creation and `restart` does not re-read it. See the config trap above.

That data corrected the classifier twice (DESIGN.md §4) and left one thing open.

**Abstract heuristics must never run on a paper whose tags already say something.** A trial
protocol and a narrative review both describe randomised trials at length — that is what they
*are* — so falling through to prose scored three protocols as `rct` and one plainly-tagged
`Review` as `systematic_review`, and the latter set a real article's evidence grade.
`NEGATIVE_PUBLICATION_TYPES` now ends classification at `unknown` for those tags, checked
*after* the map so a positive identification still wins. Protocols are refused in
`RetrieveStage` before the cache sees them. Watch both directions when editing this: mapping
`Review` straight to `NARRATIVE_REVIEW` was tried and reverted, because it demoted genuine
systematic reviews PubMed had tagged only with the supertype. `Review` is a floor the text may
raise **within the review family only**.

Classification is Python, so no migration can backfill it — run
`python -m scripts.reclassify_sources` (dry by default, `--apply` to write) after touching
`PUBLICATION_TYPE_MAP`, `NEGATIVE_PUBLICATION_TYPES`, `RETRACTION_PUBLICATION_TYPES` or
`TEXT_PATTERNS`. Skipping it means the fix applies only to papers nobody has fetched yet.

**Closed 2026-08-21:** a `supported` verdict could rest on a single cited systematic review,
because `check_verdict_within_grade` read the strongest cited study and never counted them.
`max_verdict_for_claims` now requires `QUORUM_FOR_SUPPORTED` sources per claim — see invariant
#3 above. The queued article that was in exactly that state still passes: it cites an RCT *and*
a systematic review for its one claim.
