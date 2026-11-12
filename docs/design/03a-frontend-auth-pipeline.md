# 3. Decisions made, with reasoning — §3.1, §3.2, §3.3


## 3.1 Vite SPA, shaped for a Next.js port (your call, recorded)

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
it is.** `GET /feed?q=` (`0001_initial.sql`, `FeedService.search`). It was client-side over loaded
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

## 3.2 Auth: two kinds of account, minimal, with a real audit trail

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

## 3.3 Pipeline: local worker with a `pipeline_runs` table

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
