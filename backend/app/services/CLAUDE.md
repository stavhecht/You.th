# Services: accounts, feed, card, media

Accounts, feed ordering, subjects, card derivation, and the media store.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03-frontend-auth-pipeline.md](../../../docs/design/03-frontend-auth-pipeline.md) §3.2 and [03-article-body-and-media.md](../../../docs/design/03-article-body-and-media.md).

## Accounts

**Two accounts, two tables, two tokens.** `users` is the reviewer roster and
`articles.reviewed_by` is a foreign key into it, so a row there is a claim about who is
answerable for a published article. `readers` are self-service. One table with a role column
would put a correctly-set enum between a public signup and the review queue; two tables make
that unrepresentable. The tokens carry a `typ` claim (`reviewer` / `reader`) checked in
`decode_token_subject` **before** the account lookup, so a reader token presented to the
console is rejected as malformed rather than merely failing to resolve — the separation
survives a future change to either query. `lib/api/client.ts` picks which token to send from
the path, never from the caller.

## Feed and subjects

**A reader account orders the feed; it never narrows it.** `FeedService.page` gains a leading
sort tier — 0 for a subject the reader picked, 1 for everything else — and the rest of the
feed follows below. Filtering instead would let an account quietly shrink the world, and it
would make every *unclassified* article (`subject IS NULL`) invisible to anyone with an
interest set. The cost is that the tiered sort cannot use `articles_feed_idx`; the tier is
therefore skipped entirely for a reader with no interests, which is every anonymous request.
Free-text search is `GET /feed?q=`, server-side over every published article's approved text
(`0001_initial.sql`, DESIGN.md §3.1): full text first, a trigram typo fallback only when that finds
nothing, labelled `match: "similar"`. The tsvector is built by the SQL function
`article_search_document`, called by `approve()` and by the migration's backfill — never in
Python, so the two cannot index differently. `_TYPO_TITLE` in `services/feed.py` must match
`articles_title_trgm_idx`'s expression exactly or the index goes unused.

**`subject` is reviewer-set and stays optional.** It is the product's one chromatic axis and
it cannot be derived: `product` is free text, and guessing would put a confident colour on an
unchecked classification. `PATCH /console/articles/{id}/subject` is deliberately separate from
the content autosave and legal *after* publication, because it drives a colour and a browse
listing rather than a word the reader was shown. An unclassified article renders in ink and
sits under "Everything" — the design's resting state, not a broken cell — so it must never
block publishing.

**Subjects are editorial categories, not kinds of object** (2026-10-01): Fitness,
Nutrition, Supplements, Sleep and recovery, Lifestyle, Preventive health, General health,
Wellness, Other. `other` is a reviewer's answer ("none of these fit") and is not `NULL`
(unclassified). They are the drawer's filter, the readers' interests and the tile hue, so a
new category is an enum value in `domain/enums.py`, a migration swapping the Postgres type,
and a hue in both themes of `youth.css` plus `tailwind.config.ts` and `subject.ts`. They no
longer choose the generated picture's props: `imagery/prompt.py` keeps its own `Motif`, and
`_SUBJECT_MOTIFS` maps only Supplements and Nutrition onto it.

## Card picture and the generated cover

**The feed tile's picture is derived, like its headline and excerpt.** `derive_card()` takes
the first `image` node from the approved body into `articles.card_image` at publish time, so a
tile cannot show a picture the article does not contain. `youtube` nodes are excluded on
purpose: a video thumbnail lives on a third-party host, and deriving one would put a YouTube
request on every feed render — the same tracking the article page's click-to-load facade
exists to avoid. `NULL` is the normal case and the feed draws a typographic tile.

**The one picture not literally in the body is the generated cover, and a pairing rule is what
keeps the rule above true.** `ILLUSTRATE` draws two frames from one prompt with one seed — a
landscape `lead`, written into `original_content` as an ordinary image node, and a portrait
`cover` for the feed tile, which lives only in `articles.generated_imagery` (the tile shapes in
`tileRatio()` are all 1:1 or taller, so `object-cover` on a landscape lead throws its sides
away). `derive_card` reaches for the cover **only while `lead.src` is still the document's first
image**. Replace that picture, delete it, or put another above it and the pairing breaks and the
card falls back to ordinary derivation. It fails toward "no cover", never "wrong cover".

