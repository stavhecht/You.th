# Pipeline: stages, orchestrator, worker, synthesis

The nine stages, the graph that orders them, transaction boundaries, the worker, and what SYNTHESIZE does to a draft.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03-frontend-auth-pipeline.md](../../../docs/design/03-frontend-auth-pipeline.md) §3.3.

## Stages and the graph

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

## Transactions, retries, the worker

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

## RANK: which claims an article covers

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

## APPRAISE

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

## SYNTHESIZE: zero sources

**Zero usable sources means no generative call at all.** `SynthesizeStage` branches to
`services/no_evidence.py`, which assembles the `SynthesisOutput` deterministically. A model
handed a product name and no abstracts is the ungrounded generation the pipeline exists to
prevent, and it would emit no citations — so invariant #2 would pass by vacuum rather than by
verification. The draft still takes the ordinary path and passes validation on its merits (empty
handle set, `no_evidence` exempt from the cited-beat rule, `best_grade([])` is `UNKNOWN`) and a
human still approves it. This is the pair to the throttle rule in [retrieval/CLAUDE.md](../retrieval/CLAUDE.md): "we searched and found
nothing" is publishable, "we could not search" is not, and they reach synthesis as the same
empty list. `metrics.no_evidence_cause` separates nothing-retrieved from filtered-away.

## SYNTHESIZE: coverage re-prompt

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
(see the sizing table in [llm/CLAUDE.md](../llm/CLAUDE.md)) — and length was never the defect anyway. The measured failure is
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
67.0% → 63.3%, and three articles fell to about 200 words (wt_011, wt_090, wt_098). The search changes in [retrieval/CLAUDE.md](../retrieval/CLAUDE.md) confound
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

## SYNTHESIZE: reader edit and repair

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
`misattributed_citation` (in [evidence/CLAUDE.md](../evidence/CLAUDE.md)), so the repair is the last place it can be stopped: in the
107-case wellness run, 4 of the 5 judged hallucinations were repairs that kept or introduced one.
