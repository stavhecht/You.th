-- 0002: social listening, product discovery, and the collection-run queue.
--
-- Two discovery modules beside the Trend Search Agent and the MeSH scan, each
-- with its own tables. Nothing here alters a research_* or discovery_* table,
-- and nothing reads into their scores: relationships between the four systems
-- are computed at read time from keys they already store (app/intel/related.py),
-- so no link table can go stale or outlive what it links.
--
-- OBSERVATION / CANDIDATE / ASSESSMENT stay apart:
--   observations: social_post_observations, product_observations (append-only)
--   candidates:   social_candidates, products
--   assessments:  social_candidates.momentum, social_candidates.evidence
--
-- RETENTION. YouTube's developer policies require stored API data to be
-- refreshed or deleted within 30 days. Raw posts not refreshed for
-- `social_retention_days` (validator: at most 30) are deleted, and everything
-- hanging off them cascades: observations, topic links, product observations.

BEGIN;

-- The worker's queue for collection work: daily social collection, the weekly
-- social analysis, and weekly product discovery. One active row per kind, the
-- same single-flight-as-a-constraint shape as research_runs_one_active.
-- `collection_status` is per platform/source: {"youtube": {"status": "SUCCESS",
-- "calls": 17, "items": 412, "detail": null, "failures": [...]}}. The status
-- vocabulary is SUCCESS | EMPTY | UNAVAILABLE | RATE_LIMITED | ERROR |
-- NOT_CONFIGURED, and only SUCCESS and EMPTY count as a day of coverage.
CREATE TABLE collection_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label             TEXT NOT NULL UNIQUE,
    kind              TEXT NOT NULL
        CHECK (kind IN ('social_daily', 'social_weekly', 'product_discovery')),
    status            TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    collection_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    stats             JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes             JSONB NOT NULL DEFAULT '[]'::jsonb,
    error             JSONB,
    requested_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    heartbeat_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);

CREATE INDEX collection_runs_claimable_idx ON collection_runs (created_at)
    WHERE status = 'queued';
CREATE UNIQUE INDEX collection_runs_one_active_per_kind ON collection_runs (kind)
    WHERE status IN ('queued', 'running');
-- The scheduler's due-ness check and the analytics' coverage read.
CREATE INDEX collection_runs_finished_idx ON collection_runs (kind, finished_at DESC)
    WHERE status = 'completed';

-- One video or post. Identity is the platform's own id. Creator information is
-- two columns rather than a table: an independent-creator count is a COUNT
-- DISTINCT over them, and nothing else about a creator is stored.
-- `collected_via` names the access path (`youtube_api`); there is no scraping.
CREATE TABLE social_posts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform            TEXT NOT NULL,
    platform_post_id    TEXT NOT NULL,
    collected_via       TEXT NOT NULL,
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    description         TEXT,
    creator_platform_id TEXT,
    creator_name        TEXT,
    published_at        TIMESTAMPTZ,
    -- sha256 of the text the reader model sees; a changed title re-reads it.
    content_hash        TEXT NOT NULL,
    discovered_query    TEXT,
    first_collected_at  TIMESTAMPTZ NOT NULL,
    last_collected_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT social_posts_identity UNIQUE (platform, platform_post_id)
);

CREATE INDEX social_posts_published_idx ON social_posts (platform, published_at DESC);
CREATE INDEX social_posts_retention_idx ON social_posts (last_collected_at);

-- One reading of a post's public counters. Append-only: a later reading never
-- overwrites an earlier one, which is what makes a view velocity computable.
-- NULL is "the platform did not say" (hidden likes, disabled comments, no
-- share count on YouTube), never zero.
CREATE TABLE social_post_observations (
    post_id           UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    collection_run_id UUID NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    observed_at       TIMESTAMPTZ NOT NULL,
    views             BIGINT,
    likes             BIGINT,
    comments          BIGINT,
    shares            BIGINT,
    PRIMARY KEY (post_id, collection_run_id)
);

CREATE INDEX social_post_observations_post_idx
    ON social_post_observations (post_id, observed_at DESC);
CREATE INDEX social_post_observations_observed_idx
    ON social_post_observations (observed_at);

