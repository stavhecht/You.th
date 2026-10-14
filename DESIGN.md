# You.th — Architecture & Design

Evidence-checked wellness content. A Pinterest-style public feed of short articles that
state what a product or trend claims, what the research actually shows, and a plain-language
verdict — every factual sentence carrying a citation to a real paper, every article approved
by a human before publication.

**The product is You.th; the repository, the Python package and the database are still named
`evidwell`.** The rename was a design change, not a migration: renaming a package, a DSN and a
table prefix buys nothing a reader ever sees and breaks every local environment that exists.

**Status:** implemented and verified against a live Postgres 16 + pgvector — 490 tests pass with
zero skips, both migrations apply cleanly, and every endpoint has been exercised end to end.
The pipeline has run end to end against **live PubMed and OpenAlex** with local Ollama models,
producing two articles from 92 real cached papers. The caveat is now narrower and detailed in
README → *Verification status*: the **hosted** providers (Claude, Voyage) have never been
called, so their clients and the token accounting remain written-against-documentation.

Deliberately deferred, per §11 of the brief: full text for paywalled papers, and the AWS
deployment. (Open-access full text is excerpted for up to six papers per article — §4.) The agentic query-refinement loop shipped 2026-09-07 as a LangGraph cycle around
RETRIEVE and RANK (§3.2); it is deterministic, so `LLMQueryStrategy` is still a declared seam
that raises — the template strategy is still what runs.

---

## 1. The four invariants

These are not conventions — each is enforced by a specific mechanism, named here so a reviewer
can check the mechanism rather than trust the prose.

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Nothing publishes without a human | No code path from the pipeline reaches `published`. The pipeline's terminal write is `pending_review`; `published` is set only by `ReviewService.approve()`, which requires an authenticated `reviewer_id`. DB `CHECK` constraint requires `reviewed_by`/`reviewed_at` to be non-null when status is `published`. |
| 2 | Every citation is real and was in the prompt | `evidence/validation.py` runs after synthesis, before persistence. It checks each emitted `S`-handle against the exact handle set built for that prompt, then that each maps to a source row with a resolvable PMID or DOI. A draft that fails is written as `validation_failed`, not `pending_review`, and never enters the queue. |
| 3 | Evidence grade **and quantity** cap verdict confidence | `evidence/grading.py::max_verdict_for_claims()`. Two questions: how good the best cited source is, and how many there are. A `supported` verdict backed only by in-vitro/animal sources is a **validation failure**, not a style note — and so is one resting on a single trial, since `supported` needs two sources at a supported-tier grade, **per claim**. The cap is applied twice: as an instruction in the synthesis prompt, and as a hard post-check the model cannot talk its way past. |
| 4 | The AI draft is immutable | `articles.original_content` is written once by the pipeline and never updated (enforced by a DB trigger). Human edits write to `edited_content`. The public feed renders `COALESCE(edited_content, original_content)`. |

A fifth, structural rather than ethical: **the feed card is derived, never generated.**
`services/card.py::derive_card()` computes headline + verdict badge + first sentence of beat 1
from the article body. There is no separate card generation call, so card and article cannot
contradict each other.

The generated cover (§3.4c) is the one thing on a card that is not literally in the body, and
it is held to the same statement by a pairing rule rather than exempted from it: `derive_card`
uses the portrait frame **only while the document's first image is still the landscape one it
belongs to**. The tile's picture was drawn for the article the reviewer is publishing, or the
card falls back to the body — it is never an independently chosen image. Note how narrow the
claim is: the two frames are two renders of one subject, not one photograph cropped twice, and
a reviewer may redraw either alone, so they need not even come from one generation (§3.4c).

**Where to check each one:**

| Invariant | Implementation | Test |
|---|---|---|
| #1 human-in-the-loop | `services/review.py::approve` — the only assignment of `published` in the codebase | `test_publish_path.py` (AST guard), `test_db_invariants.py` (CHECK constraint) |
| #2 grounded | `evidence/validation.py::validate_draft` | `test_invariants.py` — hallucinated handle, unresolvable source, uncited beat |
| #3 evidence caps verdict | `evidence/grading.py::max_verdict_for_claims` | `test_invariants.py` — including that an *uncited* strong source cannot raise the ceiling — and `test_evidence_quorum.py` for the two-source rule and per-claim scoping |
| #4 immutable draft | DB trigger `articles_original_content_immutable` | `test_db_invariants.py` |
| card derived | `services/card.py::derive_card` | `test_content.py` — card verdict always equals article verdict; the generated cover is dropped when the lead image is replaced, removed, or displaced |

---

## 2. System shape

```
        ┌──────────────────────────────┐   ┌──────────────────────────────┐
        │  Public site  /  (Vite SPA)  │   │  Review desk  /review        │
        │  feed · article · about ·    │   │  queue · TipTap · sources    │
        │  join · you · contact        │   │  its own chrome, lazy chunk  │
        └───────────────┬──────────────┘   └──────────────┬───────────────┘
       GET /api/feed, /api/articles/{slug}                │
       reader token (optional) on /api/readers/*          │ reviewer token
                        │                                 │
                    ┌───▼─────────────────────────────────▼───┐
                    │            FastAPI backend              │
                    │   api/public          api/console       │
                    │   feed · readers ·    router-level      │
                    │   contact             bearer gate       │
                    └───────────────────┬─────────────────────┘
                                        │
        ┌───────────────────────────────▼────────────────────────────┐
        │  Postgres + pgvector                                       │
        │  articles / sources / article_sources                      │
        │  users (reviewers)  ·  readers / reader_folders /          │
        │  reader_saves  ·  contact_requests                         │
        └───────────▲────────────────────────────────────────────────┘
                    │
        ┌───────────┴────────────────────────────────────┐
        │  Pipeline worker (local process)               │
        │  extract → retrieve → rank → full_text         │
        │  → synthesize → illustrate → validate → persist│
        └───────────┬────────────────────────────────────┘
                    │
     ┌──────────────┴───────────────┐
     │ PubMed · Europe PMC ·        │   Claude (extraction, synthesis)
     │ Semantic Scholar · OpenAlex  │   Voyage (embeddings)
     └──────────────────────────────┘   Hugging Face (illustration)
```

Two surfaces, one backend, one database. They share nothing but the `articles` table, the API
server and `App.tsx`: separate route groups, separate chrome, separate auth posture, and two
account tables that cannot be mistaken for each other (§3.2).

**Nothing on the public site links to the review desk.** That is discoverability, not access
control — a URL is not a secret, and the desk being at `/review` rather than `/console` protects
nothing on its own. What keeps readers out is `RequireAuth` on the client and the router-level
bearer dependency on every `/api/console/*` route. The desk links *out* to the live site, which
is the only crossing that carries no information about what is still unpublished.

---

## 3. Decisions made, with reasoning

### 3.1 Vite SPA, shaped for a Next.js port (your call, recorded)

You chose Vite. Consequence to keep in view: **article pages will not be indexed well by
search engines.** If discoverability becomes a goal, the port is real work — but I have kept
it mechanical rather than a rewrite:

- All data access lives in `frontend/src/lib/api/*` and is framework-agnostic (plain `fetch`
  + typed response contracts). No React Router APIs leak into fetch logic.
- Route components live in `src/routes/` mirroring a Next `app/` tree one-for-one
  (`routes/feed.tsx` → `app/(public)/page.tsx`, `routes/article.tsx` →
  `app/(public)/a/[slug]/page.tsx`).
- Nothing above the route level touches `window` during render, so the components are
  server-renderable as-is.
- The public group renders under a layout route (`PublicLayout`) rather than above `<Routes>`,
  which *is* `app/(public)/layout.tsx` with `<Outlet/>` becoming `{children}`. The review group
  brings its own header, so neither inherits the other's chrome — the earlier tree mounted the
  site bar above the router and the console rendered the public wordmark as a result.

The port would then be: replace the router, add `generateMetadata` + JSON-LD to the article
route, and mark the `(review)` route group `noindex`. `masonic` is client-only and would need a
`dynamic(..., { ssr: false })` wrapper or a static-grid fallback for the first paint.

**Feed narrowing lives in the URL, not in a context.** Search, subject and verdict are all
things a reader might bookmark, share or reach with the back button, and a provider above the
router would make all three impossible while adding a second source of truth for the feed to
disagree with. Raw values are narrowed against the enum on read rather than cast — `?subject=`
is a string a stranger can send someone, and an unknown value reads as "no filter", which is
both the safe interpretation and the one that keeps a mistyped link working.

**Search is server-side over the whole approved article, with a typo fallback that says what
it is.** `GET /feed?q=` (migration 0006, `FeedService.search`). It was client-side over loaded
pages until 2026-10-14, because the only server search on offer was a `LIKE` over card columns
that would silently miss every article's body. Now `approve()` writes a weighted tsvector —
headline, product and ingredients; then qualifier, summary, claims and excerpt; then the body's
text — built by one SQL function, `article_search_document`, that the migration's backfill
calls too. Only what a reader is shown is indexed: not `topic` (generation input), not cited
sources' titles. Full text ranks first (`websearch_to_tsquery`, `ts_rank_cd`); only when it finds
nothing, and the query uses no search syntax (`-word`, quotes, `or`), does a trigram
`word_similarity` match over headline + product answer — and the response says `match:
"similar"`, which the page renders as "no exact matches", because a close spelling can be wrong.
A search is relevance-ordered, never personalised, and offset-paged up to 200 results.

### 3.2 Auth: two kinds of account, minimal, with a real audit trail

A `users` table (email, password hash, role) and a JWT bearer dependency guarding
`/api/console/*`. Seeded with one admin. This is deliberately small, but it is *not* a shared
password — `reviewed_by` is a real foreign key, so "who approved this" is answerable forever.
Adding reviewers later is inserting rows; adding an invite flow is additive.

**Readers are a second table, not a role on the first.** `articles.reviewed_by` points into
`users`, so a row there is a claim about who is answerable for a published article. Folding
self-service signups into the same table would leave a correctly-set enum as the only thing
between registering and the review queue, and the console's gate one `WHERE` clause away from
admitting anyone. Two tables make the mistake unrepresentable: a reader id cannot satisfy
`require_reviewer`, because it is not in `users` at all.

The tokens are signed with the same secret and separated by a **`typ` claim** (`reviewer` /
`reader`), checked in `decode_token_subject` *before* the account lookup. That check is
redundant today — the missing `users` row would 401 anyway — and it is there because that
redundancy is exactly what evaporates the day someone changes either query. One secret to
rotate, one claim to check, and the two surfaces cannot be conflated by a future edit.
`lib/api/client.ts` chooses which token to send from the request path, never from the caller: a
caller that picks can pick wrong, and the wrong choice leaks a console token to an endpoint
with no business seeing one.

`optional_reader` is the feed's gate and **never raises**. A stale thirty-day token on a
surface that requires no account must degrade to the anonymous feed, not to a 401 — the
alternative is a reader whose token quietly expired seeing an error page where the site used
to be.

Password hashing is Argon2id (`argon2-cffi`), not bcrypt — no 72-byte truncation surprise.

**Login is rate limited, and the limit runs before the hash.** Argon2id being memory-hard is
the point of choosing it and also a liability on an unauthenticated endpoint: measured here, a
single junk login costs the server **31ms of CPU**, so **32 requests per second saturates a
core**. The `equalise_timing()` dummy verification that defeats account enumeration means
nonsense input costs exactly as much as a real attempt. So `/auth/login` was a CPU exhaustion
vector before it was a password oracle, and a limiter placed after the user lookup would have
fixed the second and not the first. Refusing costs 0.35µs — **~88,000× cheaper than the hash it
avoids**, which is the asymmetry the whole mechanism depends on.

`security/login_throttle.py` counts failures against two keys: a tight per-IP budget (10 per 5
min) that stops spraying and the CPU flood, and a looser per-account one (20 per 15 min) that
is the only defence against a distributed attack on a known reviewer address. Both are needed
— per-IP alone misses the patient attacker, per-account alone lets one source spray many
addresses and does nothing about junk emails, which hit no account at all. Failures are counted
for an unknown email exactly as for a wrong password, or the 429 becomes the enumeration oracle
the equal timing was there to prevent.

Three shapes worth defending, because the obvious alternative is wrong in each case:

- **It rejects rather than delaying.** A progressive delay only slows a client that waits for
  the response; an attacker firing 500 concurrent requests waits four seconds *in total*, while
  the reviewer who mistyped waits four seconds for real. It puts the friction on the honest
  user. Note this is the opposite of `retrieval/throttle.py`, which sleeps — correct for pacing
  our own outbound calls, backwards for a server.
- **Blocks escalate but always expire** (60s → 5min → 30min, capped; tier resets after a quiet
  hour). A flat window is a rate, not a deterrent — 10 guesses a minute forever is 14,000 a day
  at no cost. The cap and the expiry are what keep this from being a lockout: there is **no
  user-management endpoint in this API**, so a block needing an admin to clear it would require
  a shell on the server, and anyone who knows a reviewer's email could halt publishing at will
  (`approve()` is the only path to `published`).
- **The client IP is the socket peer, never `X-Forwarded-For`.** Nothing sits in front of
  uvicorn today, so the header is attacker-controlled: honouring it would let one client mint a
  fresh identity per request and bypass the IP budget entirely — worse than no limit, because
  the endpoint would look protected. Behind a load balancer this becomes
  `uvicorn --proxy-headers --forwarded-allow-ips=<balancer>` (§10), not a code change here.

**There are three separate throttle instances**, not one shared counter: reviewer login,
reader login, and the contact form (`api/deps.py`). Sharing would make the endpoints each
other's denial of service — readers and reviewers arrive from the same office NAT, and enough
failed reader logins would lock the console out of its own IP budget. The contact form is not a
login and nothing is being guessed there, but it is the other unauthenticated write on the
public surface and wants the same shape: an escalating, expiring, reject-don't-sleep budget per
IP and per address. A *successful* submission spends budget too, deliberately — a successful
post is exactly what a spammer repeats, and a person sending one message a week never reaches
the limit.

