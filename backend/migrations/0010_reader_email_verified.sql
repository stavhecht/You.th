-- 0010: a reader's email address, once they have proved it is theirs.
--
-- Signup never checked that. Anyone could open an account in someone else's
-- name, switch the newsletter on for them, and send topic suggestions that
-- reached the editors "from" that address. NULL means not confirmed; existing
-- accounts start there, because none of them ever was.

BEGIN;

ALTER TABLE readers ADD COLUMN email_verified_at TIMESTAMPTZ;

COMMIT;
