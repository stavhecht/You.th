# Evaluation

A repeatable evaluation of the article pipeline (extract → retrieve → rank → full
text → synthesize → validate) and of the research agent's tool fallbacks. One
command runs a suite, scores every case, prints a summary, writes a report, and
exits non-zero when a critical metric misses its threshold.

```bash
cd backend && source .venv/bin/activate
python -m evaluation.run                     # all 143 cases (114 quality + 29 failure)
python -m evaluation.run --suite retrieval   # stops after RANK — no synthesis, ~20 s/case
python -m evaluation.run --suite citations
python -m evaluation.run --suite failure     # fault-injected cases, mostly offline
python -m evaluation.run --test basic_002 --test adv_018
python -m evaluation.run --limit 20
python -m evaluation.run --save-baseline     # alias: --baseline
python -m evaluation.run --mode replay       # recordings only, no network: for CI
python -m evaluation.run --rescore evaluation/reports/<run>   # re-score saved traces
python -m evaluation.run --resume  evaluation/reports/<run>   # finish an interrupted run
python -m evaluation.run --list              # suites, datasets, case counts
```

Needs what the pipeline needs: Ollama running with the configured models (the
run checks first and **skips**, rather than fails, cases it cannot run), and
network access to PubMed, Europe PMC and OpenAlex in `cached`/`live` mode. It
needs **no database**: nothing is written where the product reads.

Exit status: `0` all critical thresholds met · `1` a critical threshold missed
(or a regression, with `--fail-on-regression`) · `2` usage/config error.

## What runs, and what is substituted

The stages, the providers, the throttle, the query builder, the scoring
constants, validation, and the LangGraph graph with its refine and revise loops
are the **production code**. The harness passes its own `StageRunner` into
`pipeline/graph.py::build_pipeline_graph` — the seam the orchestrator uses —
and records each stage.

Substituted, each behind the same Protocol the stage already depends on:

| Production | Under evaluation | Why |
|---|---|---|
| shared `httpx.AsyncClient` | `harness/http.py::EvalHttp` | records every call; replays from the cassette; injects faults below `ThrottledClient` |
| Ollama clients | `harness/llm.py` wrappers | records tokens/latency; replays drafts |
| embedding provider | `harness/embeddings.py::CachingEmbedder` | cached per text (embeddings are deterministic) |
| `SourceCache`, `SemanticReranker`, validation session | `harness/store.py` (in memory) | never writes to Postgres; the re-rank's SQL is re-expressed in Python, scoring imported unchanged |
| `ILLUSTRATE`, `PERSIST` | not run | PERSIST would put an article in the review queue; ILLUSTRATE is decorative and off in testing |

`check_stage_parity` compares the evaluated stage list with
`build_default_pipeline` at startup and prints `STAGE DRIFT` if production
gains a stage the eval does not run.

### Modes and reproducibility

`evaluation/.cache/cassettes.sqlite` records every HTTP answer, model output,
embedding and judge verdict, keyed by the request (credentials stripped).

* `cached` (default) — replay what is recorded, call live for the rest and record it.
* `live` — ignore recordings, call everything, refresh them.
* `replay` — recordings only; a case needing anything unrecorded is **SKIPPED** with the reason.
* `frozen` — recordings only for HTTP, but an unrecorded request fails as the outage it
  was (429s are never recorded) instead of skipping the case. Models and embeddings
  behave as in `cached`. Use it to compare two versions of the code on exactly the same
  search results.

429s, 5xx, transport errors and PubMed's 200-status throttle body are never
recorded, so a transient outage is never replayed as a permanent one. A rerun in
`cached` mode reproduces the same papers and drafts, so a difference between two
runs is a difference in code. Change a prompt and that call misses the cache and
runs live, which is what you want.

## What is measured

Every metric reports `n`, the cases or items it rests on; `n = 0` prints `N/A`,
never PASS. Thresholds are in `config.yaml`.