The state is an in-process dict behind a `LoginThrottle` Protocol. The honest limitation: N
uvicorn workers means N× the budget and a restart clears it. Neither matters with one local
process, and the Protocol is where a Redis implementation goes when it does. The table is
capped and evicts only *spent* entries — never blocked ones, or flooding it from fresh
addresses would release your own block. That holds because reaching a block costs ten Argon2
verifications, so the memory attack is bounded by the CPU attack it has to pay for first.

What this cannot do: with a password alone, blocking distributed guessing against a known
account and guaranteeing that account's owner is never blocked are not simultaneously
achievable. MFA or a trusted-device cookie is the real answer; until then the account budget is
set loose enough to fire on an attack rather than an afternoon.

### 3.3 Pipeline: local worker with a `pipeline_runs` table

Slightly more than `BackgroundTasks`, and worth it for one specific reason: **when a draft
fails citation validation, you need to know why.** `pipeline_runs` + `pipeline_stage_runs`
record per-stage status, timing, token cost, and the error payload. Without it, invariant #2
fails silently and looks like "the pipeline didn't produce anything today."

Each stage is a pure-ish function `(input, ctx) -> output`, run in a fixed order. That
shape is deliberate: each stage maps 1:1 onto a future Step Functions state, so the AWS
migration is a transport swap (the orchestrator calls Lambda instead of a local function)
rather than a redesign.

**The order is now a LangGraph `StateGraph` (`pipeline/graph.py`), not a list the
orchestrator walks.** The query-refinement loop is no longer deferred, and it went in exactly
where this paragraph predicted — a loop *around* stages 3–4, with no stage rewritten to
accommodate it. RANK flags any claim holding fewer than `QUORUM_FOR_SUPPORTED` supported-tier
sources (`rank.py::thin_claims`), meaning its verdict was capped by what retrieval found rather
than by what the evidence says, and a conditional edge sends the run back through RETRIEVE with
`QueryStrategy.broaden()` — which drops the outcome clause and keeps the subject.

Three properties hold it in place. Broadening **must not** relax the subject instead: that is
precisely the shape `UnanchoredQuery` exists to refuse (§4). It makes **no model call** —
`broaden` is `_compose` minus its second half, so a wider query cannot introduce a term the
narrow one was not allowed to use, and `LLMQueryStrategy` remains a seam that raises. And it is
bounded at one extra pass, because each pass spends real budget against NCBI's per-IP ceiling
that the worker shares.

That cycle is the only branch in the graph, and it is the whole reason an ordered list was no
longer enough: a list runs each stage once. Everything else the orchestrator did it still does
— the per-stage commit, the two session factories, `_abandon`'s rollback-before-record
order, and the last stage's write committing with the run-completion row are unchanged, and none
of them is expressible as a LangGraph feature. Its checkpointer is deliberately unused:
durability is already the per-stage commit plus the heartbeat sweep, and a second mechanism
beside them is how the no-double-execution guarantee gets re-derived by accident.

**The orchestrator owns every transaction boundary, and commits per stage.** No stage commits
and the worker does not commit; a successful stage is committed by the orchestrator and a
failing one is rolled back by it.

One transaction per run was the obvious first shape and it was wrong in three ways. A synthesis
failure discarded the `sources` rows and the embeddings RetrieveStage had just paid for, so the
retry re-fetched and re-embedded everything — the cache defeated at exactly the moment it
exists for. The connection sat `idle in transaction` for the length of every model call
(measured: `xact_age` tracks the call second for second), holding the row locks the cache takes
on matched sources, so a second worker on an overlapping topic would block behind them for
minutes. And each in-flight run pinned a pooled connection regardless of whether it was using
one.

Per-stage commit is safe because `sources` is an idempotent cache with no invariant attached,
and the only stage writing article data is PersistStage — one stage, so still atomic in itself.
It is also what the Step Functions target does anyway: one Lambda, one connection, one
transaction per state. This narrows the gap between local and deployed rather than widening it.

**The final stage is the exception**: its write commits together with the run's completion row.
Separating them opens a window where an article is committed but its run still reads `running`,
so a worker killed there has the run requeued as stale and writes a *second* article for the
same topic. Persist is last, so nothing long-running follows it and the atomicity costs
nothing.

**Retryable failures requeue, up to `PIPELINE_MAX_ATTEMPTS`.** `StageError.retryable` was
declared from the beginning and read by nothing; the cases that set it — a throttled provider,
a claim that went unsearched (§4) — are exactly the ones a second attempt fixes, and they are
what makes the surviving cache pay for itself. A requeued run gets `next_attempt_at` set,
because the poll interval is five seconds and a run requeued without a delay spends its whole
budget on the same 429 inside fifteen seconds. `attempts` is incremented when a worker
*claims* the run, not when it finishes, so a run that reliably kills its worker cannot cycle
forever. Stage rows are kept per attempt rather than overwritten — diagnosing a run that
succeeded on its third try means seeing what the first two did.

**A crash is recovered by heartbeat, not by a duration cutoff.** A run row says `running`
because a worker said so, and nothing retracts that if the worker is killed — the console then
reports work no process is doing, indefinitely. The first version of this compared `started_at`
against a 30-minute cutoff and swept once, at worker boot, which was wrong in three separate
directions:

- **Stuck forever.** A worker killed at 14:00 and restarted at 14:01 saw a one-minute-old run,
  judged it healthy, and never swept again. The row stayed `running` for the life of the
  process — the failure mode the sweep existed to prevent, reached by the sweep itself.
- **Requeued while alive.** A duration cutoff cannot distinguish slow from dead. A cold local
  model plus a provider backoff puts a legitimate run past any fixed number, and requeueing one
  starts a *second concurrent execution* — the double-article write the paragraph above makes
  `_finish_run` atomic to prevent, arriving through a different door.
- **Unbounded lives.** Recovery ignored `attempts`, so a run that kills every worker claiming
  it was resurrected indefinitely.

`pipeline_runs.heartbeat_at` is pinged on a timer by the worker for as long as
a run is in flight, so staleness means *no sign of life for two minutes* rather than *started a
long time ago*. That detects a kill in ~2 minutes instead of 30 and cannot fire on a live run
at any duration. On a timer rather than between stages because a single synthesis call against
a local model runs for minutes, so a per-stage ping would need a window longer than the slowest
stage — which is the coarse timeout being replaced. The ping never raises: a heartbeat that
dies while its run continues makes the run *look* abandoned, which is the double-execution case
again, so a failed beat is logged and retried.

The sweep runs between polls, never during a run, so a worker cannot sweep its own in-flight
run whatever its heartbeat is doing — a property of where the call sits, and the reason it is
not a background task. Recovery spends the same budget as a retryable failure (which is what
counting `attempts` at claim time is *for*): budget left requeues behind the same
`retry_delay` ladder, budget spent fails the run with `attempts_exhausted`. The stage rows the
dead worker left open are closed out in the same pass, both because a stage claiming `running`
under a queued run is a worse lie than either alone, and because the stage name is the one
piece of diagnosis a hard kill otherwise destroys — "died during synthesize" points at a model
timeout, "died during retrieve" at a provider.

`WORKER_STALE_AFTER_SECONDS` must stay at least 3× `WORKER_HEARTBEAT_SECONDS`, checked at
startup: set closer, a single slow `UPDATE` declares a live run dead, and the cost of that is
a duplicate article rather than a log line.

### 3.4 Article body as TipTap JSON, not markdown

`original_content` and `edited_content` are `JSONB` holding a TipTap document. Citations are a
first-class inline node (`{type: "citation", attrs: {sourceIds: ["S1","S3"]}}`), not a `[S1]`
string in prose. Three payoffs: the editor can render a citation as a clickable chip that
opens the sources panel; validation walks a typed tree instead of regexing prose; and
`original` vs `edited` diffs structurally.

The model still *emits* `[S1]` markers in a plain-text body — asking it for TipTap JSON would
burn tokens and invite malformed output. `services/tiptap.py::body_text_to_doc()` parses the
marker syntax into the document tree at the assembly step. Parsing failure is a validation
failure.

**Multi-source citations are accepted in both spellings — `[S1][S5]` and `[S1, S5]` — and the
marker pattern is defined once, in `domain/contracts.py`.** The comma form was originally
rejected, and the cost of that was a whole draft: the prompt's only example was a single handle,
so llama3.1:8b wrote `[S1, S5, S8]`, the body failed to render, and the article was written
`validation_failed`. That status never enters the review queue, so the run *looked* like nothing
had happened. Strictness at the parser is not free when the thing being parsed is generated:
rejecting a spelling nobody documented discards work rather than catching an error.

The single definition matters more than the widening. `body_text_to_doc()` and
`extract_handles()` both read markers, and they feed different things — the document tree and
`all_cited_handles()` respectively. If the extractor cannot read a form the parser renders,
`was_cited` is false for a source the article visibly cites and `check_beats_are_cited` reports
an uncited beat that is cited on the page. Validation would be measuring a document nobody is
looking at.

Malformedness is checked by *elimination*: strip every well-formed marker, and any bracket still
standing is a parse failure. The earlier "not a valid marker" pattern had to enumerate the ways
a marker can be wrong, and missed unterminated runs like `[S1, S5` — which matched nothing and
fell through as literal text, printing a broken marker into the finished article.

**Every nullable JSONB column uses `NullableJSONB`, not bare `JSONB`.** SQLAlchemy's JSON types
default to `none_as_null=False`, which persists a Python `None` as the JSON literal `null`
rather than as SQL `NULL`. That breaks the `COALESCE` above — a never-edited article renders as
JSON `null` instead of falling through to the draft — and it breaks `WHERE error IS NULL` for
every stage the orchestrator records as succeeded. Both were live: the first article written
after the schema reset had `jsonb_typeof(edited_content) = 'null'` and all six of its stage rows
had the same in `error`.

It stayed invisible because `json.loads('null')` is `None`, so every Python reader
(`display_content`, `ReviewService.approve`) took the correct branch through `or`. The defect
only appears in the SQL this document and `services/card.py` both name as the mechanism, which
is the worst place for it to first surface — enforcement that is documented but not actually in
force reads exactly like enforcement that works.

### 3.4b Article media: uploaded or generated, never linked

A picture reaches a draft two ways: a reviewer adds one from their own machine, or the
pipeline's `illustrate` stage draws one (§3.4c). A reviewer can also embed a YouTube video.
All are block-level TipTap nodes beside the beat paragraphs, so none disturbs the beats — those
are addressed by `attrs.beat`, and a picture above beat 1 does not change which paragraph the
feed card is derived from. The same holds for the section headings and paragraphs the pipeline
writes between beats 2 and 3 (§6): they carry no `beat` attribute, so however many of them
there are, `beat_text` still finds the three.

The two sources of a picture are not two code paths. A generated image goes through the same
`store_image`, produces the same content-addressed `src`, is built into a node by
`media.py::image_node`, and is re-checked by the same `assert_media_is_ours` on every autosave
and again at approve. Once it is in the document it is an ordinary picture: the reviewer can
resize it, re-wrap it, replace it or delete it with the controls that already exist.

Two rules, enforced in `services/media.py` and re-checked at approve:

**Images are uploaded, never linked.** `POST /api/console/media` stores the bytes and returns
the only `src` shape the server will accept back. A linked image breaks when the other site
reorganises and tracks the reader until it does, so the image node's `parseHTML` declines any
other `src` — a picture copied out of a web page is dropped, while the same picture *dropped
as a file* is uploaded and kept. What a file is is decided by its first bytes; the upload's
filename and `Content-Type` are read and discarded. **SVG is not on the allowlist**: it is a
script-bearing document, and one served from our own origin inside a published article is
stored XSS against every reader of it. PNG, JPEG, GIF and WebP cannot carry script.

**A video is an id, not a URL.** The document stores the eleven-character YouTube id and the
renderer builds the embed src from it, so no string anyone typed reaches an iframe intact. The
public page renders a still until it is pressed — an embedded player is about a megabyte of
Google's JavaScript executed on every reader of every article that has one, and mounting the
iframe on the click makes being tracked by YouTube something the reader chose.

Storage is content-addressed local disk — filename is the SHA-256 of the bytes, so uploads
dedupe, cannot collide, and nothing client-supplied reaches the filesystem. Served at
`/api/media/…` rather than a prefix of its own because the dev proxy and any deployment
already route `/api`. The S3 swap is that one module; orphan collection (an image dropped from
a draft leaves its file) is deferred with the rest of §11.

**Layout is two attributes, never CSS.** A reviewer can resize a picture or a video and wrap
prose around it — Word's "Square" — and the whole model is `width` (20–100, a percentage) and
`align` (`none` / `left` / `right`). Both are validated server-side beside the `src` check, on
autosave and again at approve.

The three things that follow from storing data rather than a stylesheet:

- **The same classes render both sides.** `mediaWrapClass()` in `lib/media.ts` produces the
  `.ew-media` classes that `styles/evidwell.css` defines, and the console editor and the
  published article both use them. That identity is the only way "the reviewer approves what
  publishes" is true of layout and not just of words.
- **It collapses on a phone.** The float and the width live inside one `@media (min-width:
  640px)` block, so under that every picture is a full-width block — a 40% float in a 320px
  column leaves about fifteen characters a line. An inline `style` could not express this,
  which is the concrete reason the document does not store one.
- **Nothing reviewer-authored reaches a reader as style.** The renderer turns one of three
  words into a class it already ships. There is no string a reviewer can supply that becomes
  CSS on a published page.

A percentage rather than pixels because the editor's column (~850px) and the article's measure
(76ch) differ, and a percentage is the only unit that means the same in both. No x/y
coordinates: media stay siblings of the beat paragraphs and are placed by being dragged
between them, which is what keeps `attrs.beat` — and therefore the derived card — meaningful.

In the editor the two nodes are React node views (`console/MediaNodeView.tsx`). Worth knowing
before editing it: TipTap's React renderer wraps a node view in an element of *its own*, and
that element is the one in the editor's flow, so it is the one that has to float — hence the
`attrs` option on `addNodeView()` rather than styling the component's own root. Dragging the
corner writes the width straight to that element's custom property and commits a single
attribute on release, so one gesture costs one undo step and one autosave rather than sixty.

