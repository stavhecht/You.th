# 4b. Topic discovery: the research agent

Two things propose topics, and they answer different questions:

- The **MeSH literature scan** (`app/discovery`, runs every 24 h in the API process) finds
  substances whose *publishing rate* is accelerating.
- The **research agent** (`app/research`, added 2026-09-30, split 2026-10-12) finds what
  *people* are getting interested in, then checks what the literature can responsibly say
  about the best of it.

Both **propose and never enqueue**. A reviewer pressing *Generate draft* is what creates a
pipeline run, and the draft still needs *Approve* to publish (invariant #1). This was an
explicit decision when the research agent was specified as "generate 4–6 articles weekly":
automatic research, human promotion.

## Two halves: the Trend Search Agent, then the evidence check

A research run is one LangGraph workflow in two fragments (`research/graph.py`):

```
TREND SEARCH AGENT (research/trends/) — no scientific evidence anywhere
load_config → collect_sources → normalize_items → cluster_topics → check_history
  → enrich_topics → score_topics → select_trends ──(evidence check wired, nothing failed)──┐
                                                └──→ END (standalone: scripts/trend_search) │
EVIDENCE CHECK (research/evidence.py)                                                       │
research_science → deep_research → select ─(fewer than min, reserve left, round 0)→ backfill
     ↑______________________________________________________________________________|
                                       └→ END
```

The split exists so each number means one thing. A **trend score** means "how much is this
trending" and contains no evidence; a **proposal** on the desk means "trending, *and* there is
literature to write from". Before the split, evidence was 25% of a single score and required
for any ranking, so the two could not be read apart, and every candidate paid for PubMed
counts whether or not it was trending.

- **The trend phase never marks a topic `selected`.** It ranks, and marks the best
  `evidence_pool_size` as `in_evidence_pool`. Only the evidence check's `select` produces
  `selected`, because the desk renders Promote buttons for those, and polls mid-run.
- **The trend output is frozen when its phase ends** (`research_runs.summary`) and served by
  `GET …/runs/{id}/result` as `TrendSearchResult`. Each topic carries
  `scientific_assessment: "not_assessed"` and the evidence check's outcome as a status
  (`downstream.evidence_gate`: passed / failed / not_checked), never as a score.
- **Nothing can switch the evidence check off per run.** A trigger may narrow sources and
  raise thresholds; settings are the floor.

## Who does what

```
n8n weekly schedule ──┐                                 ┌── desk: "Find trending topics"
n8n manual webhook ───┴→ POST /api/automation/research/runs   POST /api/console/research/runs ─┘
                                   (X-Trigger-Token)            (reviewer JWT)
                                          └──────────┬──────────┘
                           research/service.py::start_research_run(mode, params)   ← one entry point
                                                     │  research_runs row, status=queued
                                   pipeline worker claims it (same process as article runs)
                                                     │
                                   research/graph.py — trend search, then evidence check
                                                     │
                            research_candidates  +  research_topic_observations (history)
                                                     │
                     reviewer: Generate draft → _enqueue_run(origin=research) → article pipeline
```

- **n8n decides when, with what, and who hears about it.** The weekly workflow sends
  `RESEARCH_WEEKLY_PARAMS`, polls, fetches `/result` and POSTs a summary to
  `RESEARCH_NOTIFY_URL`; a start that fails for any reason but 409 stops the execution with an
  error. It contains no research logic. Setup is in `n8n/README.md`.
- **The desk button calls the backend directly**, not n8n, so it works when n8n is down.
- **Runs are single-flight**, by a SELECT check and the partial unique index
  `research_runs_one_active` that holds when two triggers race.
- **Research runs execute in the pipeline worker, one thing at a time**, because both kinds of
  run count papers against NCBI's per-IP ceiling through a per-process throttle.

## Sources

One protocol, `TrendSource` (`trends/sources/base.py`): a name, a source type
(search / news / feed / web / video / community), capabilities (discover, search, volume,
interest), and `search(SourceQuery) → SourceBatch` of normalised `SourceItem`s. A source that
could not answer raises `ProviderUnavailable` — `permanent` for a rejected key or a spent
quota — and never returns `[]`. `research/factory.py` is the only place that knows the
implementations; a new source is a class there and a line in the factory.

| Source | Access | What it gives | Limits and decisions |
|---|---|---|---|
| Publisher RSS/Atom (`rss.py`) | none | discovery: headlines per feed, 21 feeds in `trends/feeds.py` | Conditional GETs (ETag / Last-Modified); a 304 replays the stored items. Feeds refusing a reader (NIH, Mayo: 403) are not included. Overridable by `RESEARCH_SOURCES_FILE`. |
| Google Trends (`GoogleTrendsSource` over the existing provider) | none (pytrends, archived) | discovery: rising queries per seed; enrichment: anchored interest series | Relative indices, never search counts. Breaks or throttles when Google changes it; an official alpha API exists but was not generally available. |
| GDELT DOC 2.0 (`gdelt.py`) | none | discovery: article lists per category; enrichment: 3-month daily volume (the baseline) | ≤1 request / 5 s per IP, answers errors as text with status 200, and `{}` is treated as unmeasured (see below). |
| News (`NewsSearchSource` over News API → DuckDuckGo news) | `NEWS_API_KEY` optional | enrichment: articles per topic in two windows | News API's free plan is development-only. |
| Web (`WebSearchSource`: Brave, else ddgs) | `BRAVE_SEARCH_API_KEY` optional | enrichment: pages per topic | Counts toward source *types*, never toward outlets this week: web pages are evergreen. ddgs scrapes DuckDuckGo and is the keyless fallback. |
| Google News RSS (`google_news.py`) | none | discovery and enrichment, the richest free news signal | **Off by default**: the feed says it is "for personal, non-commercial use". Redirect links are never followed. |
| YouTube Data API v3 (`youtube.py`) | `YOUTUBE_API_KEY` | uploads per window, median views per day | ≤10 `search.list` (100 units each) per run; key in a header; lifetime views never used. |
| Reddit, TikTok, Instagram | — | — | **Not implemented.** Reddit's Data API is approval-gated and did not work for this project (an adapter was built, then removed 2026-10-13). TikTok's Research API is limited to non-profit academic researchers. Instagram's Graph API reads owned business accounts, plus a hashtag search under app review. Neither fits without an agreement; the protocol is the seam. |

Every call goes through a per-run budget (`RESEARCH_MAX_REQUESTS_PER_SOURCE`), a phase
deadline (collect 300 s, enrich 240 s), a circuit breaker (three failures in a row), and for
HTTP sources `TransientRetry` over `ThrottledClient`: pacing, `Retry-After`, exponential
backoff with jitter, a per-source minimum wait after a 429, and every retry counted in the
run's provider status.

## Normalising, deduplicating, clustering

- **Items** carry publication and collection time separately, both timezone-aware; a
  timestamp more than an hour in the future is dropped. Descriptions are truncated; article
  bodies are never stored.
- **Stories** (`trends/documents.py`) join items by exact URL, canonical URL (named tracking
  parameters only), headline, and near-duplicate headline (distinct-token Jaccard ≥ 0.8). Thirty
  syndicated copies are one story. **Independent outlets** are counted over stories, by the
  topic's own earliest item in each, among news and feed items only.
- **Topics** come from rising queries, headline *phrases* (1–3 words naming something
  specific, `trends/phrases.py`; one outlet is enough to be *considered*) and manual queries,
  merged by key, then by
  embedding (0.88), ordered by a deterministic pre-score, capped at 75 and triaged by the local
  model in batches of 25 with two example headlines each. Groups the model leaves out are
  asked again once and then set aside.
- **Guards after triage**: items count toward a topic only through queries sharing a word with
  its subject or outcome; a bare category ("protein", "weight loss") is too broad for one
  article; products and shops are shopping intent; a topic filed under an unrequested category
  keeps its seed's category when that was requested.

## The trend score (`trends/scoring.py`, `trend-v1`)

| Dimension | Weight | Sub-signals (each 0–100) |
|---|---:|---|
| Momentum | 30 | Trends interest change (or breakout / rising %); GDELT recent week against its baseline weeks; news counts against the previous window; YouTube uploads; stories against this topic's previous runs |
| Cross-source coverage | 25 | independent outlets `100·(1−2^(−n/4))` (70%); kinds of source with activity, of those that answered (30%) |
| Audience interest | 20 | Trends level against an anchor term; YouTube median views/day; distinct questions asked |
| Freshness | 15 | median age of dated items; newness against earlier runs; novelty against articles already written |
| Editorial fit | 10 | share of items on wellness; a specific subject; an outcome (an angle) |

- **Missing data: reweight, with a floor.** A dimension with nothing measured is left out and
  the rest renormalised. `data_coverage` is the measured share of the whole score, counted per
  sub-signal, and only over sub-signals whose source this deployment has. Below 0.6, or with no
  momentum measured at all, a topic gets **no score** ("insufficient data") rather than a score
  built on what remained.
- **Reproducible.** `score_from_metrics(TopicMetrics, weights)` reads nothing else, and both
  are stored on the candidate and in the trend history.
- **Explanations are templates over measured numbers** (`trends/explain.py`): a topic's
  summary, selection reason and limitations contain a clause only when its number was
  measured. No model writes them.
- **Status** is emerging / accelerating / steady / declining from observations and momentum,
  and **preliminary** whenever nothing compared two periods.
- **Selection** requires a score, ≥ `research_min_trend_score`, measured coverage with ≥2
  independent outlets, one topic per subject and at most two per category, and returns fewer
  than asked rather than padding. A topic proposed in the last 28 days is held back unless its
  stories doubled or its momentum is ≥ 65.

## History

`research_topics` is a topic's identity across runs (`subject|outcome`, normalised, with an
embedding match for drifted wording); `research_topic_observations` is one row per topic per
run, written once when the run finishes, upserted, and pruned after 180 days. Counts of how
often a topic was seen or proposed are derived from observations, never incremented.