| Group | Metrics | How |
|---|---|---|
| Query understanding | `query_understanding_accuracy`, `context_carryover_rate` | extracted product/ingredients name `expected_subject`; claims name an `expected_outcome_terms` |
| Agent / tools | `tool_selection_accuracy`, `unnecessary_tool_calls_per_case`, `tool_call_count`, `decision_accuracy`, `agent_loop_rate`, `successful_termination_rate` | expected tools called, forbidden ones not; graph branch rules (refine thin claims, no synthesis without sources, coverage re-prompt, revise fixable failures, stop when validated, total search failure stops the run); retries after a throttle vs redundant repeats |
| Retrieval | `retrieval_recall_at_{1,3,5,10}`, `rank_precision_at_{5,10}`, `rank_recall_cap_at_{5,10}`, `rank_mrr`, `rank_ndcg_at_10`, `empty_retrieval_rate`, `duplicate_rate` | gold papers (`relevant_ids`, each verified in PubMed first) for recall; a per-case relevance rule (subject × outcome terms) for the rest |
| Citations | `citation_existence_rate`, `citation_precision`, `citation_correctness`, `citation_completeness`, `citation_recall`, `citation_quality` | sentence-level: which handles each sentence cites; judge verdict per cited statement; finding sentences without a citation; credibility tier of what is cited |
| Sources | `source_existence_rate`, `source_title_match_rate`, `retracted_citation_rate` | PMIDs/DOIs checked against PubMed, Crossref and doi.org through the production `RetractionChecker`, plus a title match against the registry record |
| Appraisal (INFO) | `source_stance_accuracy`, `source_stance_direction_accuracy`, `stance_coverage`, `stance_disagreement_rate` | APPRAISE's per-(source, claim) labels against the case's hand-labelled `expected_stances`, exactly and folded to for / against / neither; pairs labelled over pairs offered; final drafts carrying a `verdict_against_stance`, `refutation_understated` or `verdict_on_off_topic_sources` warning. No thresholds: the labels gate nothing yet, and these numbers decide whether they should |
| Answers | `behavior_accuracy`, `verdict_accuracy`, `answer_correctness`, `answer_relevance`, `answer_completeness`, `groundedness`, `hallucination_rate`, `draft_hallucination_rate`, `uncertainty_appropriate_rate`, `no_answer_correct_rate`, `validation_pass_rate`, `recency_cited_fraction` | outcome vs expected behaviour; verdict vs the case's acceptable range; judge scores; hallucination = invented handle, a number absent from the cited source, a contradicted or unsupported statement, a forbidden claim or phrase, or a citation no registry knows |
| Failure handling | `failure_recovery_rate`, `fallback_success_rate`, `infinite_loop_rate`, `hallucination_after_tool_failure_rate`, `retry_classification_accuracy` | fault-injected runs: controlled outcome, bounded retries, the fallback tool still answered, no article after every search failed, transient faults marked retryable |
| Performance | `latency_{mean,median,p95,p99}_s` + per-tool and per-stage tables | wall clock as run, and the **live** latency of each call (recorded with the response, so replayed runs still report real costs); tokens; cost from `llm/pricing.py` (unpriced models report "unpriced", never $0) |

`hallucination_rate` is over articles that passed validation — what would
reach a reviewer. `draft_hallucination_rate` is over every final draft, before
validation filters it. The gap between them is what the validator is catching.

### The judge

`evaluators/llm_judge.py` asks two narrow questions: does this source text
support this statement (per cited sentence), and how relevant, complete and
correctly-hedged is the article (per case, plus expected/forbidden claims). It
runs through the production `structured_chat` helper, so its output is
grammar-constrained JSON.

**It must be a different model from the generator.** The default judge is
`llama3.1:8b`, while synthesis runs on `qwen2.5:7b-instruct`. If the two are
configured to match, the run disables the judge and says why. It never sees the
pipeline's validation report or verdict ceiling, and it is shown the article
with its parts labelled — every article opens by restating the claim it checks,
and an unlabelled first paragraph was measured being read as the article's own
assertion. A statement counts as hallucinated on statement-support grounds only
when the judge's "not_supported" coincides with low lexical support, or when a
deterministic signal fires (an invented handle, a number absent from the cited
source). The one judge-only signal is an asserted *forbidden claim*: no
deterministic check can read meaning, so each is named in the report for a
person to confirm. It is a small model, so the report also lists the label mix
behind `citation_correctness` to make its strictness visible.

## Output

Each run writes `evaluation/reports/<timestamp>_<suite>/`:

* `report.md` — metrics with PASS/WARN/FAIL/N/A, the latency table, regression vs
  baseline, capability gaps, a per-case summary, and a debug block for every
  failed case (query, how it was understood, tools called, agent decisions,
  ranked documents with relevance labels, the article, each citation's registry
  status and judge verdict, every check with its reason).
* `results.json` — everything above, machine-readable.
* `cases.jsonl` — one scored case per line, written as the run goes.
* `traces/<case>.json` — the full trace: every tool call with sanitised inputs,
  status and latency; every stage span and its metrics; each retrieval round's
  candidate pool and ranked list; every draft; each validation report; the
  routing decisions. Outputs only — no model reasoning is requested or stored.

`evaluation/reports/LATEST` names the most recent run.

## Baselines and regression

`--save-baseline` writes `evaluation/baselines/<suite>.json`: metrics, per-case
pass/fail, the git commit and the environment (models, providers, top-k). Every
later run of that suite prints the deltas, flags metrics that moved the wrong
way by more than `regression.tolerance` (latency: 25% relative), lists cases
that flipped, and says when the environment differs from the baseline's — a
"regression" across a model change is a model change. Regressions WARN by
default; `--fail-on-regression` (or `fail_on_regression: true`) makes them fail
the run.

