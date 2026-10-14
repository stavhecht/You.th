# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All backend commands run from `backend/` with the venv active (`source .venv/bin/activate`).

```bash
# setup
docker compose up -d db                  # pgvector/pgvector:pg16 on :5432
pip install -e ".[dev]"
python -m scripts.migrate                # applies migrations via asyncpg; no psql needed
                                         # 0001_initial.sql is the only file — squashed a fifth
                                         # time on 2026-09-30, folding in source_chunks,
                                         # chunk_settings, full-text excerpts and the research
                                         # tables, and dropping 7 write-only columns. The data
                                         # was COPIED into a fresh database, not discarded; the
                                         # old copy, evidwell_backup_20260930, was dropped on
                                         # 2026-10-14. Squashing
                                         # is only free while every database holding the schema
                                         # can be rebuilt; the file's header states that
                                         # condition. 0002 swaps the subject enum
                                         # for editorial categories; 0003 moves the
                                         # research agent onto them. 0004 adds
                                         # article_sources.stance (APPRAISE). 0005 adds
                                         # the trend search's topics, observations and
                                         # feed state. 0006 adds public
                                         # search (tsvector + pg_trgm). Next
                                         # is 0007.
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
mypy app                                 # NOT clean — see below

# evaluate — quality metrics over 143 cases; see evaluation/README.md
python -m evaluation.run                 # exits 1 when a critical metric misses its threshold
python -m evaluation.run --suite retrieval --limit 20   # stops after RANK, no synthesis
python -m evaluation.run --test basic_002 --save-baseline

# frontend, from frontend/
npm run dev            # :5173, public site at /, review desk at /review
npm run typecheck
npm run build          # tsc -b && vite build
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
  rebuild — only a `pyproject.toml` change needs `--build`. Both watchers are forced to polling
  (`WATCHFILES_FORCE_POLLING`, `VITE_DEV_POLL`): Docker Desktop's bind mounts do not deliver
  inotify events on macOS, so an event-based watcher looks healthy and silently never reloads.

Two declared checks do not currently pass, so don't read a failure as something you broke:

- `npm run lint` is in [package.json](frontend/package.json) but eslint is neither installed
  nor configured — the script fails outright. Use `npm run typecheck`.
- `mypy` is configured `strict = true` in [pyproject.toml](backend/pyproject.toml) but reports
  ~30 pre-existing errors across 12 files (mostly untyped-dict returns from service methods
  flowing into response models, plus `voyageai` stubs). It is not in the verified set. `ruff`
  and `pytest` are the real gates.

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
| `test_reader_accounts.py` | the composite FK on `reader_saves`, the save-is-a-move primary key, folder-name uniqueness, and the contact `CHECK` |
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
`_mesh_within` has had its chance to find a known substance inside it. **Identical searches
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
  invented one is dropped, and `article_sources.stance` is NULL for it (migration 0004) — the
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
  recordings are still keyed by model + payload + feedback only: after editing the *synthesis*
  prompt, run the eval with `--mode live` or the drafts are the old prompt's.
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
ingest screen only ever catches what was already withdrawn when we first saw it.

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
190px measure (`MasonryFeed`'s `columnWidth`), fed by `GET /console/articles/{id}/card` — which
runs the *same* `derive_card()` `approve()` runs. Both halves are deliberate: a console-local
tile clone, or a TypeScript port of `derive_card`, would be a second definition free to drift,
and a preview that drifts from what publishes is worse than none because it is reassuring
rather than useful. Console → feed is the permitted direction; the reverse is what the boundary
forbids. Every tile in the overlay is `inert` — `ArticleCard` renders a live Save button and
links to `/a/:slug`, which 404s until publication. The button flushes autosave before opening,
for the same reason Approve does.

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
(migration 0006, DESIGN.md §3.1): full text first, a trigram typo fallback only when that finds
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

**Subjects are editorial categories, not kinds of object** (migration 0002, 2026-10-01): Fitness,
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
- **Single-flight is an index now** (`research_runs_one_active`, migration 0005). The SELECT
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
  top-k, eats the margin.
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
  test and leave the process exposed. **There are three separate instances** of it —
  reviewer login, reader login, and the contact form (`api/deps.py`). One shared counter
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
  abstract. It falls back to full text only when the scoped search is empty ("zone 2
  training": 0 against 20). Its study-type tags are coarser than PubMed's, so a paper only it
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
