-- Public search over published articles.
--
-- Until this migration the header's search box matched only the cards already
-- loaded into the browser — headline, excerpt and qualifier — because a
-- `LIKE` over card columns would have been a search that quietly missed the
-- body of every article. This is the search that does not miss it.
--
-- Two mechanisms, tried in order by `services/feed.py::FeedService.search`:
--
-- 1. Full-text search over `search_document`, a weighted tsvector. English
--    stemming ("sleeping" finds "sleep"), `websearch_to_tsquery` so a search
--    box's quotes, `or` and `-word` work and malformed input never raises, and
--    `ts_rank_cd` for order. Weights:
--      A  headline, product, ingredients — what an article is *about*
--      B  verdict qualifier, summary, claims, card excerpt — what it says
--      C  the approved body's text
--    Only what a reader is shown is indexed. `topic` is the generation input
--    (sometimes the user's raw question) and is not on the page; cited
--    sources' titles are not either, so "creatine kidneys" finds the article
--    about it rather than every article that cites a kidney paper.
--
-- 2. When full text finds nothing, a trigram fallback over headline + product
--    for typos ("magnesum", "ashwaganda"). `word_similarity` via `<%`, not
--    `similarity` via `%`: measured 2026-10-14 against real headlines, whole-
--    string similarity scored "creatin" against "Creatine increases muscle
--    strength creatine" at 0.21, under any usable threshold, while word
--    similarity scored it 0.88. The default `pg_trgm.word_similarity_threshold`
--    (0.6) is used as is; at it "vitamn d" (0.58) is missed and "stres" matches
--    "strength" (0.67), which is why the API labels these results as close
--    matches rather than as results.
--
-- Measured on synthetic data (2026-10-14, ~940-word bodies): ranked full-text
-- search costs ~3.5 µs per *matching* article on top of a ~0.3 ms index probe,
-- so a word in 2,630 of 80,000 published articles took 9 ms and a rare one
-- 0.07–0.3 ms. The fallback added 0.3–2 ms.
--
-- The document is built by ONE function, `article_search_document`, called
-- here for the backfill and by `ReviewService.approve()` at publish time — so
-- the backfilled rows and the newly published ones cannot be indexed two
-- different ways. It reads COALESCE(edited_content, original_content), the
-- same rule as the card (invariant #4): what the reviewer approved.
--
-- Body text comes from the TipTap tree's `text` nodes (`$.**.text`). Citation
-- nodes carry no `text`, so `[S1]` never reaches the index; image and video
-- nodes carry only `attrs`, so their URLs do not either. A `jsonb_to_tsvector`
-- over the whole document was the obvious alternative and is wrong: it indexes
-- every string value, so "paragraph", "citation" and every media path would
-- become searchable words matching every article.
--
-- Both indexes are partial on `status = 'published'`, like articles_feed_idx:
-- drafts are never searched, and `_published_only` in the service is still the
-- audit point for that rule — the index predicate is an optimisation, not the
-- guard.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE articles ADD COLUMN search_document tsvector;

CREATE FUNCTION article_search_document(a articles) RETURNS tsvector
LANGUAGE sql STABLE AS $$
    SELECT
        setweight(to_tsvector('english', concat_ws(' ',
            a.card_headline, a.product, array_to_string(a.ingredients, ' '))), 'A')
     || setweight(to_tsvector('english', concat_ws(' ',
            a.verdict_qualifier, a.summary, array_to_string(a.target_claims, ' '),
            a.card_excerpt)), 'B')
     || setweight(to_tsvector('english', coalesce((
            SELECT string_agg(node #>> '{}', ' ')
            FROM jsonb_path_query(
                coalesce(a.edited_content, a.original_content), 'strict $.**.text'
            ) AS node
        ), '')), 'C')
$$;

UPDATE articles a
SET search_document = article_search_document(a)
WHERE status = 'published';

CREATE INDEX articles_search_idx
    ON articles USING GIN (search_document)
    WHERE status = 'published';

-- The expression must match `services/feed.py::_TYPO_TITLE` character for
-- character, or the planner will not use this index.
CREATE INDEX articles_title_trgm_idx
    ON articles USING GIN ((card_headline || ' ' || product) gin_trgm_ops)
    WHERE status = 'published';