Be precise about what the pairing buys, because two stronger claims are **false**. The frames
are *not* one photograph at two aspect ratios: measured 2026-08-24, the same prompt and seed at
two sizes give visibly different compositions, and the provider `auto` resolves to (`nscale`)
ignores the seed entirely, so even two identical requests differ. And they are no longer even
guaranteed to come from one *generation* — `POST /console/articles/{id}/illustration` takes a
`frames` list, so a reviewer can redraw either alone. What the rule still guarantees is the part
worth having: the tile's picture was drawn for this article under the same locked claim-free
prompt builder, and is discarded the moment the article stops carrying its partner. Neither
frame asserts anything, so neither can assert what the other does not — which is exactly why
redrawing one and not the other is safe. If they ever need to be provably the same photograph,
set `IMAGE_COVER_MODE=crop` — **the default since 2026-09-30**. It renders the landscape
lead once and centre-crops the 3:4 cover out of it (`imagery/encode.py::crop_to_ratio`). That
is one billed render instead of two, and in that mode everything above about differing
compositions stops being true. A Regenerate press in crop mode always redraws both frames and
counts as one render against `REGENERATE_BUDGET`. That predicate is also where the cover gets
its `MEDIA_SRC_RE` check: it is not in the document, so
`assert_media_is_ours` — which walks the document — never sees it.

**Because either frame can be redrawn alone, provenance lives on the frame, not the pair.**
`GeneratedImage` carries its own `prompt`, `negative_prompt`, `model` and `seed`; `Illustration`
is just `lead` + `cover`. They were shared fields while drawing either meant drawing both, and
after a one-frame redraw a shared prompt would be a true record of one picture and a false one
of the other — which is worse than no record, because it still answers when asked. Rows written
before the move keep their top-level fields; `Illustration`'s `model_validator` pushes them down
onto both frames on read, so nothing is lost and nothing has to be migrated. A frame's own value
always wins. Two consequences worth knowing: `IllustrateStage` reads `illustration.lead.prompt`
for its metrics (identical to the cover's on that path, one call and one seed), and the console's
spend budget counts **renders, not presses** — `REGENERATE_BUDGET` is 24 per ten minutes, the
same ceiling as the 12 two-render presses it replaced. A one-frame redraw on an article with no
imagery at all is a **409**, not a quiet upgrade to both: there is no other frame to keep, and
billing two renders to someone who asked for one is the mistake the budget exists to prevent.

## Media store

- **Image bytes live in Postgres, in `media_objects` — there is no `MEDIA_ROOT`.** They were
  on local disk until that turned out to be the one piece of published state a database backup
  did not cover: images generated by a host-venv run were served as 404s the moment the stack
  moved into containers, because the compose volume was its own storage and had never seen
  `backend/var/media`. Every path in `articles.generated_imagery` pointed at a file the API
  could not reach. One store, one backup, one restore. That directory and the one-time
  `import_media` script that emptied it are both gone — nothing reads or writes local disk.
  - **The URL did not change.** `/api/media/<2 hex>/<62 hex>.<ext>` is still what a document
    holds and what `MEDIA_SRC_RE` matches, so no stored JSON was rewritten. The two-character
    shard is now decorative — there is no directory — and stays because rewriting stored
    documents to drop a slash would be a migration with nothing to gain.
  - **A column on `articles` cannot work**, and the `media_objects` section of
    `migrations/0001_initial.sql` records why: ILLUSTRATE stores pictures at stage 5 and the article row is not created until
    PERSIST at stage 7, uploads are deliberately not article-scoped, and content addressing
    dedups across articles. Keyed by digest, related to nothing.
  - `store_image` is now `async` and takes a **session, not a path**, and **does not commit** —
    so ILLUSTRATE's pictures land with the orchestrator's stage commit and roll back with a
    failed stage, instead of surviving on disk as orphans. `prepare_image` is the pure half
    (sniff, digest, path) and is what `test_media.py` exercises without a database; the INSERT
    is covered in `test_db_invariants.py`.
  - `services/media.py` still decides what a file is from its first bytes (never the filename
    or Content-Type) and **SVG is still refused** — it is a script host, and this is served
    from the app's own origin. A document may only reference media the store wrote; checked on
    autosave *and* again in `ReviewService.approve()`. Do not relax either check to make a
    paste work. `media.py::image_node` builds the node the pipeline writes and
    `tests/unitTest/test_media.py` pins that `assert_media_is_ours` accepts it, because a writer and a
    checker that disagree produce an article whose *first* autosave is refused.
  - `store_image` still has **no size gate**: the upload route enforces `MEDIA_MAX_BYTES`, and
    a generated image never passes through it, so `services/illustration.py` is the only
    ceiling on that path.
  - Serving is `api/public/media.py`, not `StaticFiles`. It answers **404 for every miss** —
    malformed path, unknown digest, wrong extension — because a 422 on one and a 404 on
    another tells a prober which half was wrong. It also fixed a live bug in passing: on a
    stock Python install `.webp` is absent from the `mimetypes` table, so every generated
    illustration was served as `application/octet-stream`, and only browser sniffing in
    `<img>` kept it rendering.
