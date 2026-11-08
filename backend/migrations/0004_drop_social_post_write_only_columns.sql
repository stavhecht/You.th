-- 0004: four social_posts columns that were written and never read.
--
-- The reader's cache is keyed by its own text_key, not by content_hash, so a
-- changed title was never re-read because of that column.

BEGIN;

ALTER TABLE social_posts
    DROP COLUMN collected_via,
    DROP COLUMN content_hash,
    DROP COLUMN discovered_query,
    DROP COLUMN first_collected_at;

COMMIT;
