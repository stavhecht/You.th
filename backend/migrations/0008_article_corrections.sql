-- 0008: withdrawing and correcting a published article.
--
-- Withdrawal records who, when and why, like a rejection does.
--
-- A correction is typed into `correction_draft`, never into `edited_content`:
-- the feed serves `edited_content` live, so an autosave there would publish a
-- half-typed sentence. Publishing the correction moves the draft across and
-- appends {at, by, note} to `corrections`, which the article page shows.
--
-- `original_meta` keeps the model's own headline, summary, verdict and
-- qualifier the first time a reviewer changes one of them, so "what the model
-- wrote" stays answerable for those four fields as `original_content` keeps it
-- for the body. NULL means never edited.

BEGIN;

ALTER TABLE articles
    ADD COLUMN withdrawal_reason TEXT,
    ADD COLUMN withdrawn_at      TIMESTAMPTZ,
    ADD COLUMN withdrawn_by      UUID REFERENCES users (id) ON DELETE SET NULL,
    ADD COLUMN correction_draft  JSONB,
    ADD COLUMN corrections       JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN original_meta     JSONB,
    ADD CONSTRAINT articles_withdrawn_requires_reason CHECK (
        status <> 'withdrawn' OR (withdrawal_reason IS NOT NULL AND withdrawn_at IS NOT NULL)
    );

COMMIT;
