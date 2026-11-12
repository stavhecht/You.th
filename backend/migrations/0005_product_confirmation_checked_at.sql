-- 0005: when a product was last looked up for an official page or FDA record.
--
-- Lookups are capped per run, and nothing recorded who had been tried, so the
-- same few products were asked about every week. NULL means never looked up.

BEGIN;

ALTER TABLE products ADD COLUMN confirmation_checked_at TIMESTAMPTZ;

COMMIT;