## Adding cases

Datasets are JSONL in `evaluation/datasets/`, one case per line; lines starting
`//` are comments. `regression.jsonl` holds `{"ref": "<id>"}` lines that point
at cases defined elsewhere. Ids are unique across every file. The schema is
`evaluation/schema.py::EvalCase`, which rejects unknown fields, so a typo fails
the load rather than silently dropping an expectation.

Minimal:

```json
{"id": "basic_101", "category": "simple_factual", "query": "Does taurine improve endurance?"}
```

That alone yields termination, tool selection, decisions, citation validity,
groundedness, hallucination and latency. Each optional field adds a check:

```json
{
  "id": "basic_102",
  "category": "simple_factual",
  "query": "Does creatine improve muscle strength?",
  "expected_behavior": "answer_with_citations",
  "acceptable_verdicts": ["supported"],
  "expected_subject": ["creatine"],
  "expected_outcome_terms": ["strength", "muscle"],
  "relevant_ids": ["pmid:28615996"],
  "expected_source_types": ["systematic_review", "meta_analysis", "rct"],
  "required_concepts": [["resistance training", "weight training"]],
  "expected_claims": ["Creatine with resistance training increases strength"],
  "forbidden_claims": ["Creatine is an anabolic steroid"],
  "forbidden_phrases": ["guaranteed"],
  "min_citations": 3
}
```

* `expected_behavior`: `answer_with_citations` · `acknowledge_insufficient_evidence` ·
  `acknowledge_uncertainty` · `reject_false_premise` · `refuse_or_clarify` ·
  `controlled_failure` · `degrade_gracefully`. Each implies default acceptable
  outcomes and verdicts (`schema.py::DEFAULT_OUTCOMES` / `DEFAULT_VERDICTS`);
  override with `acceptable_outcomes` / `acceptable_verdicts`.
* `expected_subject` / `expected_outcome_terms` are word prefixes ("depress"
  covers "depression"). They drive both query understanding and the relevance
  rule, so write them as the literature would phrase them.
* `relevant_ids` must be real: each is checked against PubMed or doi.org at the
  start of a run, and unconfirmed ones are dropped and listed in the report.
  Never write one from memory.
* `expected_stances` maps each claim, worded exactly as extraction wrote it in
  the recorded run, to `pmid:`/`doi:` ids and their stance (`supports` ·
  `no_effect` · `contradicts` · `unclear` · `off_topic`). Per claim because one
  paper can point different ways for two claims. A claim worded differently in
  a new run is listed as unmatched, not scored. Label from the paper's **abstract** only: that is all APPRAISE is shown,
  and grading it against knowledge it never had measures the wrong thing. A
  paper is scored only when it was ranked and labelled.
* `expected_tools` / `forbidden_tools` name tools as the trace records them
  (`pubmed`, `europe_pmc`, `openalex`, `europe_pmc_lookup`,
  `europe_pmc_fulltext`, `llm_extraction`, `llm_appraisal`, `llm_synthesis`,
  `embeddings`).
  `ideal_tools` names something this system does not have (`news_api`,
  `web_search`); it feeds the capability-gaps section only.
* Multi-turn: `"turns": [{"query": ...}, ...]` plus `conversation_subject`. Only
  the last turn reaches the pipeline, because `PipelineContext` carries no
  history. The judge sees the whole conversation.
* Failure cases: `faults` (see `FaultSpec`: target, kind, status, operation,
  param_contains, after, times), `expected_fallback`, `expect_retryable`, and
  `llm: "scripted"` when prose is not the subject. `research_scenario` names a
  scenario in `harness/research.py`.
* Prompt injection: `inject_documents` appends a synthetic record to one
  provider's results. Its DOI must start `10.0000/eval-`.

**Wellness trends** (`wellness_trends.jsonl`, 107 cases) cover every entry of the
100-trend list: `subcategory` is the trend's category, tags carry its number
(`wt_001`…`wt_100`), the question's angle, and `verify` where the reference answer
was written from general knowledge and not yet checked against a source. Run them
in stages: `--suite wellness_pilot` (19), `wellness_core` (50), `wellness` (all).
They are not in `all`.

To scale to 500+ cases, add lines. Nothing is enumerated in code except the
research scenarios.

## Known limits

* The judge is a local 8B model. Treat single-case judge verdicts as leads, and
  aggregate judge metrics as indicative; the deterministic metrics do not
  depend on it.
* The relevance rule is lexical. It cannot tell a paper that mentions creatine
  in passing from one about creatine. Gold `relevant_ids` are the stronger
  ground truth, and cover a subset of cases.
* Retries are classified and bounded, not exercised end to end: a retryable
  failure is recorded as such instead of waiting the worker's 60-second backoff.
* Live latency in `cached` mode is the recorded latency of each call. Wall clock
  per case is only meaningful in `live` mode or for calls that missed the cache.
