-- The Trend Search Agent: topic identity and history, feed state, run summary.
--
-- The research agent (app/research) was split on 2026-10-12. Its first half,
-- the Trend Search Agent (app/research/trends), collects from publisher RSS
-- feeds, GDELT, Google Trends, news, web search and (keyed) YouTube and Reddit,
-- groups copies into stories, clusters topics and scores them on momentum,
-- cross-source coverage, audience, freshness and editorial fit — with no
-- scientific evidence in the score. Its second half, the evidence check, asks
-- PubMed and Europe PMC about the best of those only. DESIGN.md §4b.
--
-- What this adds:
--
-- research_topics — a topic's identity across runs. A run's candidates are new
--   rows every week; this is what says "this week's Creatine for women is the
--   topic seen three weeks ago". Keyed by subject|outcome, normalised. There
--   are deliberately no incremented counters here (times seen, times
--   selected): those are COUNTs over the observations, which a retried write
--   cannot drift. `merged_into` lets an operator fold one topic into another
--   (the store follows it one hop); splitting is just a new key.
--
-- research_topic_observations — one row per topic per run: the trend history.
--   Written once, when a run finishes and only if its trend phase completed
--   (partial metrics would read as "declining" the following week), as an
--   upsert on (topic_id, research_run_id), so finishing twice is a no-op.
--   Pruned after `research_history_retention_days` (180), which a settings
--   validator keeps above everything that reads history.
--
-- research_feed_state — per publisher feed: the ETag / Last-Modified the next
--   fetch sends, when it was last fetched and last succeeded, its last error,
--   and the last parsed items. The items are what a 304 Not Modified replays:
--   without them "unchanged" would read as "this feed has nothing".
--
-- research_runs.summary — the trend agent's output, frozen when its phase
--   ends, plus the run's observability counts. The /result endpoint serves it.
--
-- research_candidates.topic_ref_id — the candidate's persistent topic.
--
-- research_runs_one_active — single-flight as a constraint. start_research_run
--   checked for an active run and then inserted, so two simultaneous triggers
--   (the weekly slot and a desk press) could both pass the check.

BEGIN;

CREATE TABLE research_topics (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_key     TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    subject       TEXT NOT NULL,
    outcome       TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    merged_into   UUID REFERENCES research_topics(id) ON DELETE SET NULL,
    CONSTRAINT research_topics_not_merged_into_itself CHECK (merged_into IS NULL OR merged_into <> id)
);

-- Matching drifted wording reads the topics seen recently.
CREATE INDEX research_topics_recent_idx ON research_topics (last_seen_at DESC);

CREATE TABLE research_topic_observations (
    topic_id               UUID NOT NULL REFERENCES research_topics(id) ON DELETE CASCADE,
    research_run_id        UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    observed_at            TIMESTAMPTZ NOT NULL,
    window_start           TIMESTAMPTZ NOT NULL,
    window_end             TIMESTAMPTZ NOT NULL,
    stories                INTEGER NOT NULL,
    independent_publishers INTEGER NOT NULL,
    trend_score            DOUBLE PRECISION,
    data_coverage          DOUBLE PRECISION NOT NULL,
    trend_status           TEXT,
    scoring_version        TEXT NOT NULL,
    proposed               BOOLEAN NOT NULL DEFAULT false,
    metrics                JSONB NOT NULL,
    scores                 JSONB NOT NULL,
    PRIMARY KEY (topic_id, research_run_id),
    CONSTRAINT research_topic_observations_window CHECK (window_start <= window_end)
);

-- A topic's history, newest first; and the retention sweep.
CREATE INDEX research_topic_observations_topic_idx
    ON research_topic_observations (topic_id, observed_at DESC);
CREATE INDEX research_topic_observations_observed_idx
    ON research_topic_observations (observed_at);

ALTER TABLE research_candidates
    ADD COLUMN topic_ref_id UUID REFERENCES research_topics(id) ON DELETE SET NULL;
CREATE INDEX research_candidates_topic_idx ON research_candidates (topic_ref_id)
    WHERE topic_ref_id IS NOT NULL;

CREATE TABLE research_feed_state (
    feed_url             TEXT PRIMARY KEY,
    etag                 TEXT,
    last_modified        TEXT,
    last_fetched_at      TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    last_status          INTEGER,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    items                JSONB
);

ALTER TABLE research_runs ADD COLUMN summary JSONB;

CREATE UNIQUE INDEX research_runs_one_active ON research_runs ((true))
    WHERE status IN ('queued', 'running');

COMMIT;
