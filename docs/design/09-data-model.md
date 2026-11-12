# 9. Data model

Full DDL: `backend/migrations/0001_initial.sql`. Shape:

- **`users`** — reviewers. `role` in (`admin`, `reviewer`).
- **`articles`** — `status`, `slug`, `topic`, `original_content` (JSONB, immutable),
  `edited_content` (JSONB), `verdict`, `verdict_qualifier`, `evidence_grade`, derived card
  fields, `subject`, `validation_report` (JSONB), `reviewed_by`, `reviewed_at`,
  `published_at`.
- **`readers`** — public accounts. `email`, `password_hash` (same Argon2id hasher as `users`),
  `display_name`, `interests subject[]`, `newsletter`. Separate from `users` for the reason in
  §3.2.
- **`reader_folders`** / **`reader_saves`** — one shelf per folder. See below for the two
  constraints that carry the behaviour.
- **`contact_requests`** — `kind`, `name`, `email`, `link`, `note`, `created_at`. Written
  unauthenticated, emailed to the editorial inbox, read only in the console. It had a
  handling state (`status`, `handled_by`, `handled_at`) until the 2026-11-03 squash: nothing set it.

**Two card columns are derived, and one enum is not.** `card_image` / `card_image_alt` are
materialised at publish time from the first `image` node in the approved body, by the same rule
as `card_headline` and `card_excerpt` — so a feed tile cannot show a picture the article does
not contain. `youtube` nodes are excluded on purpose: a video thumbnail lives on a third-party
host, and deriving one would put a YouTube request on every render of the feed, which is the
tracking the article page's click-to-load facade exists to avoid. NULL is the normal case and
the feed draws a typographic tile.

`subject`, by contrast, is **set by a reviewer and never inferred**. It cannot be derived from
`product`, which is free text, and a guess would put a confident colour on an unchecked
classification. It stays nullable everywhere: an unclassified article renders in ink and sits
under "Everything", which is the design's resting state rather than a gap, so it must never
block publishing.

**Interests reorder the feed; they never filter it.** `FeedService.page` gains a leading sort
tier — 0 for a subject the reader picked, 1 for everything else — and the rest follows below.
Filtering would let an account quietly shrink the world, and it would make every *unclassified*
article permanently invisible to anyone with an interest set. The cost is that the tiered sort
cannot use `articles_feed_idx`, because the leading key is a `CASE` expression; the tier is
therefore skipped entirely for a reader with no interests, which is every anonymous request.
The keyset cursor encodes the tier as well as `(published_at, id)`, so signing in or out
mid-scroll hands back a cursor the other mode can still read. If personalised paging ever
becomes hot the fix is a materialised tier column, not a different sort.
- **`sources`** — `pmid`, `doi`, `title`, `abstract`, `journal`, `year`, `study_type`,
  `citation_count`, `url`, `source_api`, `embedding_model`, `chunk_settings`, `last_seen_at`, plus the
  retraction record: `retracted_at`, `concern_at`, `retraction_checked_at`, `retraction_note`
  (§4 — a NULL `retraction_checked_at` means *nobody successfully asked*, not "clean").
- **`source_chunks`** — `(source_id, ordinal)`, `embedding vector(N)`. One row per
  overlapping chunk of a source's abstract (§4). No text column: ranking reads only the vector,
  and a chunk can be re-cut from the abstract. `sources.embedding_model` and
  `sources.chunk_settings` name the model that embedded them and how they were cut.
- **`article_sources`** — `(article_id, source_id, claim, citation_handle)`. Provenance.
  `excerpts` holds the full-text passages the model was shown for that source,
  NULL for the many sources shown none (§4, Pass 3).
- **`pipeline_runs`** / **`pipeline_stage_runs`** — observability, plus the retry state
  (`attempts`, `next_attempt_at` on the run; `attempt` on each stage row, so a retry adds a
  second set rather than colliding with the first). See §3.3. The stage row also carries the
  token ledger — `model` plus four token counts, priced at read time and never stored as
  cost (§3.7); the run's four counters are the lifetime rollup across attempts.

Indexes that matter:

```sql
CREATE UNIQUE INDEX sources_doi_key  ON sources (lower(doi)) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX sources_pmid_key ON sources (pmid)       WHERE pmid IS NOT NULL;
CREATE INDEX articles_feed_idx ON articles (published_at DESC) WHERE status = 'published';
CREATE INDEX articles_subject_feed_idx
    ON articles (subject, published_at DESC, id DESC) WHERE status = 'published';
CREATE UNIQUE INDEX readers_email_key      ON readers (lower(email));
CREATE UNIQUE INDEX reader_folders_name_key ON reader_folders (reader_id, lower(name));
```

The subject index is a second index rather than a widening of `articles_feed_idx`: the
unfiltered feed is the common path and must not pay for the narrow one.

