-- 0007: a published article can be withdrawn.
--
-- Until now `published` was terminal: a wrong article could only be taken down
-- with SQL. A file of its own, with no BEGIN, because Postgres refuses to use
-- a new enum value in the transaction that added it, and 0008's CHECK uses it.

ALTER TYPE article_status ADD VALUE IF NOT EXISTS 'withdrawn';
