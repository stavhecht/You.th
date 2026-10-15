"""Application settings, loaded from the environment.

``embedding_dim`` is the one to be careful with: it must match both the active
embedding provider's output width and the ``vector(N)`` column in the applied
migration. ``validate_embedding_dim`` asserts the first two agree at startup,
because a mismatch otherwise surfaces as an opaque pgvector error at the first
write — or worse, as silently degraded retrieval.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: A list field that accepts `a,b,c` as well as a JSON array.
#:
#: ``NoDecode`` is load-bearing, not decoration. pydantic-settings treats any
#: complex-typed field as JSON and calls ``json.loads`` inside the dotenv
#: *source*, which is upstream of every validator — so a plain
#: ``mode="before"`` validator never sees the value, and `FOO=a,b` raises
#: SettingsError before it can be split. ``NoDecode`` suppresses that decode and
#: hands the raw string to `_split_csv` below.
CsvList = Annotated[list[str], NoDecode]

#: Weakest to strongest. Mirrored by ``research/contracts.py::EvidenceStatus``;
#: kept as strings here so config does not import the research package.
_EVIDENCE_STATUSES = ("none", "limited", "emerging", "moderate", "strong")
#: Mirrored by ``research/contracts.py::SOURCE_NAMES``, for the same reason.
_TREND_SOURCES = (
    "google_trends", "rss", "google_news", "gdelt", "news", "web", "youtube",
)
#: How far back trend status and newness read a topic's observations.
_TREND_STATUS_LOOKBACK_DAYS = 56


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), extra="ignore", case_sensitive=False
    )

    debug: bool = False

    # --- database ---
    database_url: str = "postgresql+asyncpg://evidwell:evidwell@localhost:5433/evidwell"
    db_echo: bool = False

    # --- auth ---
    jwt_secret: str = Field(
        default="dev-only-insecure-secret-change-me-32-chars",
        min_length=32,
        description="HS256 signing key",
    )
    cors_origins: CsvList = ["http://localhost:5173"]

    # --- generative provider ---
    #: 'ollama' | 'anthropic'. Ollama is the default while the pipeline is being
    #: built: every generative call runs locally, costs nothing, and needs no
    #: key. Set LLM_PROVIDER=anthropic for real deployment — the hosted client
    #: is fully wired and nothing else changes. See llm/factory.py.
    llm_provider: str = "ollama"          # "ollama" | "anthropic" | "llamacpp"

    # --- Ollama (local, free) ---
    ollama_base_url: str = "http://localhost:11434"
    #: Minutes, not seconds: a cold model is loaded into memory before the first
    #: token, and a CPU-only synthesis turn over eight abstracts is slow.
    ollama_timeout_seconds: float = 600.0
    #: Both default to the same small model. Extraction is mechanical enough to
    #: survive it; synthesis is where a bigger local model
    #: (qwen2.5:14b-instruct, llama3.3:70b) actually shows, since that call
    #: carries invariants #2 and #3. Pull whatever you set here first —
    #: `ollama pull llama3.1:8b`.
    ollama_extraction_model: str = "llama3.1:8b"
    ollama_synthesis_model: str = "llama3.1:8b"
    #: The APPRAISE call (which way each source points). Not the synthesis
    #: model, on measurement: over 96 hand-labelled (source, claim) pairs on
    #: 2026-10-02, qwen2.5:7b got the direction of studies that test the claim
    #: right 70% of the time and *flipped* 15 of them (it reads good news as
    #: support — a coffee study finding a benefit labelled as supporting "has
    #: no effect on heart health"), while llama3.1:8b got 98% and flipped none.
    #: A flipped label is the error that would push a verdict the wrong way.
    #: llama3.1:8b is also the extraction model, so no extra model is pulled,
    #: and it is often still resident from EXTRACT when APPRAISE runs.
    #: Empty means the synthesis model.
    ollama_appraisal_model: str = "llama3.1:8b"

    # --- llama.cpp server (local; used when llm_provider='llamacpp') ---
    #: For models Ollama cannot load — Ternary-Bonsai-2-27B needs PrismML's
    #: llama.cpp fork — through `llama-server`'s OpenAI-compatible endpoint.
    #: Extraction, appraisal and synthesis all run on it; embeddings stay on
    #: EMBEDDING_PROVIDER. Every generative call, not just synthesis, because
    #: on a 16 GB Mac it cannot share the GPU with an Ollama language model
    #: (see llm/llamacpp_client.py).
    llamacpp_base_url: str = "http://localhost:8080"
    #: A label, not a lookup: llama-server serves the one model it was started
    #: with. It names the model in the run record and the price table.
    llamacpp_model: str = "ternary-bonsai-2-27b"
    #: Sent as `reasoning_effort`. "none" turns Bonsai's thinking off (a budget
    #: of 0 does not); "medium" is the vendor's recommended shorter setting.
    #: Bonsai's template rejects "high" with a 500, and "low" thinks about as
    #: long as "xhigh" (PrismML KNOWN_ISSUES, 2026-09-23).
    llamacpp_reasoning_effort: str = "none"

    # --- Claude (hosted; used when llm_provider='anthropic') ---
    anthropic_api_key: str = ""
    #: Split by job, not by tier-for-its-own-sake. Extraction is a short,
    #: mechanical structured response. Synthesis is where invariants #2 and #3
    #: are produced, so it keeps the stronger model — raise it back to
    #: "claude-opus-5" if drafts start failing citation or grade validation.
    #: Measured, not assumed: claude-haiku-4-5 left `ingredients` empty in 9 of
    #: 10 samples, and an empty ingredient list silently unanchors the PubMed
    #: query (query_builder._compose falls back to claim keywords, turning
    #: "ashwagandha for stress" into a search for stress in general).
    #: claude-sonnet-5 was 0/10 on the same test. Do not lower this without
    #: re-running that check.
    extraction_model: str = "claude-sonnet-5"
    synthesis_model: str = "claude-sonnet-5"
    #: Empty means ``synthesis_model``. Anything else needs an entry in
    #: ``llm/pricing.py`` or its cost reports as unknown.
    appraisal_model: str = ""

    # --- embeddings ---
    embedding_provider: str = "ollama"  # 'ollama' | 'voyage' | 'openai'
    voyage_api_key: str = ""
    openai_api_key: str = ""
    #: mxbai-embed-large is the local default because it is 1024-d, the same
    #: width as voyage-4 — so the move from local to hosted embeddings is a
    #: re-embed but not a migration. A 768-d model is both.
    ollama_embedding_model: str = "mxbai-embed-large"
    #: Only consulted for a model absent from
    #: ``llm/embeddings/ollama.py::KNOWN_DIMENSIONS``; prefer adding it there,
    #: so startup checks the width instead of taking your word for it.
    ollama_embedding_dim: int = 1024
    #: Must equal the provider's dimension AND the migration's vector(N).
    embedding_dim: int = 1024

    # --- scholarly providers ---
    pubmed_api_key: str = ""
    semantic_scholar_api_key: str = ""
    openalex_mailto: str = ""
    #: Free from openalex.org. Without one, OpenAlex requests share a small
    #: daily budget per IP and fail with 429 once it is spent.
    openalex_api_key: str = ""
    #: Providers enabled for retrieval. Phase 1 runs PubMed alone by design —
    #: one API learned properly beats four half-integrated.
    enabled_providers: CsvList = ["pubmed"]
    http_timeout_seconds: float = 30.0
    #: How many times a throttled provider call is retried before it is
    #: recorded as a failure. Two rides out a burst; more would let one
    #: throttled provider hold a run open indefinitely, and the worst case is
    #: already this many waits of up to the cap below. Per-provider request
    #: rates are constants in ``retrieval/factory.py``, not settings — they are
    #: facts about each API rather than things to tune.
    provider_max_retries: int = 2
    provider_max_retry_wait_seconds: float = 10.0

    # --- retrieval tuning ---
    #: Ranked sources kept **per claim**, deduplicated across claims when the
    #: synthesis prompt is assembled. This is the one knob that decides how many
    #: sources an article *can* cite, so it moved 8 -> 12 when articles grew
    #: from three beats to a three-to-five minute read.
    #:
    #: Raising it further means raising `SYNTHESIS_NUM_CTX` in
    #: `llm/ollama_client.py` with it. Ollama's context overflow is silent: the
    #: front of the prompt is dropped, so sources vanish while the instruction
    #: to cite them survives, and the run fails as `hallucinated_handle` with
    #: nothing pointing at the real cause.
    retrieval_top_k: int = 12
    retrieval_max_candidates_per_claim: int = 50
    retrieval_min_year: int | None = None
    #: How many open-access papers per article get full-text excerpts in the
    #: synthesis prompt (pipeline/steps/full_text.py). 0 turns full text off.
    #: Six papers at two ~300-word excerpts each is about 5,000 prompt tokens;
    #: raising it eats into the same Ollama context window as `retrieval_top_k`.
    full_text_max_papers: int = 6
    #: Whether APPRAISE labels each ranked source's direction (supports / no
    #: effect / contradicts) per claim — one model call per claim. Off, the
    #: stage records ``cause: disabled`` and the article is drafted exactly as
    #: before it existed; nothing downstream requires the labels yet.
    appraisal_enabled: bool = True
    #: How APPRAISE asks: ``one_call`` (quote the abstract's sentence about the
    #: claim's outcome, then label it) or ``two_call`` (relevance first, then a
    #: direction call shown only the relevant sources). One call is the default
    #: on measurement — level on flipped directions, slightly ahead on the rest,
    #: half the calls (CLAUDE.md, APPRAISE). Two calls is kept for re-measuring
    #: when the appraisal model changes.
    appraisal_mode: Literal["one_call", "two_call"] = "one_call"
    #: Whether SYNTHESIZE uses APPRAISE's relevance calls to trim its prompt:
    #: a source labelled ``off_topic`` for every claim it was retrieved for is
    #: left out, and the rest show only the claims they were not labelled
    #: off-topic for. Proposed 2026-10-13 after a 22-source prompt was cited by
    #: title words ("Retrieved for" all three claims) rather than by findings.
    #:
    #: **Measured and rejected the same day — leave it off.** 18 stance cases,
    #: frozen searches, one sample per arm: prompts shrank 38% and latency fell
    #: 242 s → 80 s, but the model cited the same number of sources (77 vs 75),
    #: and verdict accuracy and uncertainty each fell 6 points. Four verdicts
    #: rose a level, none fell; the creatine-kidney false premise went
    #: no_evidence → weak with 11 of 12 papers dropped. The filter is biased
    #: toward support by construction: APPRAISE's outcome guard rewrites a
    #: refutation whose quote does not name the outcome to ``off_topic`` and
    #: exempts ``supports``, so dropping ``off_topic`` drops the null trials.
    #: Revisit only after that guard stops producing ``off_topic`` for them.
    synthesis_relevance_filter: bool = False
    #: The plain-language edit (``pipeline/steps/reader_edit.py``): one more
    #: synthesis call that rewrites the draft for everyday readers, kept only if
    #: it changed nothing but the wording. ~3 minutes on llama-server, which
    #: reuses its cache of the source block. False skips it.
    synthesis_reader_edit: bool = True

    # --- pipeline ---
    worker_poll_interval_seconds: float = 5.0
    #: How many times a run may be *started* before a retryable failure is
    #: treated as permanent. Only failures that set ``StageError.retryable``
    #: consume it — a malformed draft is not retried at any budget. Backoff
    #: between attempts is ``orchestrator.RETRY_DELAYS``. A run whose worker was
    #: killed spends the same budget: the attempt is counted when it is claimed.
    pipeline_max_attempts: int = 3
    #: How often the worker reports that its in-flight run is still alive.
    worker_heartbeat_seconds: float = 15.0
    #: Silence after which a `running` run is treated as abandoned. Must stay
    #: comfortably above the heartbeat interval — see the validator below.
    worker_stale_after_seconds: float = 120.0
    #: How often the worker looks for abandoned runs. Checked between polls, so
    #: a worker busy with its own run does not sweep; with one worker that is
    #: harmless, since the only run it could recover is the one it is running.
    worker_sweep_interval_seconds: float = 60.0

    # --- trend discovery ---
    #: Days of literature one scan measures. Cadence and window are independent:
    #: `discovery_observations` is keyed (descriptor, pmid), so counts are
    #: COUNT(DISTINCT pmid) over a ledger rather than incremented counters, and
    #: running weekly with a 14-day window double-counts nothing.
    discovery_window_days: int = 14
    #: Days each scan re-reads *before* its window start. MeSH indexing lands
    #: days to weeks after a record enters PubMed, so a record inside last
    #: window's dates only becomes visible during this one. Re-reading is free —
    #: observations conflict to a no-op — and the only cost is API calls. Set it
    #: too small and every scan under-counts the newest half of its own window,
    #: which is exactly the half a trend detector exists to see.
    discovery_overlap_days: int = 21
    #: Completed windows averaged into a descriptor's baseline. Six fortnights
    #: is a quarter: long enough that one quiet fortnight is not a surge, short
    #: enough that a year-old wave stops reading as new.
    discovery_baseline_windows: int = 6
    #: Windows of history required before *any* candidate is emitted. Below
    #: this the scan writes observations and proposes nothing. A first scan with
    #: no baseline can only rank by raw volume, which floods the desk with
    #: vitamin D and creatine — the three things a reviewer would have named
    #: unaided, arriving with the authority of a ranking. There is deliberately
    #: no volume fallback; build the baseline with
    #: `python -m scripts.scan_trends --bootstrap --apply`.
    discovery_min_baseline_windows: int = 4
    #: Papers in the window before a substance can be proposed. Three is one lab
    #: publishing a series; four is where "several groups" starts.
    #:
    #: Lowered 4 -> 3 on 2026-09-07 because four was starving the desk, measured
    #: rather than guessed. Over a live 14-day window the substance counts ran
    #: 9, 5, 5, 4, then 3, 3, 3, 3, then a long tail at 2 and 1 — so a floor of
    #: four admitted **four substances in the whole corpus** and a floor of three
    #: admits eight. With three of those already promoted the desk had nothing
    #: left to offer, which reads as a broken scan rather than a strict one.
    #:
    #: The cost is smaller than it looks, because this floor is about *trend
    #: detection*, not about how much evidence an article gets. A substance is
    #: proposed on its recent publishing rate; the article it becomes retrieves
    #: independently across all of PubMed (`retrieval_top_k` per claim), so a
    #: substance with three recent papers can still rest on twenty-year-old
    #: systematic reviews. Thin *recent* interest is also already priced in by
    #: the scorer's `log2(1 + n)` term rather than only by this gate.
    #:
    #: Do not read it down to 2: that admits 24 more substances at once, which
    #: is a ranking over noise.
    discovery_min_papers: int = 3
    #: Angles one scan may propose for a single substance. A substance surges as
    #: a whole and gets written about one question at a time — "omega-3 for
    #: muscle recovery" and "omega-3 for skin" are different articles resting on
    #: different papers. Without a cap a single surge takes every slot on the
    #: desk, which is the opposite of the variety the ranking exists to provide;
    #: with a cap of 1 the other angles are never offered at all. Two is a desk
    #: that can show eight substances or four, not one.
    discovery_max_angles_per_substance: int = 2
    #: How far back to look when deciding *which questions* a substance is
    #: studied for. Much wider than the trend window on purpose — they are
    #: different questions with different timescales. "Is omega-3 surging?" is a
    #: fortnight's question; "what is omega-3 studied for?" is a slow fact.
    #: Measured 2026-09-06: a 21-day window produced four usable angles across
    #: the whole corpus; sixty days produced caffeine against strength,
    #: endurance, cognition and heart rate. Costs nothing — it reads the
    #: observation ledger, not PubMed.
    discovery_angle_lookback_days: int = 90
    #: Papers backing a *single angle* before it is worth proposing separately.
    #: A different question from `discovery_min_papers`: that floor asks whether
    #: the substance is moving, this one asks whether there is enough to write a
    #: specific piece. Measured 2026-09-06 — omega-3 had 8 papers over 55 outcome
    #: descriptors, so an angle floor set at the *then* substance floor of 4
    #: would have collapsed every substance back to one angle.
    #:
    #: It sat below the substance floor until 2026-09-07, when that floor came
    #: down to 3 and the two became equal. Left at 3 rather than dropped to 2:
    #: two co-occurring papers is too thin to name an article's specific subject,
    #: and equality is not the no-op the old note warned about — it only means a
    #: substance at exactly the floor needs *all* its papers on one outcome to
    #: earn an angle. Everything else falls back to a bare topic, which is what
    #: `polyphenols` and `triterpenes` did on the first scan after the change.
    #: **Never set it above `discovery_min_papers`**: that is unreachable by
    #: construction, and every angle would silently become a bare topic.
    discovery_min_papers_per_angle: int = 3
    #: Descriptors appearing in more than this share of a scan's records are
    #: stoplisted automatically, whatever `discovery/vocab.py` says. MeSH has
    #: ~30,000 descriptors and check tags change between editions, so a hand
    #: list will always be missing something — and what it misses is always the
    #: same shape: a term on most of the corpus, which by construction cannot
    #: distinguish any part of it.
    discovery_document_frequency_ceiling: float = 0.35
    #: Candidates one scan may propose, and so the most the desk ever holds —
    #: `expire_absent` retires anything this scan did not re-rank, so the desk is
    #: exactly the last scan's list.
    #:
    #: This is the reviewer-flooding control: it is literally the number of
    #: "spend money" buttons on the screen. Six rather than eight by request.
    #:
    #: **It is a ceiling and has never been the reason the desk looks empty.**
    #: `rank_candidates` is asked for `limit * 3` precisely so suppression can
    #: remove already-decided topics without shrinking the desk — the next-best
    #: candidates are pulled up automatically. When the desk shows three, the
    #: binding constraint is `discovery_min_papers`, not this. Check the dry
    #: run's "N of M above the floor" line before raising it.
    discovery_max_candidates: int = 6
    #: Records efetched per scan across all seeds. A hard request budget, not a
    #: tuning knob: NCBI answers sustained overage by blocking the IP, and this
    #: scan shares that IP with the pipeline worker.
    discovery_max_records_per_scan: int = 4000
    #: How long a dismissed candidate stays suppressed. Half a year, because a
    #: reviewer who said no is saying no to *this* evidence base, and
    #: re-proposing it next fortnight with two more papers is how a proposal
    #: queue teaches people to stop reading it. The paper count must also have
    #: doubled — see `DiscoveryService.suppression_reason`.
    discovery_dismiss_cooloff_days: int = 180
    #: Seeds to query, by name from `discovery/seeds.py`. Narrow it to debug one
    #: net; empty means all of them.
    discovery_seeds: list[str] = []
    #: Run the scan on a timer, so the desk fills without anyone pressing a
    #: button. Safe to leave on: a scan *proposes* and never enqueues, so it
    #: spends PubMed requests and no tokens at all — the entire generative bill
    #: still sits behind a reviewer choosing to promote something.
    discovery_scan_enabled: bool = True
    #: How stale the last *succeeded* scan may get before the scheduler runs
    #: another. Measured from the ledger rather than from process start, which is
    #: what makes a restart cheap and a missed slot self-healing — the same
    #: reasoning as `discovery_overlap_days`.
    #:
    #: Daily against a 14-day window is deliberate over-sampling: MeSH indexing
    #: lags entry by days to weeks, so a topic's counts keep moving after the
    #: window that will end up owning them, and re-reading a covered window is
    #: free (`discovery_observations` is ON CONFLICT DO NOTHING).
    discovery_scan_interval_hours: int = 24
    #: How long an observation stays in the ledger, by entrez_date. The scorer
    #: reads the current window plus `discovery_baseline_windows` before it
    #: (98 days at the defaults) and angles read `discovery_angle_lookback_days`
    #: (90); nothing reads further back, so a succeeded scan deletes older rows.
    #: Measured 2026-09-30: 51% of the ledger was past this and never read. The
    #: validator below keeps it above what the scorer needs.
    discovery_observation_retention_days: int = 120

    # --- research agent: trend search, then the evidence check ---
    # See app/research/ and DESIGN.md "Research agent". A research run
    # *proposes*: it never enqueues a pipeline run, exactly like the MeSH scan
    # above. Its first half (app/research/trends/) finds and scores what people
    # are getting interested in; its second half checks the literature for the
    # best of those. These are the only definitions of these values — the
    # graph, the scorers and the API read them from here.
    #: How many topics a run selects for the desk. Clamped to [min, max] when a
    #: request asks for a specific number.
    research_target_article_count: int = 5
    research_min_article_count: int = 4
    research_max_article_count: int = 6
    #: Candidate topics kept after clustering and triage.
    research_max_candidates: int = 50
    #: Trend sources a run may use. A trigger can narrow this list, never widen
    #: it. A source needing a key it does not have is skipped and says so.
    #: `google_news` is deliberately absent: its feed is published "for
    #: personal, non-commercial use" (see research/trends/sources/google_news.py)
    #: — add it here only if that fits this deployment.
    research_enabled_sources: CsvList = [
        "google_trends", "rss", "gdelt", "news", "web", "youtube",
    ]
    #: Requests a run may send each source. Bounds cost and time; a source that
    #: runs out is reported `skipped: budget`, which is unmeasured, never zero.
    research_max_requests_per_source: int = 40
    #: Wall-clock limits on the collect and enrich phases. Requests still
    #: running at the deadline are abandoned and reported, not waited for.
    research_collect_deadline_seconds: float = 300.0
    research_enrich_deadline_seconds: float = 240.0
    #: Candidates enriched with topic-specific searches (news, GDELT volume,
    #: Trends interest, web, YouTube). Only enriched topics can be
    #: selected.
    research_enrich_pool_size: int = 15
    #: Trend-ranked topics handed to the evidence check. Raised to the target
    #: count when lower, so the check always has enough to choose from.
    research_evidence_pool_size: int = 8
    research_trend_window_days: int = 7
    research_scientific_window_days: int = 365
    #: Trend score weights, integers summing to 100 (checked below). A
    #: dimension with no data is dropped and the rest renormalised — never
    #: filled in.
    research_weight_momentum: int = 30
    research_weight_coverage: int = 25
    research_weight_audience: int = 20
    research_weight_freshness: int = 15
    research_weight_editorial_fit: int = 10
    #: A topic whose measured share of the trend score is below this gets no
    #: score at all ("insufficient data"), never a score built on what is left.
    research_min_data_coverage: float = 0.6
    #: The lowest trend score a topic may be selected with.
    research_min_trend_score: float = 0.0
    #: Distinct outlets (deduplicated stories, syndication collapsed) a topic
    #: needs this window before it can be selected: one outlet is not a trend.
    #: Two is the least that is cross-source. Three emptied the first live
    #: run (2026-10-12) with Google News off and GDELT refusing requests, the
    #: two sources that supply most outlets.
    research_min_independent_sources: int = 2
    #: Outlets a headline phrase needs before it is *considered*. One: measured
    #: 2026-10-12, a week of 356 feed stories from 12+ outlets had no specific
    #: wellness term in two outlets' headlines, so requiring two at discovery
    #: found nothing. The cross-source rule is enforced at selection instead
    #: (`research_min_independent_sources`, after enrichment searches each
    #: source for the topic), where it measures coverage rather than wording.
    research_phrase_min_publishers: int = 1
    #: Selected topics per category, for a varied desk. Ignored when a run asks
    #: for one category.
    research_max_per_category: int = 2
    #: A topic proposed this recently is not proposed again unless its coverage
    #: has doubled or it is accelerating.
    research_repeat_cooloff_days: int = 28
    #: Topic observations (trend history) are kept this long. At least the
    #: novelty lookback, the cooloff and the status lookback (checked below).
    research_history_retention_days: int = 180
    #: Optional JSON file replacing the default RSS feeds
    #: (research/trends/feeds.py documents the shape). Empty: the defaults.
    research_sources_file: str = ""
    #: The weakest evidence a selected topic may rest on:
    #: none < limited < emerging < moderate < strong.
    research_min_evidence_status: str = "emerging"
    #: Cosine similarity at which two trend queries are one topic.
    research_cluster_threshold: float = 0.88
    #: Cosine similarity at which a candidate repeats a recent article or an
    #: earlier decision and is discarded rather than re-proposed.
    research_duplicate_threshold: float = 0.92
    #: How far back "recent" reaches for that check.
    research_novelty_lookback_days: int = 180
    #: A stable, mid-popularity term put in every Google Trends comparison so
    #: interest levels from different 5-term requests share one scale.
    research_trends_anchor: str = "vitamin d"
    #: Seconds between Google Trends requests. It 429s readily; be patient.
    research_trends_request_interval_seconds: float = 4.0
    #: Rising queries whose own related queries are also fetched. 0 since the
    #: news and feed sources supply breadth; each one is a 4 s+ Trends call.
    research_expand_top: int = 0
    research_geo: str = "US"
    research_language: str = "en"
    #: Domains counted as authoritative among a topic's web results (shown to
    #: the reviewer). A suffix match, so ".gov" covers every US government site.
    research_authoritative_domains: CsvList = [
        ".gov",
        ".edu",
        "nih.gov",
        "who.int",
        "nhs.uk",
        "cdc.gov",
        "mayoclinic.org",
        "clevelandclinic.org",
        "hopkinsmedicine.org",
        "health.harvard.edu",
        "examine.com",
        "cochrane.org",
        "bmj.com",
        "thelancet.com",
        "jamanetwork.com",
        "nature.com",
        "sciencedirect.com",
        "europepmc.org",
    ]
    #: newsapi.org key. Empty means the keyless DuckDuckGo news fallback.
    news_api_key: str = ""
    #: Off means DuckDuckGo news even when a key is set — a switch, so pausing
    #: News API (its free plan is 100 requests a day) does not mean deleting
    #: the key from `.env`.
    news_api_enabled: bool = True
    #: Brave Search API subscription token. Set, web search uses Brave's
    #: documented API; empty, it falls back to keyless DuckDuckGo (ddgs).
    brave_search_api_key: str = ""
    #: YouTube Data API v3 key. Empty skips the YouTube source.
    youtube_api_key: str = ""
    #: `search.list` calls a run may make (100 quota units each, of a default
    #: 10,000 a day). Statistics lookups cost 1 unit and are not counted here.
    youtube_max_searches_per_run: int = 10
    #: Candidates nobody promoted or dismissed are deleted this long after their
    #: run finished. Novelty reads only decided candidates, so undecided ones
    #: from an old run are never read again — and a run writes ~45 of them.
    research_retention_days: int = 60
    #: Shared secret for /api/automation/* (n8n). Empty disables that router
    #: entirely — it answers 404 — so an unconfigured deployment exposes nothing.
    research_trigger_token: str = ""

    # --- article media ---
    #: Per-file ceiling. Generous for a photo, small enough that a stray upload
    #: cannot bloat the database or the request buffer. The reviewer sees the
    #: limit in the error, so raising it is a config change, not a code one.
    #:
    #: There is no `media_root` any more: image bytes live in `media_objects`,
    #: so the store needs a session rather than a path and there is nothing
    #: left to configure. See the `media_objects` section of
    #: migrations/0001_initial.sql.
    media_max_bytes: int = 8 * 1024 * 1024

    # --- article imagery (generated) ---
    #: 'huggingface' | 'none'. Set to 'none' to draft text-only articles
    #: without touching the provider at all.
    image_provider: str = "huggingface"
    #: The Hugging Face token, from IMAGE_GEN_KEY. It needs the "Make calls to
    #: Inference Providers" permission — a plain read token authenticates and
    #: then 403s on the first render. **An empty key is not a startup error**:
    #: ``imagery/factory.py`` returns no client and drafts come out text-only,
    #: because a picture is decorative and the API must still boot for review,
    #: publishing and the public feed.
    image_gen_key: str = ""
    image_model: str = "black-forest-labs/FLUX.1-schnell"
    #: Which inference provider serves the model. 'auto' picks the fastest
    #: available and fails over; name one (nscale, fal-ai, replicate, together)
    #: for consistent latency and billing.
    image_inference_provider: str = "auto"
    #: Generous: two renders run back to back and a cold provider can take a
    #: while to answer the first one.
    image_timeout_seconds: float = 120.0
    #: FLUX.1-schnell is distilled for 1-4 steps at guidance 0.0 — these are the
    #: model card's numbers, not tuning knobs. Raising steps costs credits and
    #: buys nothing on a *schnell* checkpoint.
    image_steps: int = 4
    image_guidance_scale: float = 0.0
    #: The article's lead image: landscape, for a 760px prose column.
    image_lead_width: int = 1216
    image_lead_height: int = 832
    #: The feed tile's cover: portrait. 3:4 on purpose — ``tileRatio()`` in
    #: ``frontend/src/features/feed/ArticleCard.tsx`` hashes a slug into 3/4,
    #: 1/1, 4/5 or 2/3, and 3:4 sits nearest the middle of that spread, so
    #: ``object-cover`` takes a modest centre crop on every tile rather than a
    #: severe one on half of them.
    image_cover_width: int = 864
    image_cover_height: int = 1152
    #: 'crop' | 'render'. 'crop' renders the lead once and centre-crops the
    #: cover out of it (to the cover's aspect ratio): one billed render per
    #: article instead of two, and the two frames are provably one photograph.
    #: 'render' draws the cover as a second generation at the cover size.
    image_cover_mode: str = "crop"

    @field_validator(
        "cors_origins",
        "enabled_providers",
        "discovery_seeds",
        "research_authoritative_domains",
        "research_enabled_sources",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow comma-separated env values as well as JSON lists.

        These fields are ``CsvList``, so pydantic-settings hands the raw string
        over undecoded and both forms have to be handled here — including the
        JSON one, which nothing upstream will parse any more.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"not valid JSON: {value!r}") from exc
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator(
        "image_lead_width",
        "image_lead_height",
        "image_cover_width",
        "image_cover_height",
    )
    @classmethod
    def _dimension_is_a_multiple_of_16(cls, value: int, info: ValidationInfo) -> int:
        """Diffusion latents are 1/8 scale over a patch grid; 16 is the safe step.

        Providers disagree about what to do with a size that does not divide:
        some 400, some silently round and return an image that is not the shape
        that was asked for — which reaches the feed as a tile cropped wrong,
        with nothing anywhere saying why. Refusing at startup names the number.
        """
        if value <= 0 or value % 16:
            raise ValueError(
                f"{info.field_name} must be a positive multiple of 16, got {value}"
            )
        return value

    @model_validator(mode="after")
    def _heartbeat_leaves_room_to_miss_one(self) -> Settings:
        """The staleness window must survive a missed beat or two.

        Set too close together, one slow UPDATE — a checkpoint, a brief lock
        wait — declares a live run abandoned, and the sweep requeues work that
        is still executing. Three intervals means it takes three consecutive
        misses, which is a dead process rather than a hiccup.
        """
        floor = self.worker_heartbeat_seconds * 3
        if self.worker_stale_after_seconds < floor:
            raise ValueError(
                f"worker_stale_after_seconds ({self.worker_stale_after_seconds}) must be "
                f"at least 3x worker_heartbeat_seconds ({self.worker_heartbeat_seconds}) "
                f"= {floor}s, or a single missed heartbeat requeues a live run."
            )
        return self

    @model_validator(mode="after")
    def _overlap_covers_a_missed_scan(self) -> Settings:
        """The overlap must be at least a whole window.

        A missed cron slot leaves a hole in the baseline, and the hole is worse
        than the missing scan: a descriptor with one empty bucket has a lower
        mean, so the next real window reads as a surge. The scan then proposes a
        false trend produced by an outage, with nothing anywhere saying so.

        An overlap of at least one window means the next scan re-reads
        everything the missed one would have, and re-reading costs only API
        calls — observations are keyed (descriptor, pmid) and conflict to a
        no-op.
        """
        if self.discovery_overlap_days < self.discovery_window_days:
            raise ValueError(
                f"discovery_overlap_days ({self.discovery_overlap_days}) must be at "
                f"least discovery_window_days ({self.discovery_window_days}), or a "
                "single missed scan leaves a permanent hole in the baseline that "
                "reads as a surge."
            )
        return self

    @model_validator(mode="after")
    def _research_settings_are_consistent(self) -> Settings:
        """Refuse a research configuration that cannot mean what it says.

        Trend weights not summing to 100 would make the score a number on no
        particular scale; a min above the max would silently shrink every run;
        an unknown source name in the enabled list would be silently ignored.
        """
        weights = (
            self.research_weight_momentum,
            self.research_weight_coverage,
            self.research_weight_audience,
            self.research_weight_freshness,
            self.research_weight_editorial_fit,
        )
        if any(w < 0 for w in weights) or sum(weights) != 100:
            raise ValueError(
                f"research_weight_* must be non-negative integers summing to 100, "
                f"got {sum(weights)}"
            )
        if not 0.0 <= self.research_min_data_coverage <= 1.0:
            raise ValueError("research_min_data_coverage must be between 0 and 1")
        unknown = sorted(set(self.research_enabled_sources) - set(_TREND_SOURCES))
        if unknown:
            raise ValueError(
                f"research_enabled_sources has unknown sources {unknown}; "
                f"known: {', '.join(_TREND_SOURCES)}"
            )
        if self.research_enrich_pool_size < 1 or self.research_evidence_pool_size < 1:
            raise ValueError("research enrich and evidence pools must hold at least 1 topic")
        if not (
            1
            <= self.research_min_article_count
            <= self.research_target_article_count
            <= self.research_max_article_count
        ):
            raise ValueError(
                "research article counts must satisfy 1 <= min <= target <= max, got "
                f"{self.research_min_article_count} / {self.research_target_article_count}"
                f" / {self.research_max_article_count}"
            )
        if self.research_min_evidence_status not in _EVIDENCE_STATUSES:
            raise ValueError(
                f"research_min_evidence_status must be one of {_EVIDENCE_STATUSES}, "
                f"got {self.research_min_evidence_status!r}"
            )
        if self.research_trigger_token and len(self.research_trigger_token) < 32:
            raise ValueError(
                "research_trigger_token must be at least 32 characters; it is the "
                "only thing guarding /api/automation/*"
            )
        if self.image_cover_mode not in ("crop", "render"):
            raise ValueError(
                f"image_cover_mode must be 'crop' or 'render', got {self.image_cover_mode!r}"
            )
        return self

    @model_validator(mode="after")
    def _retention_covers_what_is_read(self) -> Settings:
        """Pruning must never delete a row the scorer or the angle lookup reads.

        Too short, and the baseline silently loses its oldest buckets — every
        substance's mean drops, and the next window reads as a surge.
        """
        needed = max(
            self.discovery_window_days * (self.discovery_baseline_windows + 1),
            self.discovery_angle_lookback_days,
        )
        if self.discovery_observation_retention_days < needed:
            raise ValueError(
                f"discovery_observation_retention_days "
                f"({self.discovery_observation_retention_days}) must be at least {needed}: "
                "the baseline windows and the angle lookback read that far back"
            )
        if self.research_retention_days < 1:
            raise ValueError("research_retention_days must be at least 1")
        # Trend history feeds the novelty-free parts of the score (newness,
        # the history momentum signal), repeat suppression and trend status.
        # Pruning inside any of their lookbacks would make a topic look new
        # again, or a repeat look fresh.
        history_needed = max(
            self.research_repeat_cooloff_days,
            self.research_novelty_lookback_days,
            _TREND_STATUS_LOOKBACK_DAYS,
        )
        if self.research_history_retention_days < history_needed:
            raise ValueError(
                f"research_history_retention_days ({self.research_history_retention_days}) "
                f"must be at least {history_needed}: repeat suppression, novelty and "
                "trend status read that far back"
            )
        return self

    def validate_embedding_dim(self, provider_dimension: int) -> None:
        """Fail fast when the configured width disagrees with the provider.

        Raises:
            RuntimeError: naming both numbers, because the fix depends on which
                one is wrong — re-embedding the cache or editing the migration.
        """
        if provider_dimension != self.embedding_dim:
            raise RuntimeError(
                f"embedding_dim mismatch: settings say {self.embedding_dim}, "
                f"provider '{self.embedding_provider}' emits {provider_dimension}. "
                "The vector(N) column must match the provider; changing it "
                "requires a migration and a full re-embed of `sources`."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the environment is read once per process."""
    return Settings()