### 3.4c Generated illustration: two frames, one locked prompt builder, no claim

Every draft gets two pictures from `black-forest-labs/FLUX.1-schnell`, drawn in stage 5 by
`pipeline/steps/illustrate.py` and stored through the ordinary media store. A landscape **lead**
goes into `original_content` as a normal image node; a portrait **cover** lives only in
`articles.generated_imagery` and is what the feed tile shows. Two frames rather than one because
every shape `tileRatio()` produces is 1:1 or taller, so `object-cover` fits a landscape picture
to a tile by discarding its sides. §1's fifth rule survives through the pairing predicate
described there.

**Since 2026-09-30 the default is `IMAGE_COVER_MODE=crop`: one render, the cover cut from
it.** The landscape lead is rendered once, and the portrait cover is centre-cropped out of it to
3:4. At the default sizes that is 624×832, ample for a 190px tile. That halves the cost to one
billed render per article, and it makes the two frames provably one photograph. A Regenerate
press in this mode always redraws both frames, for one render. `render` mode keeps the
two-generation behaviour described below. A local FLUX provider (mflux, 4-bit) was measured on
the target 16 GB M2 Pro and not built: 198 s and 18.4 GB of peak GPU memory for one 1216×832
image, and swap grew from 6.2 to 16.8 GB.

**In `render` mode the two frames are not the same photograph, and the docs said otherwise
until it was measured.** A seed initialises latent noise shaped like the frame, so one seed at two sizes is
two compositions — and the provider `auto` currently resolves to (`nscale`, reached through HF's
OpenAI-shaped images endpoint, which has no seed field) ignores seeding altogether: two
identical requests came back different on 2026-08-24. What survives is the useful half. Both
frames were drawn for one article under the same locked prompt builder, which forbids either
from asserting anything — so there is no composition the tile can promise and the article
withhold. The stronger guarantee is available if it is ever wanted: render portrait once and
crop the landscape with Pillow. It halves the cost, and the centred-subject-in-negative-space
style makes the crop safe. It was not taken because each frame composed for its own shape looks
better, and the weaker guarantee is sufficient for the rule it has to keep.

**Either frame can be redrawn on its own**, and the same argument is what licenses it. A
reviewer can like the picture in the prose and not the one on the tile; the two are seen in
different places and judged separately, so `POST /console/articles/{id}/illustration` takes a
`frames` list and draws only what was asked for, keeping the rest from the article's current
record. Nothing is written back until every requested frame has been drawn and stored — a cover
saved beside a lead that then failed is the one state `derive_card` cannot reason about.

The cost of that freedom is that provenance had to move off the pair and onto the frames.
`GeneratedImage` carries its own `prompt`, `negative_prompt`, `model` and `seed`; `Illustration`
is now just `lead` + `cover`. After a one-frame redraw the two genuinely come from two prompts,
and a single shared field would be a true record of one picture and a false record of the other
— which is worse than none, because it still answers when asked. Rows written before the split
keep their top-level fields and a `model_validator` pushes them down onto both frames on read,
so no migration is needed and no history is discarded; a frame's own value always wins.

**A generated picture must not become a claim.** This is the design constraint, and it drives
everything about `imagery/prompt.py`. An illustration of someone visibly healthier beside a
supplement asserts efficacy no citation backs; a clinic or a lab bench borrows authority the
article has not earned; a before-and-after pair states a result outright. None needs a caption
to be read as an argument, and an image is the register readers scrutinise least. So the prompt
is assembled here from fixed parts rather than written by a model, and the exclusions are
hard-coded.

Three consequences. **No second model call writes the prompt**: asking an LLM for a nice image
prompt reintroduces the ungrounded generation this pipeline exists to prevent, one layer down
and unvalidated. **The verdict never reaches it, and neither does the headline**, which is where
the verdict usually is — an illustration that looks hopeful for `supported` and bleak for `weak`
is a scoreboard drawn in pictures, composed before a human approved the verdict. And **the
exclusions ride in the positive prompt**: FLUX.1-schnell is guidance-distilled and runs at
`guidance_scale=0.0`, where a CFG-style `negative_prompt` has nothing to act on. It is sent
anyway, for the providers and checkpoints where it bites, but it is not what enforces any of
this — the positive prompt is.

**Locked does not mean identical, and the first version got that wrong.** It fixed the motif,
the camera angle, the arrangement and the light, leaving the subject noun as the only variable —
three words in eighty. On real renders that produced a feed where every article was white pills
on warm beige: the prompt described a space so narrow that different sampler noise still landed
in the same picture. The fix is not more randomness at the sampler, and cannot be — the provider
ignores `seed` entirely, so those renders already came from different noise.

So the prompt now separates **treatment from composition**. `TREATMENT` — palette, matte
surfaces, register — is fixed and applies to every render; framing, arrangement, light, surface
and the motif itself are selected from tuples by the seed. Exactly 5,184 combinations, all of
them the same magazine. Locking the treatment is what makes a feed read as one publication;
locking the composition as well is what made it read as one photograph.

**The motif is an axis, and it is the one that mattered most.** Each subject held a single
string, so every article the hint table called a `SUPPLEMENT` — which is most of a wellness
feed's vocabulary — rendered the literal phrase *"loose capsules, a small heap of pale powder
and a plain unlabelled glass jar"*. The four composition axes could not compensate, because
they only re-photograph whatever the motif named: five hundred angles on one still life still
read as one picture. `_MOTIFS`, `_PEOPLE_MOTIFS` and the default fallback therefore hold
`_MOTIF_RADIX` variants each, checked at import for the same reason the axis lengths are. Two
details are deliberate. It is the **slowest** digit — stride past all four composition axes — so
neighbouring seeds move the camera and distant ones change the objects, which keeps a seed
meaning "this article's composition" rather than "a reroll". And the article's noun moved to the
*head* of the sentence (`Still life of Ashwagandha KSM-66: …`) from a trailing `, suggesting
<noun>`; arriving after three concrete object nouns that had already specified the frame, it was
doing almost nothing, which is why creatine, ashwagandha and melatonin all drew the same jar.

**Two axes carry the tonal range, because the palette cannot.** `_SURFACES` varies material and
**value** — pale paper through to dark oiled wood — never hue, and `_LIGHTS` reaches from even
overcast through hard directional sunlight to near-dark. Five pale neutrals under four flavours
of soft daylight differ on paper and not at feed-tile size, especially on a four-step distilled
checkpoint; contrast and shadow depth are the only range available once hue is locked, and
locking hue is what the paragraph above is protecting.

**The strides are a mixed radix**, each the product of the axis lengths before it, so the axes
are exact digits of `seed % 5184` and every combination appears once per 5,184 consecutive
seeds. They were coprime odd numbers `(1, 7, 53, 401)` on the theory that coprimality *between
the strides* buys independence *between the axes*. It does not, and the failure is arithmetic
rather than statistical: with stride 7 against a 5-option axis, writing `seed = 35q + r` gives
`(seed % 5, seed // 7 % 5) == (r % 5, r // 7)` — 35 values of `r` landing on 25 pairs, so 10
framing×arrangement combinations came up exactly twice as often as the other 15. Measured over
400k random seeds: χ² 49073 on 16 degrees of freedom, commonest pair 23008 against 11272 for the
rarest; 511 on 499 under the radix. The quantity that matters is each stride against the product
of the *preceding axis lengths*, not the strides against each other. `_STRIDES` is therefore
derived from `_AXIS_RADIX` rather than written out, so it cannot fall out of step when someone
adds a seventh framing — as widening the axes from `(5, 5, 4, 5)` to `(6, 6, 6, 6)` proved,
since that change touched no stride at all.

That also gives the seed real work. It selects *our* prompt rather than the provider's noise, so
a retried run composes the same photograph and Regenerate composes a different one — the
property `seed_for_run` was written for and could not deliver while it depended on the provider
honouring it.

**A person may appear, on the one path where the subject is an activity.** The distinction is
depicting the subject versus depicting a result. A protocol *is* something someone does, so a
body mid-stretch on a mat or a forearm mid-lift photographs what the article is about and
asserts nothing beyond "this is the thing we looked at". A body beside a supplement jar, a bowl
of food or a tube of cream is the case the rule above describes: nothing in frame is doing
anything, so the only thing the person can be there to communicate is an outcome.

So people are gated on the motif — `_PEOPLE_MOTIFS`, currently `PROTOCOL` alone — rather than
switched on globally, and every other subject renders the still life unchanged. `DEVICE` is the
one with a real argument on both sides (a hand on a massager is use; a person glowing beside a
red-light panel is efficacy) and is deliberately left out until someone wants to make it. Two
guards come with the gate, both because being wrong here costs far more than a plain still life:

- **Only a confident signal opens it** — a reviewer's explicit `subject`, or a motif matched
  against `product` itself. A motif inferred from the *topic* does not, because topics name
  outcomes and `sleep` is a protocol word: "magnesium for sleep" would otherwise have put a
  body in a supplement article, which is precisely the refused picture. Uncertainty falls back
  to objects, which is always safe.
- **`EXCLUSIONS_WITH_PEOPLE` replaces the body ban rather than dropping it.** Read the two side
  by side: the clinic, text, branding and before-and-after clauses are identical, because none
  of them was ever about whether a person was in frame. What changes is that "no body" becomes a
  much narrower ban on the body being *displayed* — a physique, a transformation, eye contact
  with the camera. Those are the forms in which a person states a result, and they are what the
  blanket rule was actually buying.

The people path carries its own composition vocabulary (`_PEOPLE_FRAMINGS`, `_POSES`,
`_PEOPLE_SETTINGS`; `_LIGHTS` is shared) because the still-life words do not transfer — a
flat-lay of a human being is a mortuary photograph — but the same option counts, so the mixed
radix and the 5,184 combinations hold on both. `_PEOPLE_MOTIFS` carries the same number of
variants as every other motif tuple for exactly that reason. `TREATMENT` is shared unchanged, and that is what
keeps a yoga photograph in the same magazine as the pill still lifes instead of drifting into
stock fitness photography. The genre word moved out of `TREATMENT` to `_Path.genre` in the
process: `editorial still-life photograph` was a composition clause hiding in the treatment
string, and it flatly contradicted a motif with a person in it.

The motif is inferred from the article's text when no reviewer has classified the draft, which
is every pipeline call. **This is not a classification and must never become one**: it decides
which objects are photographed, is never displayed, and never writes `articles.subject`. The
asymmetry is what licenses it — a wrong subject is a confident false statement on the page, a
wrong motif is a slightly odd still life.

**Precedence between the hints is by field, not by position in the table.** `infer_motif_subject`
takes product, then topic and ingredients, and stops at the first field that matches anything.
Searching them concatenated — which is what it did — lets a vague word in a low-trust field
outrank a precise one in a high-trust field: `SUPPLEMENT` is checked last and `PROTOCOL` holds
`sleep`, `exercise` and `training`, which is how articles name their *outcome*. "Magnesium
glycinate" / "magnesium for sleep quality" and "Whey protein" / "protein for muscle after
exercise" both resolved to `PROTOCOL` and drew a towel and a timer. Topics routinely name an
outcome, so that was the ordinary path for supplements, and no reordering within the table could
have fixed it. It is also the tier the people gate above reads: only `product` is trusted enough
to put a body in frame.

**Failure is never fatal.** A picture is decorative and an article without one is publishable —
the feed already draws a typographic tile, which is the resting state of the design rather than
a degraded one. `IllustrateStage` catches everything and records a `cause` in
`pipeline_stage_runs.metrics`. This is the deliberate inverse of §4's throttle rule, and the
difference is visibility: recall lost to a 429 produces a more cautious verdict that reads as a
correct answer, whereas a missing picture is on the reviewer's next screen. Fail on the
degradations nobody can see.

**Cost is out of the ledger**, with embeddings and for a stronger reason: an image is billed per
render and `pipeline_stage_runs` prices four token columns. `record_usage` is never called here.

A reviewer who dislikes a picture has two moves, both ordinary: edit or delete it in the editor
like any other image, or redraw it with a fresh seed through
`POST /console/articles/{id}/illustration`. Three controls sit on that one route, each placed
where its effect is visible. **Regenerate picture** and **…and the tile** are in the editor
toolbar, beside Image, because that is where the article's own picture is. **Redraw tile** is in
the *Show draft* overlay, because the tile is only visible there — a button that spends money
and changes nothing on screen is a button reviewers press twice. The overlay offers it only when
the tile is actually showing a generated cover; with the pairing broken by a reviewer's own
picture, a redraw would succeed, bill a render and change nothing.

That route is the only console action that bills an external provider per press, so it sits
behind `security/spend_throttle.py` — a per-reviewer budget that rejects with `Retry-After`
rather than sleeping, checked before the render for the same reason the login throttle is
checked before the Argon2 hash. **It counts renders, not presses** (24 per ten minutes), so a
one-frame redraw costs half a two-frame one; charging the cheaper action at the expensive
action's rate is the wrong incentive on the one control that exists to stop needless spending.
The route also uses the article's `subject` when one is set, which the pipeline cannot:
`subject` is reviewer-set and the article row does not exist when `illustrate` runs.

### 3.5 Embeddings: Voyage, behind an interface

`voyage-3` tier: strong on scientific text, cheap enough to embed thousands of cached
abstracts. `EmbeddingProvider` is a Protocol with one method, so OpenAI is a config swap.

**Vector width is a single config constant** (`settings.embedding_dim`, default 1024) because
changing it is a table migration and an index rebuild, not a config change. The migration
templates the dimension.

### 3.6 LLM: a model setting per job, structured outputs, no sampling params

Both generative calls go through `client.messages.parse()` with a Pydantic `output_format`,
which constrains the response to the schema and hands back a validated model instance. This
removes a whole class of failure (malformed JSON, missing keys) before our own validation runs.

**The two calls carry separate model settings, because they are different jobs — though both
currently resolve to the same model:**

