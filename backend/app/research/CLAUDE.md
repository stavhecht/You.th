# Research agent: Trend Search Agent + evidence check

The second proposer: what people are getting interested in, then a literature check.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/04b-topic-discovery.md](../../../docs/design/04b-topic-discovery.md).

## Rules

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
  bounded at 5 and enrichment at 4 — see "Exercised" in [backend/CLAUDE.md](../../CLAUDE.md)).
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
