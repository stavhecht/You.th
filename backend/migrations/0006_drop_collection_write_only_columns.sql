-- 0006: five collection columns that were written and never read.

BEGIN;

ALTER TABLE collection_runs
    DROP COLUMN attempts,
    DROP COLUMN requested_by;

ALTER TABLE social_post_observations DROP COLUMN shares;

ALTER TABLE product_observations DROP COLUMN collection_run_id;

ALTER TABLE social_topics DROP COLUMN first_seen_at;

COMMIT;
