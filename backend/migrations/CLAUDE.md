# Migrations

Schema history and the squash rule. Loaded when you work under this directory; the root
[CLAUDE.md](../../CLAUDE.md) (commands, invariants, map) still applies.

Files: `0001_initial.sql`, `0002_social_products.sql`, `0003_social_topic_question.sql`,
`0004_drop_social_post_write_only_columns.sql`, `0005_product_confirmation_checked_at.sql`,
`0006_drop_collection_write_only_columns.sql`, `0007_article_status_withdrawn.sql`,
`0008_article_corrections.sql`, `0009_collection_kinds.sql`, `0010_reader_email_verified.sql`.
**Next is 0011.** (2026-11-12: a two-step sign-in column was added and withdrawn before the
branch was ever pushed; both files were deleted and `0011` renumbered to `0010`, with
`schema_migrations` in the dev and test databases edited to match. Do that only before a
migration has left your machine; after, write a new migration.)
**A new enum value needs a file of its own with no `BEGIN`** (0007): Postgres refuses to use
a value in the transaction that added it, and 0008's CHECK names `withdrawn`. Apply with
`python -m scripts.migrate` (asyncpg, no psql needed); `--status` shows what is applied. After a
new migration, also apply it to `youth_test` (see [backend/tests/CLAUDE.md](../tests/CLAUDE.md)).

## Squash history

0001 was squashed a sixth time on 2026-11-03, folding in 0002-0008: editorial subject
categories, research categories, `article_sources.stance`, the trend search's tables, public
search (tsvector + pg_trgm), and 7 more write-only columns dropped. The dev and test databases
kept their data: `schema_migrations` was collapsed to the new checksum after a schema-only diff
against a database built from the file showed no change. (The fifth squash, 2026-09-30, COPIED
the data into a fresh database; its backup, `evidwell_backup_20260930`, was dropped on
2026-10-14.)

**Squashing is only free while every database holding the schema can be rebuilt or have its
history rewritten**; the file's header states that condition.