-- A social topic's identity: the same topic_key() the trend search uses, so a
-- social topic and a trend topic about the same thing share a key exactly.
-- Separate from research_topics on purpose: a row there feeds the trend
-- search's history and novelty, and social activity must not move its scores.
CREATE TABLE social_topics (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_key     TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    subject       TEXT NOT NULL,
    outcome       TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE social_post_topics (
    post_id  UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    topic_id UUID NOT NULL REFERENCES social_topics(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, topic_id)
);

CREATE INDEX social_post_topics_topic_idx ON social_post_topics (topic_id);

-- What the reader model said about a text, keyed by sha256(prompt version +
-- text). The cache that keeps unchanged posts and feed items from being read
-- twice. Pruned with the posts' retention, since it quotes their text.
CREATE TABLE text_readings (
    text_key TEXT PRIMARY KEY,
    reading  JSONB NOT NULL,
    read_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX text_readings_read_idx ON text_readings (read_at);

-- One weekly proposal of a social topic. `metrics` are the deterministic
-- numbers (social/analytics.py); `momentum` and `evidence` are assessments,
-- kept apart from them and from each other. Popularity never feeds `evidence`.
CREATE TABLE social_candidates (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_run_id UUID NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    topic_id          UUID NOT NULL REFERENCES social_topics(id) ON DELETE CASCADE,
    rank              INTEGER NOT NULL,
    metrics           JSONB NOT NULL,
    momentum          JSONB NOT NULL,
    evidence          JSONB,
    status            TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'promoted', 'dismissed')),
    pipeline_run_id   UUID REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    decided_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    decided_at        TIMESTAMPTZ,
    dismiss_reason    TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT social_candidates_once_per_run UNIQUE (collection_run_id, topic_id),
    -- The same three guarantees research_candidates and discovery_candidates carry.
    CONSTRAINT social_candidates_promoted_has_run CHECK (
        status <> 'promoted' OR pipeline_run_id IS NOT NULL
    ),
    CONSTRAINT social_candidates_decided_has_reviewer CHECK (
        status = 'proposed' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)
    ),
    CONSTRAINT social_candidates_dismissed_has_reason CHECK (
        status <> 'dismissed' OR dismiss_reason IS NOT NULL
    )
);

CREATE INDEX social_candidates_topic_idx ON social_candidates (topic_id, created_at DESC);

-- A product, deduplicated across sources by normalised brand|name.
-- `status` climbs uncertain -> corroborated (2+ independent sources) ->
-- confirmed (official page found, or an FDA record), and never falls.
-- `launch_status` is separate: a mention is not a launch.
CREATE TABLE products (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_key   TEXT NOT NULL UNIQUE,
    brand         TEXT NOT NULL,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'uncertain'
        CHECK (status IN ('uncertain', 'corroborated', 'confirmed')),
    launch_status TEXT NOT NULL DEFAULT 'none'
        CHECK (launch_status IN ('none', 'claimed', 'corroborated', 'confirmed')),
    official_url  TEXT,
    fda_reference JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One source naming one product. A mention from a social post cascades away
-- with the post (retention); feed and news mentions stay.
CREATE TABLE product_observations (
    id                BIGSERIAL PRIMARY KEY,
    product_id        UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    collection_run_id UUID REFERENCES collection_runs(id) ON DELETE SET NULL,
    social_post_id    UUID REFERENCES social_posts(id) ON DELETE CASCADE,
    source_kind       TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    publisher         TEXT,
    published_at      TIMESTAMPTZ,
    observed_at       TIMESTAMPTZ NOT NULL,
    mention_kind      TEXT NOT NULL,
    quote             TEXT NOT NULL,
    CONSTRAINT product_observations_once_per_source UNIQUE (product_id, source_url)
);

CREATE INDEX product_observations_product_idx
    ON product_observations (product_id, observed_at DESC);
CREATE INDEX product_observations_post_idx ON product_observations (social_post_id)
    WHERE social_post_id IS NOT NULL;

COMMIT;

-- A promoted social topic becomes a pipeline run like any other, and the
-- token ledger can tell what social listening cost with one WHERE. Outside the
-- transaction: a value added inside one cannot be used until it commits.
ALTER TYPE run_origin ADD VALUE IF NOT EXISTS 'social';