## The evidence check

Unchanged in substance, run on the trend pool only:

- `research_science` counts PubMed and Europe PMC (each the other's fallback);
- `deep_research` runs the article pipeline's own extraction and `TemplateQueryStrategy`,
  discarding `UnanchoredQuery` topics;
- `select` applies `research_min_evidence_status` (default `emerging`), ranks by trend score
  with evidence as a tie-break, and one backfill round sends the next trend-ranked topics
  through the whole check.

**Popularity is never evidence**: a breakout with one paper behind it is discarded however
it trends, and the tests pin exactly that case.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Every discovery source fails | Run fails `no_trend_data`. Nothing is invented. |
| Some sources fail | Their sub-signals are unmeasured and the topic's data coverage drops; provider status `partial` / `failed`. |
| A source refuses for good (401/403, spent quota) or fails three times in a row | Not asked again this run. |
| A source runs out of budget or time | `skipped` / `partial` with the reason; unmeasured, not zero. |
| Triage fails outright | Raw queries are used, and the run's notes say so. |
| Triage leaves groups out | Asked again once; still left out, the group is set aside. |
| PubMed down, Europe PMC up (or the reverse) | The other source's counts are used. |
| Both literature sources down | Run fails `scientific_evidence_unavailable`; the frozen trend output is still served (`partial`). |
| Worker dies mid-run | The sweep marks the run `failed` (not requeued); the next trigger starts fresh. |

## Measured on the live runs (2026-10-12)

The first full-breadth trend run (every category; RSS, Google Trends, GDELT, news, web)
took 10.6 minutes and ranked 10 topics. Four of the top six were things a reviewer would
reject at a glance, and each became a guard:

- **"soulja boy running for president"** ranked fourth: the seed "running" made it look on
  topic, and triage had left its group out (16 of 75), so it ran on its raw query. Left-out
  groups are now asked again, then set aside.
- **"protein", "strength training", "weight loss"** ranked: categories, not angles. A bare
  subject that is a seed or only generic words is now too broad for one article.
- **"running shoes"** ranked second: a product. Product and shop words are shopping intent.
- **"coneflower for immune support"** cited two articles about carbohydrates: triage had
  grouped a carbs phrase into it. Items now count only through queries sharing a word with
  the subject or outcome.

**Exact phrases rarely recur across outlets.** A week of 356 feed stories from 12+ outlets
had no specific wellness term in two outlets' headlines, so a two-outlet floor at discovery
found no topics at all. Discovery now considers one outlet's specific term, and the
cross-source rule is applied at selection, after enrichment has searched each source.

**End to end through the worker** (2026-10-13, default run): 6.4 minutes, 448 items, 12
candidates enriched, 7 evidence-checked, 5 selected, all with linked sources.

**GDELT** refused nearly every request from the development network (429 with no
`Retry-After`, even 12 s apart) and once answered `{}` for "creatine" over a week while Google
News listed 60 articles. `{}` is therefore unmeasured, and GDELT's failure costs a momentum
sub-signal rather than the run. **Google News RSS** answered every request; it is off by
default for its terms, not its reliability.

## Measured on the first live runs (2026-09-30), before the split

**Google Trends' *rising* lists can be spam.** For "fitness" and "protein" over seven days,
the rising list was mostly unrelated commercial queries at +7,000% or more: "learn golang",
"online banking review", "cheap flights new york". The *top* lists were sane (planet fitness,
whey protein), and "sleep" rising was real.

The first run, before any defence, selected four product-review queries out of five. Three
layers now stand between that and the desk:

1. **Deterministic.** A query must share a word with its seed or with `WELLNESS_TERMS`, and
   shopping, job and litigation words make it a non-topic. `review` and `comparison` are
   shopping intent.
2. **The triage model.** It works in batches of 25; a topic spanning more than 6 groups, or
   with a subject like "various", is refused. Its output is then deduplicated across batches
   (same topic, or the same subject and outcome), and a query equal to its own seed is
   dropped. The second run had proposed "gut health" and "Gut Health" as two topics, and
   "sleep" as a topic under the seed "sleep".
3. **The evidence gate.**

The same run showed why the first layer cannot be the evidence gate. Deep research gave
"online banking review" *moderate* evidence: the extraction model invented plausible claims,
and the anchored query found 14 papers. Any query can find some literature.

## Assumptions

- Google Trends is reached by scraping. It will break or throttle, and the source interface
  is the mitigation. SerpApi (paid) or Google's Trends API, once generally available, are the
  likeliest replacements.
- The free news signals are thin without Google News (off by terms) and GDELT (rate-limited).
  A deployment that can license a news API will see far more outlets per topic.
- A promoted research topic runs through the unchanged article pipeline with
  `origin = research`. Its canonical topic string is what extraction reads.