| Call | Model | Why |
|---|---|---|
| Extraction | `claude-sonnet-5` | Topic → product + claims. Mechanical and schema-constrained, and *intended* for a small model — see below for why it isn't on one. |
| Synthesis | `claude-sonnet-5` | Where the guarantees are produced. Must cite only supplied `S`-handles (#2) and keep the verdict under the evidence ceiling (#3). A failure here is the expensive kind. |

Both are config (`settings.extraction_model` / `settings.synthesis_model`), not constants.
**If drafts start landing in `validation_failed`, raise the synthesis model first** — that is
the signal a downgrade was too aggressive, and `pipeline_runs` records it per stage.

The asymmetry the split was built on is still the right one: validation is a hard post-check
that no model talks its way past, so a weaker *synthesis* model degrades **yield** (more
rejected drafts), not **correctness**. That is what makes downgrading safe to try at all.

**Extraction is the case where it did not hold, which is worth recording.** `claude-haiku-4-5`
was the original choice and was reverted after sampling: it left `ingredients` empty in 9 of 10
runs. That is not a schema failure — the field is optional, the response validates, the stage
reports success — and the effect lands two stages away, where the query is built from the
ingredient list and quietly loses its anchor. So the cheap-model failure mode here is not "loud
and cheap" as first assumed; it is the same silent-degradation shape this document keeps
returning to, and it is why extraction sits on Sonnet.

**The consequence was worse than this section originally claimed, and the correction is the
interesting part.** The prediction here was that an unanchored query searches the product's
*brand name*, returns nothing, and routes to the `no evidence` path — a wrong article, but a
*cautious* one. That is not what the code did. `_compose` fell back to **claim keywords**, so a
run on creatine with `ingredients: []` searched `(workout AND performance)` and got 50 real
papers about pre-workout blends, CrossFit programming and probiotics — none of them about
creatine. Synthesis cited two of them, citation validation resolved both handles, and the
article shipped a `supported` verdict at `systematic_review` grade. Failing safe and failing
loud are different properties, and assuming the first gave the second is what let this run.

Two changes follow. The subject group now falls back to `product` (required, always present)
before it falls back to anything else, so the empty-`ingredients` case is anchored rather than
merely detected. And when nothing yields a substance, `TemplateQueryStrategy` raises
`UnanchoredQuery` and `RetrieveStage` fails the run — it does not warn. A warning was the
original design here, and a warning on a worker's stdout is not a control: every downstream
signal still read as success. This is the same rule as the throttle one in §4 — "we searched
and found nothing" is publishable, "we searched for the wrong thing" is not — and the two
arrive at synthesis looking identical, so they have to be separated where the query is still
visible.

A second, harder constraint now pins it there: both calls pass `thinking` explicitly (below),
and Haiku 4.5 predates adaptive thinking, so that argument **400s** on it. Reaching for a small
extraction model again means handling that difference, not just changing the setting.

Notes that matter for implementation:
- `temperature` / `top_p` / `top_k` are **rejected** on Opus 5 and Sonnet 5 — a request
  carrying them returns a 400. We send none on either call, so the code is safe across the
  whole model range. Behaviour is steered by prompt only.
- `max_tokens` caps thinking **plus** output — so synthesis is sized generously (8K) even
  though the article is ~300 words, and extraction gets 4K for a response of a few hundred.
  Every call passes an explicit cap; there is no unbounded request in the codebase.
- **`thinking` is passed explicitly rather than left to the model default**, because that
  default is not stable across the range: Sonnet 5 and Opus 5 think when it is omitted, Opus
  4.8 and 4.7 do not. The model is a *setting*, so an implicit default would let
  `EXTRACTION_MODEL` silently decide whether the budget above is spent on thinking or on
  output — config changing behaviour the stages are supposed to be insulated from. The cost of
  being explicit is the Haiku incompatibility noted above; it is the better trade.
- **Hitting the cap is diagnosed, not inferred.** A truncated structured response has no
  parsed output at all, so it arrives looking exactly like a schema failure and sends you to
  `domain/contracts.py` instead of to `max_tokens`. `_check_truncation` reads `stop_reason`
  and names the real cause. This is a thinking-budget symptom specifically: the two share one
  allowance, so a thinking-heavy turn on a tight cap produces it while the schema is fine.
- The synthesis system prompt is stable across calls and marked with `cache_control`; the
  per-article source block is volatile and goes after it. **The minimum cacheable prefix is
  per-model and not monotonic** — 512 tokens on Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5 —
  and a prompt below it silently fails to cache, with no error. Measured with
  `messages.count_tokens`: the synthesis prompt is **1285 tokens**, so it still caches on
  Sonnet 5, but with only ~260 tokens of headroom — trimming it would silently break caching.
  The extraction prompt is **306 tokens** and caches on no model; it never did on Opus 5
  either (512 minimum), so that is not a regression, and at 306 tokens it does not matter.

### 3.7 Cost: store tokens, price at read time

Every number in the paragraph above was a claim nothing could check. `TokenUsage` had carried
`cache_read_tokens` and `cache_write_tokens` since the Anthropic client was written, and the
persistence layer dropped both — so the `cache_control` breakpoint that section justifies at
length was, by construction, unmeasurable. `pipeline_runs` stored one summed input/output pair
and no model id, which is not "imprecise" but **unpriceable in principle**: extraction and
synthesis are separately configurable and may be different models at different rates.

Three decisions, each with a cheaper alternative that fails quietly.

**The ledger sits on `pipeline_stage_runs`, not on the run.** That table is already at the
grain the question needs — one row per run × attempt × stage, kept per attempt rather than
overwritten. Cost surprises come from retries, and a run-level rollup cannot show them. The
run keeps a rollup so "what did this consume" stays one row read, and that rollup is written
as an **increment**, not an assignment: `ctx` is rebuilt per attempt, so assigning would
overwrite the first attempt's tokens with the second's and report the cheapest attempt as the
run's whole cost.

**Four token columns, not two.** The classes are priced an order of magnitude apart in both
directions — a cache read is a tenth of a fresh input token, a cache write a quarter more than
one. Folding them into `input_tokens` would make caching look free and the first call look
cheap, in opposite directions, so the errors would not even cancel.

**No cost column anywhere.** Tokens are a fact about a call that happened; a price is an
external number that changes. `llm/pricing.py` computes cost at read time, so a price
correction fixes history instead of leaving it silently wrong. The trade is the mirror image —
past runs re-quote at today's rates — and that is right while these figures inform a build
decision rather than an invoice. If a run's cost ever has to be *owed* rather than
*estimated*, this becomes a table with effective dates and the choice inverts.

Two things follow that are easy to get backwards:

- **An unpriced model reports `null`, never `0`.** They are both falsy and mean opposite
  things, and the silent zero would appear exactly when someone points `SYNTHESIS_MODEL` at
  something new — the one moment "this run cost nothing" would be believed. A run total is
  all-or-nothing for the same reason: extraction alone is ~1% of a run, so reporting it when
  synthesis is unpriced yields a number both plausible and wrong by two orders of magnitude.
  Model ids are provider-namespaced (`anthropic/claude-sonnet-5`) so a *local* model, which is
  genuinely free, is never confused with an unpriced hosted one.
- **Tokens spent by a call that failed are still recorded**, and this is the case worth having.
  Truncation spends the entire 8K synthesis budget and produces nothing, so a run reported at
  zero would make the pipeline look cheapest at the moment it is burning the most. `LLMError`
  therefore carries `usage`, the stage records it before converting to a `StageError`, and
  `_abandon` writes it *through* the rollback that discards the stage's own writes — the
  rollback undoes ours, not the provider's charge. This is also why `record_usage` mutates the
  context in place rather than returning a copy: a raising stage returns no context.

**Embeddings are outside the ledger, deliberately and visibly.** `EmbeddingProvider` returns
bare vectors, and threading usage back through it would change the Protocol's return type and
every caller. The gap is about 28k tokens per cold run (~$0.002 at voyage-4 rates), roughly 1%
of a synthesis call. The number that would justify the refactor is not a run — it is a full
re-embed of the cache, and that is a script, not a pipeline stage.

### 3.8 The visual system: You.th over Modernist

Two stylesheets, and the split is what makes retuning possible at all.
`styles/design-system.css` is the **vendored** Modernist design system — treat it as read-only
and re-copy it from the design project rather than hand-editing, because that project is what
the readme and the component pages render from. `styles/youth.css` is the product layer: it
declares the `--ew-*` tokens every component actually speaks, and re-points Modernist's own
`--color-*` roles at them so the focus ring and `::selection` follow the active theme instead
of staying stuck on the light palette.

Two things are carried from the previous theme because the new comp does not contradict them.
**Modernist is light-only** — its `--color-bg` / `--color-text` are fixed hexes — so the
product tokens are the ones that flip. And **the ink is a four-step ramp, not one ink**:
headline, excerpt, meta and rule each have a different contrast job, and `color-mix()` on a
single ink cannot hit those ratios predictably across both grounds.

What the You.th layer changes, and why each matters more than it looks:

- **Warm paper, not grey.** `#f4f1ec` under `#201e1d`, so the page reads as stock rather than
  as a screen.
- **Radius is back.** Modernist is a zero-radius system; You.th rounds tiles at 20px and panels
  at 22px. The radii are tokens in one file rather than literals in components, so the two
  systems disagree in exactly one place instead of everywhere.
- **Actions are pills.** Now that cards have corners, `rounded-full` is what still separates a
  control from a card at a glance. This holds on the review desk too: `features/console/
  controls.ts` carries the same shapes, because a reviewer crossing between the two surfaces
  should not have to relearn what a button looks like.
- **Colour still means subject, never verdict.** The five subject hues are an addition to
  Modernist rather than accent steps of it, and none of them sits on a judgment. A grid of
  tiles can be chromatic without becoming a traffic light.

**One exception to the token rule, and it is principled.** Feed-tile text is fixed white over a
fixed dark scrim (`.ew-tile-scrim`) rather than theme tokens, because it sits over a photograph
whose colours nobody controls. It is the one place a literal beats a token, and it is written
as a class so the stops are legible and retune once for every surface that uses a tile.

**The verdict mark is dropped on image tiles**, and that is a deliberate exception to
"geometry carries the verdict" rather than an oversight. The mark is drawn in the ink ramp;
over a scrimmed photograph the ink is invisible, and a white version would be a second mark
meaning the same thing in a different colour. The wording carries it there — which it can,
because on a tile the verdict is a word to read rather than a value to compare down a column.

`tailwind.config.ts` resolves every value to a custom property, so a hex in a component is a
bug: it cannot follow the theme and it is invisible to anyone retuning the system. The known
consequence is that `var()` colours do not support Tailwind's slash-opacity syntax
(`text-ink/50` produces nothing), which is intentional pressure toward the four-step ramp.

---

## 4. Retrieval design

**The scholarly APIs are the search index. pgvector is a re-ranking and caching layer.**
No attempt is made to pre-index PubMed.

### Pass 1 — keyword recall (external APIs)

Per claim, query providers in parallel; each returns candidate papers normalised to a common
`CandidatePaper` shape (title, abstract, year, journal, study type, citation count, PMID, DOI,
URL, source API). Target 20–50 candidates per claim after dedup.

**Identity is a union-find over every identifier a record carries**, not one key per record
(`retrieval/dedup.py`). The same paper routinely arrives from three providers with three
different identifier *subsets* — Europe PMC knows the PMID, Semantic Scholar the DOI, PubMed
both — and a single preferred key cannot unify those: the DOI-only and PMID-only records
produce different keys, survive as two candidates, become two `sources` rows, and reach the
model under two handles, which it cites as two independent findings. One record carrying both
identifiers joins the groups, so a single PubMed hit bridges the other two.

That failure is invisible with PubMed alone (its records carry both), which is exactly why it
is guarded now: it appears the day the other three providers come online, and it appears as
*more* apparent corroboration rather than as an error. `retrieve.duplicates_merged` in the
stage metrics is the number to watch — near-zero cross-provider overlap means matching has
stopped working.

Titles never join two records that carry identifiers. A normalised title is an identity key
only for a record with no identifier at all. Erratum notices and conference abstracts repeat
their parent's title verbatim, and a wrong merge is invisible afterwards because the result
simply looks like one paper: under-merging costs a prompt slot, over-merging silently deletes a
study.

On merge the group keeps the union of identifiers, the strongest study-type classification, the
highest citation count — and the abstract from the **most trusted provider**, not the longest
one. OpenAlex ships an inverted index rather than text, so its reconstruction is frequently
longer than the clean original while carrying tokenisation damage, and that text is both what
gets embedded and what the model reads as evidence.

Provider roles:

| Provider | Role | Notes |
|---|---|---|
| PubMed E-utilities | Primary recall for clinical evidence | MeSH terms + publication-type filters give the cleanest study-type signal |
| Europe PMC | Breadth + open-access full text (the FULL_TEXT stage) | Also the fallback when PubMed rate-limits |
| Semantic Scholar | Citation counts, cross-domain | |
| OpenAlex | Broad coverage backstop | Used to fill gaps, not lead |

Explicitly excluded: Google Scholar and any scraping. SerpApi is reserved as a later fallback
only if genuine coverage gaps appear, and is not in the MVP.

**Pacing and throttles (`retrieval/throttle.py`).** Every provider is built behind a
`ThrottledClient`: a per-provider token bucket, plus bounded retries that honour `Retry-After`.
Both halves are load-bearing for correctness, not just for politeness.

The limiter sits at the *HTTP-call* level rather than around `search()`, because a PubMed
search is two requests — esearch then efetch — and the published ceiling counts requests. It is
per provider per **process**, while every documented limit here is per IP, so the configured
rates in `retrieval/factory.py` sit under the published ones to leave room for a second worker
on the same host. NCBI answers sustained overage by blocking the address, which no retry
recovers from.

The retry half exists because of what a 429 turns into downstream. A throttled provider that
merely fails degrades recall, a thinner evidence base yields a more cautious verdict, and a
cautious verdict is indistinguishable in the finished article from a correct one — the failure
has no visible symptom. PubMed makes this worse by signalling some throttles with **200 OK and
an error string in the body**, which parses cleanly as zero results; `pubmed.detect_throttle`
is passed to the client so those are retried rather than believed.

**A claim where every provider call failed fails the stage** (`StageError`, retryable). This is
the pairing with the zero-source branch in §5: "we searched and found nothing" is publishable,
"we could not search" is not, and the two arrive at synthesis as the same empty list. Coverage
is tracked per claim, not per run — two claims searching cleanly does not license a verdict on
a third that was throttled. `retrieve.rate_limited` is recorded separately from
`provider_failures` because the remedies differ: throttling wants slower pacing, failure wants
a look at the provider.

A consequence worth knowing: Semantic Scholar without an API key is **skipped at construction**
rather than returning an empty list from `search()`. A provider that answers without making a
request counts as a successful search, which would mask a claim whose only real provider was
throttled.

**Retrieval is biased toward reviews at the query layer, not just the ranking layer.** The
PubMed query builder issues a review-filtered query first (`systematic[sb]` /
`meta-analysis[pt]`) and a general query second, so reviews enter the candidate pool even when
they would lose a raw relevance race against fifty primary studies.

### Pass 2 — semantic re-rank (pgvector)

1. Upsert each candidate into `sources` (cache hit → reuse the stored embedding).
2. Embed any new abstracts **in overlapping chunks**: 300-word windows, each repeating the last
   30 words of the one before (`retrieval/chunking.py`), one vector per chunk in
   `source_chunks`. The size is what fits the embedding model's 512-token window, so most
   abstracts are one chunk and are embedded whole; a longer one is split instead of silently
   truncated at the window, which would drop its results. The overlap means a sentence that
   straddles a boundary still appears whole in the next chunk.
3. Embed the claim text.
4. Score each candidate by its **best** chunk's cosine similarity to the claim — one passage
   that answers the claim is enough, and the rest of a long abstract cannot dilute it. The
   model is still shown the whole abstract; chunks exist only for ranking.
5. Apply metadata filters: evidence grade floor, recency window, minimum abstract length.
6. Apply the review bonus, then take top-k (default k=8).

Step 1 happens in RetrieveStage, and **its row ids are carried forward on the context**
(`CachedCandidate` in `domain/contracts.py`) rather than looked up again. RankStage used to
re-upsert the entire candidate set purely to recover them — roughly a hundred statements
against rows written seconds earlier — so it now takes no `SourceCache` at all and reads only.
A model rather than a parallel `dedup_key -> source_id` map, because the map version drops any
candidate missing from it, and a source lost between retrieval and ranking is invisible
downstream: it reads as thinner evidence, and thinner evidence reads as a more careful verdict.

Refreshing matched rows is one `UPDATE ... FROM (VALUES ...)` per batch, not one per row. Warm
is the normal case — the cache exists so a repeat topic re-matches everything — and the
per-row loop made that the most expensive path instead of the cheapest. Measured on 100
candidates: **206 statements per warm run before, 4 after.**

The final score is deliberately not pure cosine similarity:

```
score = best_chunk_cosine_similarity
      + grade_bonus[study_type]     # meta-analysis/SR: +0.15, RCT: +0.08, obs: 0, in-vitro: -0.10
      + recency_bonus               # ≤5y: +0.03, tapering to 0 at 15y
```

The magnitudes are a starting point to be tuned against real output, not a claim of
correctness — they are constants in `retrieval/rerank.py` for exactly that reason. The intent
is that a single recent systematic review outranks several topically-tighter primary studies,
per the brief.

### The `sources` table as a growing library

Once fetched and embedded, a paper is persisted and reused for every future article. The store
becomes a reusable library of cited literature: the tenth ashwagandha article costs almost no
embedding calls. `sources.last_seen_at` supports a later staleness sweep (citation counts drift).

### Classification: what the first live runs corrected

Everything above was designed before a real PubMed response existed. Two of the three defects
the first 92 cached sources exposed were in classification, and both came from one mistake —
`classify_study_type` treated a publication type that was *informative but unmapped* exactly
like *no metadata at all*, and fell through to reading the abstract.

That fallback is actively misleading for precisely the papers that reach it. A **trial
protocol** describes the randomised trial it intends to run, in that vocabulary; three scored
`rct`, whose ceiling is `supported`, on the strength of a study that has reported nothing. A
**narrative review** discusses the trials it surveys; one plainly tagged `Review` scored
`systematic_review`, was cited in a real draft, and set that article's evidence grade. The
function's own docstring promises to prefer the weaker classification when torn. It did the
opposite, and no amount of reasoning about the code surfaced that — only real data did.

Three changes, each narrow on purpose:

- **`NEGATIVE_PUBLICATION_TYPES` suppresses the text fallback.** Tags like `Clinical Trial
  Protocol`, `Editorial` and `Comment` say something real without naming a design, so they end
  classification at `unknown` rather than handing it to prose. Checked *after* the map, because
  a genuine RCT can carry `Comment` alongside its own type and a positive identification must
  outrank a negative one.
- **Protocols are refused at retrieval, not graded down.** A protocol reports no findings, so
  no verdict can honestly rest on it; admitting one can only cost a top-k slot. Dropped in
  `RetrieveStage` before dedup and before the cache, and counted in `protocols_dropped` — a
  number that climbs means the queries are drifting toward planned work.
- **`NARRATIVE_REVIEW` is a real tier**, below observational and above case report, ceiling
  `weak`. Sixteen of the 92 belonged there. Folding them into `UNKNOWN` would give the same
  ceiling, so the value is diagnostic rather than protective: "we retrieved sixteen narrative
  reviews" and "we retrieved sixteen unreadable records" call for opposite responses, and one
  number cannot say both.

The reverse mistake is just as easy and was made once in passing: mapping `Review` straight to
`NARRATIVE_REVIEW` demoted genuine systematic reviews that PubMed had only tagged with the
supertype while their titles said otherwise. So `Review` is a **floor** — narrative unless the
text says systematic review or meta-analysis, and never upgradeable into a trial. Over-trusting
a tag and over-trusting prose are the same failure pointed in different directions.

`scripts/reclassify_sources.py` re-derives grades for rows already cached, because
classification is Python and no migration can express it. Without it a classifier fix applies
only to papers nobody has fetched yet — the opposite of the ones that need it, since the cached
set is what recent articles were built from. It reports protocols already in the cache and
never deletes them: a row may be referenced by `article_sources`, and a published article's
provenance outranks tidying up a grade.

### The evidence quorum

Invariant #3 asked one question for most of this project's life: is the strongest cited source
good enough for this verdict? A single randomised trial answers it perfectly and still leaves
the article overstating, because **one study is a finding, not a conclusion**. Nothing stopped a
lone trial from carrying `supported`, the strongest thing the system can say.

So `supported` now additionally requires **two distinct cited sources at a supported-tier
grade** — RCT, systematic review, or meta-analysis. Two rather than one, and the reason is
local rather than clinical: convention holds that a good meta-analysis suffices on its own, but
that assumes a grade you can trust, and ours is a heuristic that has already been wrong in both
directions on real data (§4 above). A second source means no single misgrading can license a
confident verdict by itself.

Falling short drops the ceiling exactly one step, to `mixed` — "there is real evidence and it
is not settled", which is the honest description of one good trial. Dropping to `weak` would
understate an RCT as badly as `supported` overstates it. Below the top tier nothing changes:
those ceilings already say "provisional", and a quorum there would penalise thin evidence that
is being reported as thin.

**Counted per claim, and the article inherits its weakest claim's ceiling.** Counted
article-wide, two claims each backed by a single trial satisfy a quorum of two between them
while neither is corroborated — and the finished article shows no trace of which source backed
which claim. A claim with no cited source at all caps the article at `weak`; that is the
aggressive reading and it is deliberate, since asserting `supported` about a product while one
of its claims turned up nothing is exactly the overstatement this system exists to prevent.
`no_evidence` remains permissible everywhere: an article may always report finding nothing.

Two consequences worth stating plainly. `PromptSource.claims` exists only to make this
countable, and is **required and non-empty** — defaulted to `[]` it made every draft fail the
quorum while the report pointed at the verdict rather than at the missing attribution.
And `best_evidence_grade`, stored on the row and shown in the console, still records the best
grade cited: the quorum lowers the *ceiling*, not the description of what the article rests on.

What this cannot see: a meta-analysis and a trial it already includes are not independent
sources, and detecting that needs reference lists the abstract-only MVP does not fetch. Sources
are counted after cross-provider dedup, so one paper arriving from three APIs is one source —
that part is solid.

### Retractions: refused at ingest, re-checked on a schedule

A citation is a promise that a study says what we claim it says. A retraction withdraws the
study, and nothing in a cached row notices.

**Measured before anything was written** (2026-08-21), because the alternative was building a
fire brigade for a fire that might not exist: 90/90 cached PMIDs resolved against PubMed
`esummary`, 92/92 DOIs against Crossref `update-to`, **zero flagged**. Verified with a positive
control — PMID 9500320, retracted by *The Lancet* in 2010 — since "none found" from an untested
checker is indistinguishable from a broken one. Base rates say ~0.04 expected in 92 papers, so
this is a smoke alarm, and it was built as one.

**The protection that was assumed to exist did not.** `"retracted publication"` had been in
`NEGATIVE_PUBLICATION_TYPES` since the classifier was corrected, which was believed to cap such
a paper at `unknown`. It did not: `classify_study_type` consults the positive publication-type
map *first* and reaches the negative set only when nothing matched — correct reasoning for
`Comment`, which describes a different aspect of a paper that really is a trial. A retraction is
not another aspect. A retracted trial nearly always also carries `Randomized Controlled Trial`,
so it graded `rct`, ceiling `supported`, and the cap applied to exactly the papers it did not
matter for. This was found by a test written to assert the cap, which found the opposite.
Retractions are now checked **before** the map.

**Three mechanisms, because each covers what the others cannot.**

1. **Refused at ingest.** `RetrieveStage` drops a retracted paper before dedup and before the
   cache, alongside the protocol filter, and for the stronger version of the same argument: a
   protocol reports nothing so it cannot support a claim; a retracted paper *does* report
   something, and the record has repudiated it. A grade cap was never sufficient here — it
   governs how confident the article may sound and leaves the citation in place, so the article
   still says "one trial found X [S3]" pointing at a withdrawn study.
2. **A scheduled sweep** (`scripts/check_retractions.py`, dry by default). This is the half that
   earns the feature. Retractions land *after* publication, so the ingest screen only catches
   papers already retracted when first seen. Default scope is sources cited by a published or
   pending article — a few dozen papers, one batched call — with `--scope all` for the whole
   cache.
3. **Two providers**, because neither covers the corpus. PubMed batches 200 ids per request and
   is the cheap path but indexes only what it indexes; two cached rows have no PMID at all.
   Crossref costs one request per DOI and covers every paper that has one. A positive from
   either wins: two bibliographic databases disagreeing is not a tie to break by precedence,
   because one of them has newer data and it is the one saying yes.

**An unchecked paper is not a clean paper**, and this is the reliability story. `RetractionVerdict`
has three states, not two, and `sources.retraction_checked_at` is written **only** when a
provider actually answered — never on a transport error, a 500, or an id the provider cannot
resolve. Collapsing them records a check that did not happen, which then suppresses the *next*
sweep too, so the error compounds instead of being retried. This is the same failure
`retrieval/throttle.py` exists to prevent one layer down: a search that could not run must never
look like a search that found nothing.

That principle caught a live bug. NCBI does not omit an unresolvable id — it returns a record
carrying `{"error": "cannot get document summary"}` and no `pubtype`, which the first version
read as an empty type list and reported as **verified clean**. Found by a deliberately
unresolvable control in the live probe, not by review.

**A flagged article stays published.** The sweep sets `articles.retraction_flagged_at`, which
raises a banner on the public page and puts the row in front of a reviewer. It does not
withdraw anything. This is invariant #1 read in the direction it actually points:
`ReviewService.approve()` is the only path to `published` because a *human* decides what the
public sees, and an automatic withdrawal is the same machine making the same call in the other
direction. One retracted source among several does not necessarily invalidate a conclusion. A
reader is told before they read; a human decides what to do about it. Anything stronger is a
conversation about invariant #1, not a feature.

Nothing is deleted, either. A retracted source keeps its row: it may be referenced by
`article_sources`, and breaking a published article's provenance to tidy up a citation trades a
real guarantee for a cosmetic one.

### Pass 3 — full-text excerpts (open access only)

Abstracts for breadth, full text for the pivotal few — built 2026-09-29 as the `full_text`
stage, between RANK and SYNTHESIZE (`pipeline/steps/full_text.py`):

1. Ask Europe PMC which ranked papers have an open-access full text (one request per article).
2. Choose up to six. The claims take turns picking their highest-ranked open-access paper, so
   every claim gets depth before any claim gets a second paper.
3. Download each (JATS XML), keep the body sections that report the paper's own work — not
   Introduction or Background, which summarise other papers — and split them with the same
   `chunk_text` as abstracts.
4. Give synthesis the two chunks closest to each paper's claims, under its abstract, as
   "Full-text excerpts".

**Ranking is not touched.** Only about half of cached papers are open access (144 of 309,
measured), and scoring a 20,000-word paper by its best chunk against an abstract's one is a
length bias that would reward open access over good evidence. The verdict cap is untouched too:
it reads study types and counts, never text. The stage never fails a run — without excerpts the
article is written from abstracts, as before — and records why in its metrics. The excerpts
shown are saved on `article_sources.excerpts` and listed in the review desk, because an article
can now quote a number its sources' abstracts do not contain.

Still deferred: full text for paywalled papers, and caching full text. It is fetched per run;
the `source_passages` table sketched in the migration is for when that becomes the bottleneck.

### Provenance

`article_sources(article_id, source_id, claim, citation_handle)` records which paper backed
which claim under which `S`-handle. This is what makes the review UI fast: the sources panel
is one indexed query, and each claim in the editor renders with its backing citations already
attached rather than resolved client-side.

---

## 4b. Topic discovery: the research agent

Two things propose topics, and they answer different questions:

- The **MeSH literature scan** (`app/discovery`, runs every 24 h in the API process) finds
  substances whose *publishing rate* is accelerating.
- The **research agent** (`app/research`, added 2026-09-30, split 2026-10-12) finds what
  *people* are getting interested in, then checks what the literature can responsibly say
  about the best of it.

Both **propose and never enqueue**. A reviewer pressing *Generate draft* is what creates a
pipeline run, and the draft still needs *Approve* to publish (invariant #1). This was an
explicit decision when the research agent was specified as "generate 4–6 articles weekly":
automatic research, human promotion.

### Two halves: the Trend Search Agent, then the evidence check

A research run is one LangGraph workflow in two fragments (`research/graph.py`):

```
TREND SEARCH AGENT (research/trends/) — no scientific evidence anywhere
load_config → collect_sources → normalize_items → cluster_topics → check_history
  → enrich_topics → score_topics → select_trends ──(evidence check wired, nothing failed)──┐
                                                └──→ END (standalone: scripts/trend_search) │
EVIDENCE CHECK (research/evidence.py)                                                       │
research_science → deep_research → select ─(fewer than min, reserve left, round 0)→ backfill
     ↑______________________________________________________________________________|
                                       └→ END
```

The split exists so each number means one thing. A **trend score** means "how much is this
trending" and contains no evidence; a **proposal** on the desk means "trending, *and* there is
literature to write from". Before the split, evidence was 25% of a single score and required
for any ranking, so the two could not be read apart, and every candidate paid for PubMed
counts whether or not it was trending.

- **The trend phase never marks a topic `selected`.** It ranks, and marks the best
  `evidence_pool_size` as `in_evidence_pool`. Only the evidence check's `select` produces
  `selected`, because the desk renders Promote buttons for those, and polls mid-run.
- **The trend output is frozen when its phase ends** (`research_runs.summary`) and served by
  `GET …/runs/{id}/result` as `TrendSearchResult`. Each topic carries
  `scientific_assessment: "not_assessed"` and the evidence check's outcome as a status
  (`downstream.evidence_gate`: passed / failed / not_checked), never as a score.
- **Nothing can switch the evidence check off per run.** A trigger may narrow sources and
  raise thresholds; settings are the floor.

### Who does what

```
n8n weekly schedule ──┐                                 ┌── desk: "Find trending topics"
n8n manual webhook ───┴→ POST /api/automation/research/runs   POST /api/console/research/runs ─┘
                                   (X-Trigger-Token)            (reviewer JWT)
                                          └──────────┬──────────┘
                           research/service.py::start_research_run(mode, params)   ← one entry point
                                                     │  research_runs row, status=queued
                                   pipeline worker claims it (same process as article runs)
                                                     │
                                   research/graph.py — trend search, then evidence check
                                                     │
                            research_candidates  +  research_topic_observations (history)
                                                     │
                     reviewer: Generate draft → _enqueue_run(origin=research) → article pipeline
```

- **n8n decides when, with what, and who hears about it.** The weekly workflow sends
  `RESEARCH_WEEKLY_PARAMS`, polls, fetches `/result` and POSTs a summary to
  `RESEARCH_NOTIFY_URL`; a start that fails for any reason but 409 stops the execution with an
  error. It contains no research logic. Setup is in `n8n/README.md`.
- **The desk button calls the backend directly**, not n8n, so it works when n8n is down.
- **Runs are single-flight**, by a SELECT check and the partial unique index
  `research_runs_one_active` that holds when two triggers race.
- **Research runs execute in the pipeline worker, one thing at a time**, because both kinds of
  run count papers against NCBI's per-IP ceiling through a per-process throttle.

### Sources

One protocol, `TrendSource` (`trends/sources/base.py`): a name, a source type
(search / news / feed / web / video / community), capabilities (discover, search, volume,
interest), and `search(SourceQuery) → SourceBatch` of normalised `SourceItem`s. A source that
could not answer raises `ProviderUnavailable` — `permanent` for a rejected key or a spent
quota — and never returns `[]`. `research/factory.py` is the only place that knows the
implementations; a new source is a class there and a line in the factory.

| Source | Access | What it gives | Limits and decisions |
|---|---|---|---|
| Publisher RSS/Atom (`rss.py`) | none | discovery: headlines per feed, 21 feeds in `trends/feeds.py` | Conditional GETs (ETag / Last-Modified); a 304 replays the stored items. Feeds refusing a reader (NIH, Mayo: 403) are not included. Overridable by `RESEARCH_SOURCES_FILE`. |
| Google Trends (`GoogleTrendsSource` over the existing provider) | none (pytrends, archived) | discovery: rising queries per seed; enrichment: anchored interest series | Relative indices, never search counts. Breaks or throttles when Google changes it; an official alpha API exists but was not generally available. |
| GDELT DOC 2.0 (`gdelt.py`) | none | discovery: article lists per category; enrichment: 3-month daily volume (the baseline) | ≤1 request / 5 s per IP, answers errors as text with status 200, and `{}` is treated as unmeasured (see below). |
| News (`NewsSearchSource` over News API → DuckDuckGo news) | `NEWS_API_KEY` optional | enrichment: articles per topic in two windows | News API's free plan is development-only. |
| Web (`WebSearchSource`: Brave, else ddgs) | `BRAVE_SEARCH_API_KEY` optional | enrichment: pages per topic | Counts toward source *types*, never toward outlets this week: web pages are evergreen. ddgs scrapes DuckDuckGo and is the keyless fallback. |
| Google News RSS (`google_news.py`) | none | discovery and enrichment, the richest free news signal | **Off by default**: the feed says it is "for personal, non-commercial use". Redirect links are never followed. |
| YouTube Data API v3 (`youtube.py`) | `YOUTUBE_API_KEY` | uploads per window, median views per day | ≤10 `search.list` (100 units each) per run; key in a header; lifetime views never used. |
| Reddit, TikTok, Instagram | — | — | **Not implemented.** Reddit's Data API is approval-gated and did not work for this project (an adapter was built, then removed 2026-10-13). TikTok's Research API is limited to non-profit academic researchers. Instagram's Graph API reads owned business accounts, plus a hashtag search under app review. Neither fits without an agreement; the protocol is the seam. |

Every call goes through a per-run budget (`RESEARCH_MAX_REQUESTS_PER_SOURCE`), a phase
deadline (collect 300 s, enrich 240 s), a circuit breaker (three failures in a row), and for
HTTP sources `TransientRetry` over `ThrottledClient`: pacing, `Retry-After`, exponential
backoff with jitter, a per-source minimum wait after a 429, and every retry counted in the
run's provider status.

### Normalising, deduplicating, clustering

- **Items** carry publication and collection time separately, both timezone-aware; a
  timestamp more than an hour in the future is dropped. Descriptions are truncated; article
  bodies are never stored.
- **Stories** (`trends/documents.py`) join items by exact URL, canonical URL (named tracking
  parameters only), headline, and near-duplicate headline (distinct-token Jaccard ≥ 0.8). Thirty
  syndicated copies are one story. **Independent outlets** are counted over stories, by the
  topic's own earliest item in each, among news and feed items only.
- **Topics** come from rising queries, headline *phrases* (1–3 words naming something
  specific, `trends/phrases.py`; one outlet is enough to be *considered*) and manual queries,
  merged by key, then by
  embedding (0.88), ordered by a deterministic pre-score, capped at 75 and triaged by the local
  model in batches of 25 with two example headlines each. Groups the model leaves out are
  asked again once and then set aside.
- **Guards after triage**: items count toward a topic only through queries sharing a word with
  its subject or outcome; a bare category ("protein", "weight loss") is too broad for one
  article; products and shops are shopping intent; a topic filed under an unrequested category
  keeps its seed's category when that was requested.

### The trend score (`trends/scoring.py`, `trend-v1`)

| Dimension | Weight | Sub-signals (each 0–100) |
|---|---:|---|
| Momentum | 30 | Trends interest change (or breakout / rising %); GDELT recent week against its baseline weeks; news counts against the previous window; YouTube uploads; stories against this topic's previous runs |
| Cross-source coverage | 25 | independent outlets `100·(1−2^(−n/4))` (70%); kinds of source with activity, of those that answered (30%) |
| Audience interest | 20 | Trends level against an anchor term; YouTube median views/day; distinct questions asked |
| Freshness | 15 | median age of dated items; newness against earlier runs; novelty against articles already written |
| Editorial fit | 10 | share of items on wellness; a specific subject; an outcome (an angle) |

- **Missing data: reweight, with a floor.** A dimension with nothing measured is left out and
  the rest renormalised. `data_coverage` is the measured share of the whole score, counted per
  sub-signal, and only over sub-signals whose source this deployment has. Below 0.6, or with no
  momentum measured at all, a topic gets **no score** ("insufficient data") rather than a score
  built on what remained.
- **Reproducible.** `score_from_metrics(TopicMetrics, weights)` reads nothing else, and both
  are stored on the candidate and in the trend history.
- **Explanations are templates over measured numbers** (`trends/explain.py`): a topic's
  summary, selection reason and limitations contain a clause only when its number was
  measured. No model writes them.
- **Status** is emerging / accelerating / steady / declining from observations and momentum,
  and **preliminary** whenever nothing compared two periods.
- **Selection** requires a score, ≥ `research_min_trend_score`, measured coverage with ≥2
  independent outlets, one topic per subject and at most two per category, and returns fewer
  than asked rather than padding. A topic proposed in the last 28 days is held back unless its
  stories doubled or its momentum is ≥ 65.

### History

`research_topics` is a topic's identity across runs (`subject|outcome`, normalised, with an
embedding match for drifted wording); `research_topic_observations` is one row per topic per
run, written once when the run finishes, upserted, and pruned after 180 days. Counts of how
often a topic was seen or proposed are derived from observations, never incremented.

### The evidence check

Unchanged in substance, run on the trend pool only:

- `research_science` counts PubMed and Europe PMC (each the other's fallback);
- `deep_research` runs the article pipeline's own extraction and `TemplateQueryStrategy`,
  discarding `UnanchoredQuery` topics;
- `select` applies `research_min_evidence_status` (default `emerging`), ranks by trend score
  with evidence as a tie-break, and one backfill round sends the next trend-ranked topics
  through the whole check.

**Popularity is never evidence**: a breakout with one paper behind it is discarded however
it trends, and the tests pin exactly that case.

### Failure behaviour

| Failure | Behaviour |
|---|---|
| Every discovery source fails | Run fails `no_trend_data`. Nothing is invented. |
| Some sources fail | Their sub-signals are unmeasured and the topic's data coverage drops; provider status `partial` / `failed`. |
| A source refuses for good (401/403, spent quota) or fails three times in a row | Not asked again this run. |
| A source runs out of budget or time | `skipped` / `partial` with the reason; unmeasured, not zero. |
| Triage fails outright | Raw queries are used, and the run's notes say so. |
| Triage leaves groups out | Asked again once; still left out, the group is set aside. |
| PubMed down, Europe PMC up (or the reverse) | The other source's counts are used. |
| Both literature sources down | Run fails `scientific_evidence_unavailable`; the frozen trend output is still served (`partial`). |
| Worker dies mid-run | The sweep marks the run `failed` (not requeued); the next trigger starts fresh. |

### Measured on the live runs (2026-10-12)

The first full-breadth trend run (every category; RSS, Google Trends, GDELT, news, web)
took 10.6 minutes and ranked 10 topics. Four of the top six were things a reviewer would
reject at a glance, and each became a guard:

- **"soulja boy running for president"** ranked fourth: the seed "running" made it look on
  topic, and triage had left its group out (16 of 75), so it ran on its raw query. Left-out
  groups are now asked again, then set aside.
- **"protein", "strength training", "weight loss"** ranked: categories, not angles. A bare
  subject that is a seed or only generic words is now too broad for one article.
- **"running shoes"** ranked second: a product. Product and shop words are shopping intent.
- **"coneflower for immune support"** cited two articles about carbohydrates: triage had
  grouped a carbs phrase into it. Items now count only through queries sharing a word with
  the subject or outcome.

**Exact phrases rarely recur across outlets.** A week of 356 feed stories from 12+ outlets
had no specific wellness term in two outlets' headlines, so a two-outlet floor at discovery
found no topics at all. Discovery now considers one outlet's specific term, and the
cross-source rule is applied at selection, after enrichment has searched each source.

**End to end through the worker** (2026-10-13, default run): 6.4 minutes, 448 items, 12
candidates enriched, 7 evidence-checked, 5 selected, all with linked sources.

**GDELT** refused nearly every request from the development network (429 with no
`Retry-After`, even 12 s apart) and once answered `{}` for "creatine" over a week while Google
News listed 60 articles. `{}` is therefore unmeasured, and GDELT's failure costs a momentum
sub-signal rather than the run. **Google News RSS** answered every request; it is off by
default for its terms, not its reliability.

### Measured on the first live runs (2026-09-30), before the split

**Google Trends' *rising* lists can be spam.** For "fitness" and "protein" over seven days,
the rising list was mostly unrelated commercial queries at +7,000% or more: "learn golang",
"online banking review", "cheap flights new york". The *top* lists were sane (planet fitness,
whey protein), and "sleep" rising was real.

The first run, before any defence, selected four product-review queries out of five. Three
layers now stand between that and the desk:

1. **Deterministic.** A query must share a word with its seed or with `WELLNESS_TERMS`, and
   shopping, job and litigation words make it a non-topic. `review` and `comparison` are
   shopping intent.
2. **The triage model.** It works in batches of 25; a topic spanning more than 6 groups, or
   with a subject like "various", is refused. Its output is then deduplicated across batches
   (same topic, or the same subject and outcome), and a query equal to its own seed is
   dropped. The second run had proposed "gut health" and "Gut Health" as two topics, and
   "sleep" as a topic under the seed "sleep".
3. **The evidence gate.**

The same run showed why the first layer cannot be the evidence gate. Deep research gave
"online banking review" *moderate* evidence: the extraction model invented plausible claims,
and the anchored query found 14 papers. Any query can find some literature.

### Assumptions

- Google Trends is reached by scraping. It will break or throttle, and the source interface
  is the mitigation. SerpApi (paid) or Google's Trends API, once generally available, are the
  likeliest replacements.
- The free news signals are thin without Google News (off by terms) and GDELT (rate-limited).
  A deployment that can license a news API will see far more outlets per topic.
- A promoted research topic runs through the unchanged article pipeline with
  `origin = research`. Its canonical topic string is what extraction reads.

## 5. The AI contracts

Both calls are typed end to end: a Pydantic input model, a Pydantic output model used as the
structured-output schema, and a deterministic validator downstream.

### Call 1 — claim + ingredient extraction

Input: product/trend name + optional marketing blurb.
Output (`ExtractionOutput`): `product`, `target_claims[]`, `ingredients[]`.

Cheap, low-risk, no retrieval involved. The only failure mode worth guarding is the model
inventing claims the blurb never made, so the prompt forbids inferring unstated claims and the
schema caps `target_claims` at 6.

### Query generation (swappable step)

`retrieval/query_builder.py` exposes a `QueryStrategy` protocol. The MVP ships
`TemplateQueryStrategy` (claim + substance → MeSH-aware boolean query) because it is
deterministic, free, and debuggable. An `LLMQueryStrategy` implementing the same protocol can
be dropped in without touching the pipeline.

`build()` takes `product` and `ingredients` as **separate** arguments rather than one merged
list, because they are not interchangeable: `ingredients` names the actives and is optional,
`product` is required and may be a brand. A strategy prefers the ingredient list when it has
one and falls back to the product when it does not — merging them would OR a dead brand term
into every query that already names its actives. Product names carry packaging ("Creatine
Monohydrate Powder"), so the MeSH lookup also matches a known substance appearing as a whole
word inside a longer name; without that the fallback quotes the whole string and retrieves
nothing, which reads as `no_evidence` and is the *more* dangerous failure, since that verdict
is publishable.

Raising `UnanchoredQuery` is part of the protocol, not an implementation detail —
`LLMQueryStrategy` will need the same guarantee.

### Call 2 — synthesis (the RAG generation)

Input: product + claims, top-k abstracts each tagged `S1..Sn` with year and study type, and a
strict system prompt. Output (`SynthesisOutput`): `headline`, `verdict`, `summary`,
`body` (three beats plus 0–5 sections), `citations[]`.

The system prompt enforces, in order of importance: use only the provided sources; attach a
source id to every factual claim; say plainly when evidence is weak or absent; never use
outside knowledge; let study-type grade cap verdict confidence; never name or attack brands;
never give medical advice.

**Grounding rule, enforced in code.** After generation, `validate_draft()` checks:

1. Every `S`-handle in `body` and in `citations[]` exists in the prompt's handle set.
2. Every referenced source resolves to a row with a non-null PMID or DOI.
3. Beat 2 and **every section** contain at least one citation, unless the verdict is
   `no evidence`. Beats 1 and 3 are exempt: beat 1 restates the product's own marketing claim
   and beat 3 is an interpretive bottom line, so requiring citations there encourages
   decorative citation, which is worse than none. Sections are *not* exempt — one is up to
   eight sentences and there may be five, so exempting them would turn a deliberate
   three-sentence allowance into most of the article.
4. The verdict does not exceed `max_verdict_for_grade(best_grade_among_cited)`.
5. Field bounds (§6) hold.

Any failure → `validation_failed` with a structured reason. The draft does not enter the queue.
Failures are visible in the console under a separate tab, because a persistent validation
failure is a prompt bug you want to see, not silence.

**One bounded rewrite comes first (2026-09-30).** The graph sends a failed draft back to
SYNTHESIZE **once** (`MAX_REVISE_ROUNDS`), with the failures fed back verbatim and the verdict
ceiling stated. It uses the same `synthesis_input`, so VALIDATE still checks against the exact
handle set the model saw.

- **Which failures qualify.** Only fixable ones: `hallucinated_handle`, `uncited_beat`,
  `uncited_section`, `verdict_exceeds_grade` and `malformed_body`. `unresolvable_source` is
  ours to fix, not the model's.
- **The rewrite is validated like any draft.** A second failure is stored `validation_failed`
  exactly as before. A rewrite that errors keeps the first draft, which then fails again the
  same way. The invariant is unchanged: a failing draft never reaches the queue.
- **ILLUSTRATE keeps the pictures already drawn.** They are claim-free, so the rewrite cannot
  change what they should show, and redrawing would bill again.
- **Stage-row ordinals stay unique.** They are offset by `refine_round + revise_round`.

### Call 2, zero-source branch — the `no evidence` article

When ranking yields no usable sources, **no generative call is made at all**. `SynthesizeStage`
branches to `services/no_evidence.py`, which assembles the `SynthesisOutput` deterministically
from the extraction output: what the marketing claims, that a search found nothing meeting our
evidence criteria, and that absent evidence is not contrary evidence.

A model handed a product name and zero abstracts is exactly the ungrounded generation the rest
of the pipeline exists to prevent — and it would emit no citations, so `validate_draft()` would
have nothing to check and invariant #2 would pass by vacuum rather than by verification. The
template cannot hallucinate, costs nothing, and is the one article shape where determinism
loses no quality.

The draft then takes the ordinary path and passes validation **on its merits, not by
exemption**: the emitted handle set is empty so checks 1–2 have nothing to reject,
`no_evidence` is exempt from the cited-beat rule, and `best_grade([])` is `UNKNOWN`, whose
ceiling of `weak` sits above `no_evidence`. It reaches the queue as `0/0 citations resolve` and
a human still approves it.

Two causes reach this branch and are identical to a reader but not to us — nothing retrieved
(a genuinely unstudied trend) versus candidates retrieved and then filtered away by `min_year`
or the grade floor. `pipeline_stage_runs.metrics.no_evidence_cause` distinguishes them, because
the second is a signal that the filters are tighter than the corpus can satisfy.

---

## 6. Article format

Primary output is the on-tap article: a three-to-five minute read, ~700–950 words where the
evidence supports it. A three-beat spine — (1) what it claims → (2) what the research shows →
(3) bottom line / caveat — with up to five titled **sections** between beats 2 and 3 working
through the evidence in detail.

The spine stayed three named fields when sections arrived rather than becoming a list, because
three things address the beats by name: `derive_card` reads beat 1 for the feed excerpt,
`check_beats_are_cited` reads beat 2, and `PersistStage` places the generated lead image above
beat 1. Section paragraphs carry no `attrs.beat` — a section is a sibling of the beats, exactly
as a reviewer-added paragraph is, and must not answer to `beat_text`.

**Length is a ceiling, not a floor, and this is where that is easiest to lose.** Sections have
**no minimum**. A `min_length` would be a padding instruction: an article resting on one small
trial would be obliged to invent two more things to say about it. Thin evidence should still
produce a short honest article — one or two sources means no sections at all, and the beats
alone. The prompt states the shape by evidence volume; nothing enforces a floor, and nothing
should.

Bounds are enforced structurally, per field, not as a global word count:

| Field | Bound | Enforced |
|---|---|---|
| `headline` | ≤ 12 words | Pydantic validator |
| `summary` | ≤ 2 sentences | Pydantic validator |
| `body.beat_1_claim` | ≤ 4 sentences | Pydantic validator |
| `body.beat_2_evidence` | ≤ 5 sentences | Pydantic validator |
| `body.beat_3_bottom_line` | ≤ 5 sentences | Pydantic validator |
| `body.sections` | 0–5 sections | Pydantic `max_length` |
| `body.sections[].heading` | ≤ 8 words | Pydantic validator |
| `body.sections[].body` | ≤ 8 sentences | Pydantic validator |
| `verdict` | one label + optional one-clause qualifier | enum + ≤ 15-word qualifier |

A section's `body` may hold blank-line-separated paragraphs; `tiptap.py` splits them into
sibling paragraph nodes. Its `heading` renders as an `h2` and is a **label, never a claim** —
"Dose and duration", not "Magnesium improves sleep" — so a heading cannot assert more than the
sources support, and citation markers are not allowed in one.

Every article renders with an "informational, not medical advice" disclaimer. It is a
render-time constant in a shared layout component, not model output — the model cannot forget
it, reword it, or drop it.

### Register: written for a phone, not a journal

Two rules in the synthesis prompt's Framing section, both aimed at a reader with no medical
training who is using the app between other things.

**No em dashes or en dashes**, in any field including the headline and section headings. They
read as academic, and a comma, colon, full stop or bracket always serves. Ranges go in words
("four to eight weeks").

**Every technical term and acronym is explained the first time it appears**, either in brackets
straight after it or in one or two short sentences, with the words spelled out before the short
form is used ("randomised controlled trial (RCT)", then "RCT"). A term needing more than two
sentences does not belong in the article at all; say the finding in ordinary words instead.

**What made the dash rule stick is worth knowing before editing this file, because the obvious
approach failed.** The prompt itself contained 18 em dashes while instructing against them, and
a local model mimics the register it is handed far more reliably than it follows a rule about
it. Removing them from the prompt took em dashes in output to **0 of 6 runs**. The same lever is
why `STUDY_TYPE_LABELS` carries a plain-English gloss on every entry rather than a bare label:
telling the model to explain "meta-analysis" did nothing, because the bare term was printed on
every source line it read. Do not "tidy" those glosses back to bare labels, and do not
reintroduce an em dash here.

Measured on `llama3.1:8b`, six runs, the same 12 sources: dashes eliminated outright, and the
glossed labels lifted inline citations from 3.2 to 5.0 handles per article with section counts
holding. **The term-explanation rule only half-took**: "RCT" is now usually spelled out, but
"systematic review" and "meta-analysis" still appear unglossed in most runs. That is the local
model's instruction-following ceiling, not a missing rule — the prompt is at ~1,500 words and
additions past this point measurably trade against each other (an earlier four-line addition
about `summary` halved section output). Reach for a larger `OLLAMA_SYNTHESIS_MODEL` before
reaching for more prompt.

---

## 7. Human-in-the-loop review

States: `pending_review` → `published` | `rejected`, plus `validation_failed` as a terminal
pre-queue state and `draft_failed` for pipeline errors.

`validation_failed` → `rejected` is also allowed, and it is the console's "discard" action.
Since there is no delete, rejection is the only way a draft leaves a queue tab; without this
edge, failed drafts have no available action at all and accumulate forever in the tab that
exists to make prompt regressions visible. They are still never approvable — the discard path
records a reason, it does not route around invariant #2.

The review screen is the editor and the sources panel side by side:

- **Sources panel** lists each paper's title, journal, year, study type, and a direct DOI/PMID
  link, grouped by the claim it backs. Weak study types are flagged inline, so "this confident
  verdict rests on two cell-culture studies" is visible without opening anything.
- **Validation badge** shows `4/4 citations resolve` — computed at draft time and stored on
  the article, not recomputed in the browser.
- **Editor** is TipTap with autosave (debounced 800ms) writing to `edited_content`.
  `original_content` is never touched, giving a clean audit trail of what the model wrote
  versus what went live.
- **Subject picker** is the one field a reviewer *adds* rather than checks. It sits above the
  decision block, not inside it: it is metadata that stays editable after publication, and
  grouping it with Approve would imply it is part of the irreversible act.

Publishing sets status, `reviewed_by`, `reviewed_at`, `published_at`, and materialises the
derived card fields — headline, excerpt and image — in the same transaction. The public feed
only ever queries `status = 'published'`.

**The publish control names its destination.** It reads "Publish to the public feed", says the
article will land at `/a/{slug}` recorded against the reviewer's name, and links there once it
is live. This is the only path to `published` in the system and it puts the article in front of
every reader; a button whose label is an abstract state change understates what is being
decided.

---

## 8. API surface

Full request/response models are in `backend/app/api/*/schemas.py`. Summary:

### Public (unauthenticated, read-only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/feed` | Cursor-paginated published cards. `?cursor=&limit=&verdict=&subject=&personalise=` |
| `GET` | `/api/feed/facets` | Published counts per subject and per verdict, for the browse drawer |
| `GET` | `/api/articles/{slug}` | Full article + citations + sources |
| `POST` | `/api/contact` | "Let us know" — 202, rate limited, write-only |
| `GET` | `/api/healthz` | Liveness |

`/api/feed` takes an **optional** reader token. Signed in, the reader's interests add a leading
sort tier; signed out — or with a stale token — it serves the anonymous feed rather than a 401.
`personalise=false` is how a signed-in reader asks for the plain chronological order without
signing out.

`facets` returns **sparse** maps: a key is absent when nothing is published under it, so a
client must default to zero rather than assume the enum is populated. They also do not sum to
`total` — an unclassified article is counted in the total and in no subject, which is why
"Everything" is genuinely larger than the five categories added up.

### Reader accounts (bearer token, own data only)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/readers/signup` | Create an account, its first folder, and a session |
| `POST` | `/api/readers/login` | Email + password → access token |
| `GET` | `/api/readers/me` | The caller's own account; also re-validates a stored token |
| `PATCH` | `/api/readers/me` | Display name, interests, newsletter. Omitted field = unchanged |
| `GET` `POST` | `/api/readers/folders` | List / create |
| `DELETE` | `/api/readers/folders/{id}` | Remove a folder and its saves. Refuses the last one |
| `GET` | `/api/readers/saved` | The shelf plus its folder tabs, in one request |
| `GET` | `/api/readers/saved/slugs` | `{slug: folderId}` — one request for a page of tiles |
| `PUT` `DELETE` | `/api/readers/saved/{slug}` | Save (idempotent; re-saving *moves*) / unsave |

Every route here either creates a session or operates on the caller's own row. There is no
listing endpoint, no lookup by id, and nothing that can see another reader. `PATCH /me` has
patch semantics with one consequence worth stating: **`interests: []` is a real instruction**
— turn personalisation off — and is distinct from omitting the field, which is the only way
that switch can be thrown back.

`saved/slugs` is deliberately *not* folded into the feed response. The feed is public,
identical for everyone and cacheable; a per-reader field in it would make it none of those.

### Console (JWT required)

**The API prefix is still `/api/console`, deliberately.** The *frontend* route moved from
`/console` to `/review`; the API did not. This is the reviewer API and renaming it would churn
every handler, schema, client method and test to change a string no reader ever sees. Read
"console" here as the name of the authenticated surface, not of a page.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/console/auth/login` | Email + password → access token |
| `GET` | `/api/console/auth/me` | Current reviewer |
| `GET` | `/api/console/articles` | Queue. `?status=pending_review&cursor=&limit=` |
| `GET` | `/api/console/articles/{id}` | Draft + sources + validation report |
| `PATCH` | `/api/console/articles/{id}/content` | Autosave into `edited_content` |
| `PATCH` | `/api/console/articles/{id}/subject` | Classify what the article assesses; nullable |
| `POST` | `/api/console/articles/{id}/approve` | → `published` |
| `POST` | `/api/console/articles/{id}/reject` | → `rejected` (reason required); also the discard path from `validation_failed` |
| `POST` | `/api/console/pipeline/runs` | Enqueue a topic |
| `GET` | `/api/console/pipeline/runs` | Run history + per-stage status |
| `GET` | `/api/console/pipeline/runs/{id}` | One run, all stages, errors, token cost |
| `GET` | `/api/console/contact` | The "Let us know" inbox. `?status=&limit=` |
| `PATCH` | `/api/console/contact/{id}` | Mark answered / closed, or reopen |
| `POST` | `/api/console/research/runs` | Start a research run (§4b); 202, 409 while one is active |
| `GET` | `/api/console/research/runs` | Recent research runs |
| `GET` | `/api/console/research/runs/{id}` | One run with every candidate, its signals and scores |
| `POST` | `/api/console/research/candidates/{id}/promote` | Selected or shortlisted topic → queued pipeline run |
| `POST` | `/api/console/research/candidates/{id}/dismiss` | Say no, with a reason |

**Automation (shared secret, `X-Trigger-Token`).** For n8n. The whole router answers 404 while
`RESEARCH_TRIGGER_TOKEN` is unset.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/automation/research/runs` | Start a research run; `mode` is `weekly` (default) or `manual` |
| `GET` | `/api/automation/research/runs/{id}` | Status for n8n's polling loop |

`subject` is a separate route from the content autosave, and is legal **after** publication —
unlike the body — because it drives a colour and a browse listing rather than a word the reader
was shown. Getting it wrong should be correctable without touching the article.

The contact inbox is reviewer-only reading of a publicly-written table: the rows carry an email
address and free text from anyone with the URL, so nothing is ever echoed publicly — no
"recently requested" list, no counts — and the console renders note and link as text, never as
markup. A submission also **does not create a pipeline run**. Nothing reaches generation because
a stranger asked for it, which is invariant #1's principle applied at the other end of the
pipeline.

Two deliberate omissions: no `DELETE` on articles (rejection is a state, and the audit trail is
the point), and no endpoint that can set `status = published` other than `approve`.

---

## 9. Data model

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
- **`contact_requests`** — `kind`, `email`, `link`, `note`, `status`, `handled_by`,
  `handled_at`. Written unauthenticated, read only in the console.

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

`contact_requests` carries a CHECK in the same shape as the one behind invariant #1: a row
whose status claims a human dealt with it has to name the human, so reopening a request must
clear `handled_by` and `handled_at` or the row is refused.

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

### How much is stored, and for how long

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

---

## 10. Infrastructure: local now, AWS later

**Now:** Docker Compose (Postgres 16 + pgvector), FastAPI via uvicorn, worker as a separate
process, Vite dev server. Everything runs on one machine, and with the Ollama defaults, with
no API keys at all.

**The API is containerised; the worker and the migrations are not.** `backend/Dockerfile` is a
two-stage build — toolchain and a venv in the builder, `libpq5` and a non-root `evidwell` user
in the runtime — and `docker compose up backend` serves it on :8000 against the `db` service.
What it deliberately leaves out is as much of the design as what it includes:

- **`scripts/` is excluded from the image** (`.dockerignore`), so nothing in the container can
  apply a migration, while `migrations/` *is* copied in for reference. Schema changes are a
  deliberate act against a specific database, not something a container does to whatever it
  finds at boot — an image that migrates on start will eventually be scaled to two replicas and
  race itself, and in the target topology the thing being migrated is RDS. Migrate and seed
  from the host venv.
- **Compose runs no worker service.** A container that both serves and generates would tie the
  two lifecycles together, and they are already separate processes precisely because the
  pipeline is going to become Step Functions rather than a scaled copy of the API. The API in
  Docker with `python -m app.pipeline.runner` on the host is the honest shape of the target.
- **One uvicorn worker, scaled by replicas rather than pre-fork.** Each worker holds its own
  asyncpg pool, so pre-fork multiplies connections against the database's fixed budget while
  replicas at least make that count visible. It also keeps the `LoginThrottle` caveat (§3.2)
  from getting quietly worse: in-process state means N processes is N× the budget, and one
  replica per container keeps that ratio legible.
- **No media volume, of any kind.** Image bytes are rows in `media_objects`, so they arrive
  with the database and leave with it. This was a named volume, and that is exactly how the
  problem was found: a volume is its own storage, so images written by a host-venv run were
  404s inside the container while `articles.generated_imagery` still pointed at every one of
  them. §3.4b's point was that these are live article assets rather than a cache — the fix for
  which is to stop having a second place they can be lost from.

**Target, designed for but not provisioned:**

| Local | Production |
|---|---|
| Worker process | Step Functions state machine, one state per pipeline stage |
| Manual `POST /pipeline/runs` | EventBridge schedule → Step Functions |
| Stage function call | Lambda invoke (each stage is already a self-contained function) |
| Local Postgres | RDS or Neon, pgvector extension enabled |
| Local filesystem | S3 for card images and assets |
| `.env` | Secrets Manager |

The migration is a transport change because stages already communicate through a serialisable
`PipelineContext` rather than shared memory. Retrieval fan-out becomes a Step Functions `Map`
state. Long-running synthesis stays under Lambda's 15-minute ceiling comfortably.

Not provisioned in this task. No Terraform, no CDK, no AWS account touched.

---

## 11. Phased roadmap

The organising principle: **one topic end-to-end before any breadth.** Each phase ends with
something demonstrable.

### Phase 0 — foundations (½ day)
Compose file, Postgres + pgvector up, migration applied, FastAPI boots, `/healthz` green,
Vite dev server renders an empty feed. No AI.

### Phase 1 — the vertical slice, one topic, hand-driven
Target: `"ashwagandha for stress"` produces one validated draft.
1. `sources` upsert + cache, embedding provider wired.
2. PubMed provider only — one API, learn its shape properly.
3. Extraction call (LLM 1).
4. Semantic re-rank against the claim.
5. Synthesis call (LLM 2) with the real prompt.
6. Citation validation. **Deliberately try to make it fail** — hand-edit a source id in a
   fixture and confirm the draft is rejected. Invariant #2 is untested until you have watched
   it reject something.
7. Persist as `pending_review`.

Exit criterion: one row in `articles` with `status = pending_review` and `4/4` citations
resolving, produced end to end.

### Phase 2 — the console
Auth, review queue, article detail with sources panel, TipTap editor with autosave, approve
and reject. Exit criterion: that draft is human-approved and reaches `published`.

### Phase 3 — the public feed
Masonry feed of published articles, card → article route, citation rendering, disclaimer.
Exit criterion: the approved article is visible and readable by someone who isn't you.

**At this point the whole loop works for one topic. Only now scale breadth.**

### Phase 4 — retrieval breadth
Europe PMC, Semantic Scholar, OpenAlex. Cross-provider dedup. Study-type classification
tuning. Review-bias tuning against real output. Exit criterion: five topics of varying
evidence strength produce appropriately different verdicts — and at least one correctly says
"not enough evidence."

### Phase 5 — pipeline hardening
Retries, rate-limit handling, per-stage transaction boundaries and heartbeat-based crash
recovery are done (§3.3, §4) — they were pulled forward because each of them fails *silently*
rather than loudly, and a silent failure in this system reads as a confident answer. What
remains: `pipeline_runs` UI (including `attempts`, `nextAttemptAt` and `heartbeatAt`, so a run
in backoff does not read as a stalled worker), batch topic submission, and putting
`scripts/check_retractions.py` on a schedule — it exists and is dry-by-default, but a sweep
nobody runs is the same as no sweep, and that is the whole feature (§4). Cost tracking is
done (§3.7) — tokens are recorded per stage per attempt with the model that spent them, and
the API returns an estimate. It was pulled forward ahead of the hosted-provider switch on the
same reasoning as the rest of this phase: instrumentation added *after* the switch has no
before-picture to compare against, and the saving from prompt caching is exactly the kind of
quantity that is assumed rather than measured.

### Phase 6 — production
AWS migration per §10, S3 assets, managed Postgres, EventBridge schedule. The API image exists
(§10) and is the one piece pulled forward, because it is also what pins down the runtime
contract — non-root, no migration on boot, one worker per container. Still outstanding before
anything is deployable: a migration step that runs as its own task rather than at container
start, `--proxy-headers --forwarded-allow-ips` once a balancer is in front (§3.2 — until then
the login throttle must keep reading the socket peer), and secrets from somewhere other than an
`.env` file mounted into compose.

### Later, explicitly deferred
Full text for paywalled papers, and caching full-text passages (§4, Pass 3); *model-generated* query refinement (the deterministic
broadening loop shipped 2026-09-07, see §3.2); multi-reviewer roles and invites; SerpApi
fallback; Next.js port if SEO becomes a priority.

---

## 12. Known risks

| Risk | Mitigation |
|---|---|
| Study-type metadata is inconsistent across providers | Classify from publication type + title/abstract heuristics in `evidence/grading.py`; store both raw and normalised. Unknown grades are treated as the *weakest* tier, so uncertainty caps confidence rather than inflating it. |
| Abstract heuristics read the *surveyed* literature as the paper's own design | Measured on real data, not anticipated: a trial protocol and a narrative review both describe randomised trials at length, and three protocols scored `rct` (ceiling `supported`) on that basis. The text fallback now runs only when the publication types are genuinely uninformative — `NEGATIVE_PUBLICATION_TYPES` suppresses it — and a bare `Review` may be sharpened only within the review family, never into a trial. |
| A grounded draft can still be misleading by omission | Human review is the backstop. The sources panel shows what was retrieved, not just what was cited, so a reviewer can see when something relevant was left out. |
| Abstracts overstate findings relative to full text | Partly addressed: excerpts from up to six open-access papers per article (§4, Pass 3). Paywalled papers stay abstract-only; grade caps still mitigate. |
| Retrieval finds nothing for a fringe trend | `no evidence` is a first-class verdict with its own short-article path, not an error. |
| PubMed rate limits | Per-provider token bucket keeps us under the ceiling; bounded retries honour `Retry-After`; the `sources` cache absorbs repeat topics; an API key raises the ceiling to 10 req/s. Europe PMC searches beside PubMed since 2026-10-01, scoped to title and abstract. Its coarser study-type tags cannot lower a paper PubMed also found, because a merged record keeps the strongest study type any provider gave. A paper only Europe PMC finds may grade lower than PubMed would have graded it. |
| A throttled search reads as "no literature exists" | The failure with no visible symptom, and the reason §4's pacing exists. Throttles are retried rather than parsed (including PubMed's 200-with-error-body form), and a claim whose every provider call failed fails the stage instead of reaching the `no evidence` path. |
| A killed worker leaves a run claiming to be `running` | `heartbeat_at` is pinged on a timer while the run is in flight, and a periodic sweep recovers rows that have gone quiet (§3.3). Recovery spends the run's attempt budget, so a run that kills every worker terminates instead of cycling. |
| An unauthenticated endpoint that runs Argon2 is a CPU exhaustion vector | Measured: 31ms per junk login, so ~32 req/s saturates a core. `login_throttle` rejects before the lookup and before any hashing, at 0.35µs — 88,000× cheaper than the hash avoided (§3.2). |
| Rate limiting the login locks out the only reviewers | Blocks escalate but cap at 30 minutes and always expire on their own; a successful login clears both counters. Nothing requires an admin to clear, which matters because the API has no user-management endpoint. |
| A cited paper is retracted after we publish | Refused at ingest, and re-checked on a schedule against PubMed and Crossref (§4). A flagged article raises a reader-facing banner and a console row rather than being withdrawn automatically — one retracted source among several need not invalidate a conclusion, and the human decides. |
| A retraction check that could not run reads as a clean bill of health | `RetractionVerdict` has three states and `retraction_checked_at` is written only when a provider actually answered. This caught a real bug: NCBI returns an `error` record rather than omitting an unresolvable id, and reading it as an empty publication-type list marked unlookuppable papers verified clean. |
| A model call that fails is billed and reported as free | Truncation spends the whole 8K synthesis budget and returns nothing, so the most expensive failure would look like the cheapest run. `LLMError` carries its usage, the stage records it before raising, and the ledger write survives the rollback that discards the stage's own writes (§3.7). |
| An unpriced model reads as a free one | A price lookup miss returns `null`, never `0`, and a run total is `null` if any consuming stage is unpriced. Model ids are provider-namespaced so a genuinely free local model stays distinguishable from one nobody has priced. A test asserts the clients' default models are all in the table, which is the likeliest way it goes stale. |
| A trending topic has popularity but no evidence | The evidence gate discards it whatever its trend score (§4b). Trend data decides *what* to consider; PubMed and Europe PMC decide what can be proposed. |
| Google Trends' rising lists carry spam | Measured on the first live run (§4b). A deterministic on-topic filter runs before the model, and the triage model is batched and cannot build catch-all topics. |
| Google Trends access breaks (unofficial scraping) | A run fails `no_trend_data` rather than inventing topics; the provider sits behind `TrendProvider`, so a paid source is a one-file swap. |
| Recovery requeues a run that is merely slow, and it executes twice | Why staleness is measured from the heartbeat rather than from `started_at`: a live run is never quiet, at any duration. The heartbeat never raises, the staleness window is validated at 3× the ping interval, and the sweep runs between polls so a worker cannot sweep its own run. |