**Two constraints on `reader_saves` carry behaviour that would otherwise be a handler's
discipline.** The primary key is `(reader_id, article_id)`, so re-saving an article into a
different folder is an `UPDATE` of `folder_id` — a *move*, not a second row — and the same
article cannot accumulate a copy per folder. And the foreign key is composite,
`(folder_id, reader_id) → reader_folders (id, reader_id)`, which makes "you can only save into
your own folder" a database fact rather than a check every route has to remember. Both are
tested against a live database in `test_reader_accounts.py`, because a fake session cannot
demonstrate either.

**There is deliberately no HNSW index on the vectors** — `sources.embedding` then,
`source_chunks.embedding` since chunking arrived, for the same reason. An earlier draft of the schema
created one and nothing could ever use it. The only vector query is
`rank_for_claim`, whose score is cosine + grade + recency, so top-k is applied in Python and no
`LIMIT` reaches SQL; with a restrictive `id = ANY(...)` filter over one run's ~100 candidates,
the planner picks a bitmap scan on `sources_pkey` and an exact quicksort. `EXPLAIN` confirms
it, and the exact plan is *faster* at that size (0.53ms against 1.44ms over 3000 rows) as well
as being exact rather than approximate.

The index was meanwhile the dominant cost of writing to the table designed to grow forever:
**5.26s versus 0.11s to insert 1500 embedded rows, and 27 MB of index** — roughly 48× on write,
for zero reads. `rerank.py` also used to `SET LOCAL hnsw.ef_search` before each query, which
read as tuning that index scan; the statement worked and the plan never consulted it, which is
the general shape of the problem this file keeps returning to — a mechanism that runs cleanly
and affects nothing.

`0001_initial.sql` carries the `CREATE INDEX CONCURRENTLY` statement to bring it back, commented
beside the index it declines to create, along with the
condition that would justify it: a query issuing `ORDER BY embedding <=> $1 LIMIT k` without a
restrictive filter. The v2 passage search and a related-articles feature are the two candidates.
Neither exists.

The partial unique indexes are load-bearing, and they are also why the cache **cannot** be a
single `ON CONFLICT`. A paper's identity spans both of them, Postgres accepts one inference
clause per statement, and routing by whichever identifier the incoming record happens to carry
breaks the moment a record arrives *more complete* than the row it matches: a paper first seen
PMID-only, met again with a DOI, infers against the DOI index, finds nothing, inserts, and
violates `sources_pmid_key`. Union-find makes newly-complete records the normal case, so this
is the common path.

`SourceCache` therefore resolves first — one indexed read matching every identifier at once —
then updates matched rows **by primary key**, against which no conflict target exists, and
inserts only genuinely new papers. The race that reintroduces is two workers inserting the same
new paper between the read and the write; it surfaces as an `IntegrityError` inside a
`SAVEPOINT` and is retried once, by which point the row exists and resolves as an ordinary
match. The savepoint matters: the pipeline session is long-lived, and a bare rollback would
discard the rest of the run.

One paper matching two *different* rows means the cache split it under the old keying. That is
reported and resolved to the DOI row — never repaired inline, because repair means re-pointing
`article_sources` and deleting a row `ON DELETE RESTRICT` exists to protect, and a cache refresh
must not rewrite the provenance of published articles as a side effect of fetching abstracts.

---

## How much is stored, and for how long

Audited 2026-09-30, against the live database. At that point it held 38 MB: 84k ledger rows,
415 cached papers, 15 articles, 3 research runs. After the fifth squash and the retention
rules below it held 21 MB with nothing lost that anything reads.

- **Seven write-only columns were dropped:** `source_chunks.content`,
  `articles.source_blurb` (a copy of the run's), and `discovery_observations.major_topic`,
  `.scan_id` and `.first_seen_at`, and `discovery_descriptors.first_seen_at` and
  `.last_seen_at`. Everything else is read somewhere. The one kept-but-unread column is
  `sources.last_seen_at`, deliberately: it is the key a future cache-eviction rule would use.
- **Growth is bounded where it was unbounded:**

| Table | Growth | Rule |
|---|---|---|
| `discovery_observations` | ~thousands of rows per daily scan | a succeeded scan deletes rows older than `discovery_observation_retention_days` (120); 51% of rows were older than anything reads |
| `research_candidates` | ~45 rows per run | undecided candidates of runs older than `research_retention_days` (60) are deleted; discarded ones are stored without examples |
| `media_objects` | ~70 KB per render; every Regenerate orphans the old frames | `scripts/prune_media.py` (manual, dry by default, 7-day grace) |
| `sources` / `source_chunks` | only with new topics | kept: the cache is what makes a repeat topic cheap |
| `pipeline_stage_runs` | ~8 small rows per run | kept: it is the cost ledger |
