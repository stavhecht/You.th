# Backend

Configuration traps and what has met a real response. Area notes live in nested CLAUDE.md files (see the root map).

Loaded when you work under this directory; the root [CLAUDE.md](../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/10-infrastructure.md](../docs/design/10-infrastructure.md).

## Config traps

- **`.env` is read from two places, and compose must name both.** `config.py`'s
  `env_file` is `(".env", "../.env")` *relative to the process's cwd*, so a host venv
  running from `backend/` picks up `backend/.env` **and the repo-root `.env`** — and this
  project's keys live at the root. Compose named only `./backend/.env`, which does not
  exist, so every container booted with none of them. Nothing errored, because every
  setting has a working default: imagery went `not_configured`, `EMBEDDING_PROVIDER` fell
  back to `ollama`, and `ENABLED_PROVIDERS` quietly dropped OpenAlex. Both paths are listed
  now, both optional. **When a value looks wrong in a container, compare it against the
  file before debugging the code** — and compare it by *hash*, never by printing it.
- **Editing `.env` does nothing until the containers are recreated.** Docker resolves
  `env_file` at container **creation**, so `docker compose restart` re-runs the process with
  the old environment still baked in. After rotating a key this presents as a `401
  Unauthorized` from a provider against a key you can see is correct in the file — the
  container is holding the revoked one. `docker compose up -d --force-recreate backend
  worker`. The same applies to any `.env` edit, not just secrets.

- **Credentials in logged URLs are masked** by `app/logging_setup.py`, which both the API and
  the worker configure logging through. httpx logs every request URL at `INFO` and NCBI takes
  its key as `api_key=` in the query string, so every PubMed call used to write the key in plain
  text. The filter sits on the root *handlers*: a logger-level filter would not see records
  propagated from `httpx`. A new entry point that calls `logging.basicConfig` itself loses it.

- **`WORKER_STALE_AFTER_SECONDS` must be ≥ 3× `WORKER_HEARTBEAT_SECONDS`**, enforced by a
  `model_validator` in [config.py](app/config.py) that refuses to start otherwise. Set
  closer, one slow `UPDATE` declares a live run abandoned and the sweep starts a second
  concurrent execution of it. Per-provider request *rates* are not settings — they are
  constants in `retrieval/factory.py` (scholarly APIs) and `research/factory.py` (News API and
  the trend sources: GDELT one per 6 s, Google News, RSS, Brave, YouTube), being facts
  about each API rather than knobs; only the retry counts (`PROVIDER_MAX_RETRIES`,
  `PROVIDER_MAX_RETRY_WAIT_SECONDS`) and the trend sources' per-run *budgets* are
  configurable.
- Password hashing is Argon2id via `argon2-cffi` — chosen over bcrypt to avoid 72-byte
  truncation. **The login throttle must stay ahead of the hash.** Argon2 costs 31ms per
  attempt, so `/auth/login` saturates a core at ~32 req/s of junk, and `equalise_timing()`
  makes nonsense input cost the same as a real try. `security/login_throttle.py` is checked
  before the user lookup for that reason; moving it after would still pass every behavioural
  test and leave the process exposed. **There are five separate instances** of it —
  reviewer login, reader login, the contact form, reader signup and forgot-password (`api/deps.py`; signup
  hashes a password on every call, so it counts every attempt, and hashes in a thread
  rather than on the event loop). One shared counter
  would make the endpoints each other's denial of service: readers and reviewers arrive from
  the same office NAT, and enough failed reader logins would lock the console out of its own
  IP budget. Failures are counted for unknown emails too, or the 429
  becomes the account-enumeration oracle the equal timing exists to prevent. Unlike
  `retrieval/throttle.py` it rejects instead of sleeping — a delay only slows a client that
  chooses to wait, which is the honest user and not the attacker.

- **Four things prune themselves, one does not.** Measured 2026-09-30, the schema was lean —
  almost every column is read — but three tables grew without bound, and the trend search
  (2026-10-12) added a fourth. Now:
  - A succeeded MeSH scan deletes ledger rows older than
    `DISCOVERY_OBSERVATION_RETENTION_DAYS` (120). A validator keeps that at or above what the
    baseline windows and the angle lookback read. Shortening it past them would quietly shrink
    every baseline and turn the next window into a false surge.
  - A finished research run deletes candidates nobody decided on from runs older than
    `RESEARCH_RETENTION_DAYS` (60). Promoted and dismissed ones stay, because novelty reads
    them. A discarded candidate is stored without its example results.
  - A finished research run deletes trend observations older than
    `RESEARCH_HISTORY_RETENTION_DAYS` (180) and topics nothing refers to any more. A validator
    keeps it above the repeat cooloff, the novelty lookback and the trend-status lookback:
    pruning inside them would make a topic look new again. `research_feed_state` holds one row
    per feed and needs no pruning.
  - Orphaned images do **not** prune themselves. Every Regenerate orphans the old frames, so
    run `scripts.prune_media`. It deletes only what no article column mentions, after a grace
    period.
- **`RESEARCH_TRIGGER_TOKEN` must be the same for the backend and n8n**, and it lives in the
  repo-root `.env`. Unset, `/api/automation/*` answers 404 throughout, so n8n cannot start a
  run and nothing is exposed. It must be 32+ characters (validator). The n8n container gets
  that variable, not the whole `.env`.
- **OpenAlex needs `OPENALEX_API_KEY` (free) for real volume.** Keyless requests share a small
  daily budget per IP; measured 2026-10-01 it ran out after ~100 searches, and every later call
  was a 429 with `Retry-After` of hours, until midnight UTC — runs carried on with two providers
  and less recall, and nothing flagged it. `OPENALEX_MAILTO` no longer buys the polite pool on
  its own. `ThrottledClient` now treats a `Retry-After` of an hour or more as a spent quota:
  it fails the call at once and sends that provider nothing more for the rest of the run.
- **The trend sources' keys are optional, and each is skipped without one**, with the reason
  in the run's provider status: `BRAVE_SEARCH_API_KEY` (web search through Brave's documented
  API; ddgs otherwise), `YOUTUBE_API_KEY` (~1,010 of the default 10,000 daily quota units a
  run, `YOUTUBE_MAX_SEARCHES_PER_RUN`). Keys travel in
  headers, never in a logged URL. `RESEARCH_ENABLED_SOURCES` lists what a run may use;
  `google_news` is deliberately not in the default (see [app/research/CLAUDE.md](app/research/CLAUDE.md)).
- **`NEWS_API_KEY` is optional, and its free plan is for development only**: 100 requests a
  day, delayed 24 h. A run uses up to 50. When it is unset or out of quota, DuckDuckGo news
  answers instead, and the candidate's `signals.news.source` says which one.
- **Testing phase (since 2026-10-01): image generation and News API are switched off in the
  root `.env`, keys kept.** `IMAGE_PROVIDER=none` makes drafts text-only (illustrate cause
  `disabled`; the console's Regenerate answers 503). `NEWS_API_ENABLED=false` makes research
  use DuckDuckGo news. To turn either back on, flip the line and `--force-recreate` (the `.env` trap above).
- **Every "Let us know" message is also emailed to `CONTACT_NOTIFY_TO`** (`services/notify.py`,
  Gmail SMTP by default; `SMTP_USER` + an app password in `SMTP_PASSWORD`). An empty password
  sends nothing, and the address is a placeholder (`editorial@example.com`) until the inbox
  exists. It is a `BackgroundTasks` job after the commit, so it never fails or delays a
  submission and never describes an unsaved row. **A mail outage is only a log line, and
  the desk has no inbox screen**: `frontend/src` never calls `/api/console/contact`, so
  while email is broken (a revoked app password, a wrong `SMTP_HOST`) new messages sit
  unseen in `contact_requests`, readable only through that endpoint in `/docs`. The
  sender's text stays in the plain-text body, and line breaks are collapsed out of the
  subject, so a name cannot add a `Bcc:` header. `Reply-To` is the sender.
  **Suggesting a topic needs a reader account** (2026-11-10): `POST /contact` with
  `kind: topic` answers 401 signed out and is stored under the account's own name and
  address, whatever the form sent. The public contact page sends only `other`.
- **"Forgot your password" sends through the same SMTP settings** (2026-11-10,
  `POST /readers/password/forgot` and `/password/reset`). The link is a 30-minute JWT
  (`typ: reader_reset`) carrying a fingerprint of the current password hash, so it is
  single-use with no table: any password change, its own included, kills it. Forgot always
  answers the same 202, account or not, and sends after the response. Its throttle allows
  **three emails per address an hour** (not the login's 20 per 15 minutes), since each one
  mails a stranger through the Gmail account the contact form shares. **The reset itself is
  not throttled**: a bad link costs one signature check, and keyed on no email the throttle
  made every reset on the site share one counter (20 junk requests blocked all of them).
  **A reset signs out every device**: reader session tokens carry the same password-hash
  fingerprint (`pwd`), and `_resolve_reader` refuses one whose fingerprint no longer matches.
  Tokens from before 2026-11-10 have none and are accepted until they expire (2026-12-10). With
  `SMTP_PASSWORD` empty the email is not sent and only a warning is logged, so no reader
  can reset. The link points at `PUBLIC_SITE_URL` (default `http://localhost:5173`), which
  must be the real site address wherever this is deployed.
- **`IMAGE_GEN_KEY` must be a fine-grained HF token with "Make calls to Inference Providers".**
  A plain read token authenticates and then 403s on the first render, which reads nothing like
  a permissions problem until you see the message. No key at all is a supported state, not a
  broken one — `imagery/factory.py` returns `None`, the API still boots, runs still succeed,
  and drafts come out text-only. Two dependencies come with this (`huggingface-hub`, `pillow`);
  both were already present *transitively* via `voyageai`, so an undeclared version works in
  every local venv and fails only in `docker compose up backend`, which installs from
  `pyproject.toml`. Same trap as `python-multipart`.

## Deliberately out of scope, and what has been exercised

Full text for paywalled papers, caching full text, the agentic query-refinement loop, and the AWS
deployment are deferred ([DESIGN.md](../DESIGN.md) §11). Open-access full text is fetched per run for up to six
papers (see [app/retrieval/CLAUDE.md](app/retrieval/CLAUDE.md)). `LLMQueryStrategy` is a declared seam that raises —
`TemplateQueryStrategy` is what runs. A `source_passages` table for cached full-text passages
sits commented in `0001_initial.sql` so the FK direction is already settled.

**What has and has not met a real response** (checked 2026-08-21; keep this honest, it is what
tells you which code to trust):

- **Exercised.** PubMed and OpenAlex parsing, and the full pipeline as it then was (six stages,
  before `illustrate`) — two runs on 2026-08-20 produced 92 cached sources and two articles.
- **Exercised, contrary to what this file said until 2026-08-21: Voyage.** 288 rows carried
  `sources.embedding_model = 'voyage-4'` with populated 1024-d vectors. That string is written
  from `VoyageEmbeddingProvider.model_id` and from nowhere else; the Ollama provider namespaces
  its own as `ollama/…`. So a real Voyage call was made, with a real key, and the note claiming
  otherwise was wrong. **Superseded 2026-09-04: embeddings are now Ollama and the cache is one
  space** — all 353 rows are `ollama/mxbai-embed-large`, re-embedded by
  `scripts/reembed_sources.py`. Voyage stays fully wired and one setting away
  (`EMBEDDING_PROVIDER=voyage`), but switching back is a re-embed, not a config edit; see the
  `EMBEDDING_DIM` trap in [app/llm/CLAUDE.md](app/llm/CLAUDE.md).
- **Exercised 2026-09-04, closing the note below: FLUX.1-schnell.** The 403 was a token
  permission, as predicted. A fine-grained token with "Make calls to Inference Providers"
  renders: verified from inside the backend container against the live endpoint, decoded and
  encoded to WebP. So `_encode_webp` and the dimension read-back have now seen a real response.
- **Unresolved: which generative provider those runs used.** Both defaults said `ollama` by
  2026-08-20, but the embedding default said `ollama` too and was plainly overridden, so the
  defaults prove nothing about the environment. Each run finished in ~56s including retrieval
  and 73 embeddings, which is fast for local synthesis of a 10k-token prompt but is not
  evidence. Nothing in the database records it — which is precisely the gap the token ledger
  closes going forward, since `pipeline_stage_runs.model` now names the model per call. The
  two historical runs are backfilled with tokens and a **NULL model**, so they report
  `estimatedCostUsd: null`: inventing an id would have priced them against a model they may
  never have run on.
- **Exercised 2026-09-29: Europe PMC full text.** The open-access lookup and the JATS parser
  ran live against real papers (an ashwagandha RCT parsed to Introduction / Methods / Results /
  Discussion), and `FullTextStage` ran end to end on cached sources with the live Ollama
  embedder. Not yet inside a full pipeline run.
- **Exercised 2026-10-12/13: the Trend Search Agent and the split run, live.** A default run
  through the worker (all categories; RSS, GDELT, Google Trends, news, web) took **6.4 minutes**:
  448 items (384 from 21 feeds), 38 exact-URL and 8 title duplicates folded, 31 phrases, 12
  candidates enriched, 9 eligible, 7 evidence-checked (PubMed and Europe PMC answered every
  count), **5 selected**, one discarded for no literature. Confidence read *low* on all five,
  correctly: Google Trends was refusing this IP after a day of test runs, and GDELT throttled,
  so ~64% of each score was measured. Google News RSS answered every request (off by terms).
  **YouTube and Brave, 2026-10-13**: a trend run on RSS, news, Brave and YouTube made 8
  searches each (YouTube ~808 quota units; 176 videos; Brave 123 results), raised data coverage
  to 85% and gave every ranked topic four kinds of source; neither key appeared in the logs or
  the result. YouTube's `relevanceLanguage` is a hint, not a filter: Hindi and German videos
  came back. n8n 2.41.4 imported both new
  workflows in a scratch instance, and the manual webhook passed 202, 409 and 422 through.
- **Exercised 2026-09-30: the research agent end to end.** Google Trends (pytrends), DuckDuckGo
  web and news, PubMed and Europe PMC counts, qwen triage and deep research ran live in the
  worker: 22 seeds returned 254 rising queries in ~4 minutes and the run took ~8 minutes. n8n
  2.41.4 imported both workflows, published them, and its manual webhook reached the host API
  through `host.docker.internal`. News API has not been called: no key is configured.
- **Measured and not built 2026-09-30: local FLUX (mflux, 4-bit schnell).** On the 16 GB M2 Pro
  it took 198 s and 18.4 GB of peak GPU memory for one 1216×832 image, with swap growing from
  6.2 to 16.8 GB. Hosted FLUX with crop mode (one render per article) was chosen instead.
- **Not exercised.** Anthropic: the client and its token accounting are written against
  documented response shapes only. Semantic Scholar is wired as a retrieval provider but has
  not been enabled in a run.
- **Exercised 2026-10-01: Europe PMC as a search provider** (`ENABLED_PROVIDERS` is
  `pubmed, openalex, europe_pmc` in the root `.env`). On "Ashwagandha for stress" it returned
  50 hits, most of them papers PubMed had already found (59 duplicates merged across the three
  providers), and added 3 new ones. Its queries are the pipeline's PubMed terms with the field
  tags stripped and scoped to `TITLE_ABS` (`providers.py::europe_pmc_query`). Unscoped, Europe
  PMC searches open-access *full text*: creatine plus sleep matched 13,693 papers, led by
  brain-imaging studies where creatine is a measured metabolite, against 336 in title and
  abstract. It no longer falls back to full text when the scoped search is empty (it did for
  "zone 2 training", 0 against 20, until 2026-11-08; that topic now finds 13 scoped, and the
  fallback filled ranked lists with passing mentions). Its study-type tags are coarser than PubMed's, so a paper only it
  finds can grade lower. A paper PubMed also returns cannot be lowered by it:
  `dedup.merge_group` keeps the strongest study type any provider gave.
- **Resolved 2026-09-04: FLUX.1-schnell.** It was blocked on a token permission, not code —
  `403 Forbidden: This authentication method does not have sufficient permissions to call
  Inference Providers` against a correctly routed call (HF's router reached `nscale`). A
  fine-grained token with "Make calls to Inference Providers" renders. Recorded here because
  the 403's *sibling* is the one to recognise next time: a **401 `Invalid username or
  password`** from the same endpoint is not a permission problem at all — it is a revoked key
  still baked into a running container, because Docker resolves `env_file` at container
  creation and `restart` does not re-read it. See the config trap above.

## Changes of 2026-11-11 (audit follow-up)

- **Reviewer accounts.** A reviewer token carries the password-hash fingerprint (`pwd`), as a
  reader token does, and `require_reviewer` refuses one that no longer matches: changing the
  password (`POST /console/auth/password`, which answers with a fresh token) signs out every
  other session. A token with no `typ` is refused; it used to be read as `reviewer`.
  **A forgotten password is reset by email** (2026-11-12): `POST /console/auth/password/forgot`
  mails a 30-minute link to `/review/reset` (`typ: reviewer_reset`, so a reader's link cannot
  reset a reviewer), answers the same for any address, and has its own throttle (three an hour
  per address); `/password/reset` sets the new one, which also signs out every session. Both
  are unauthenticated, listed in `test_console_auth_coverage.py`. Two-step sign-in was added on
  2026-11-11 and withdrawn the next day, before any push (its migrations were deleted): the
  real gap was a reviewer who forgot their password, which it made worse. `scripts.seed_admin`
  still resets a password from a shell. **Roles are still not enforced** (admin and reviewer can do
  the same things); there is no user-management endpoint to restrict.
- **Reader email is verified** (`readers.email_verified_at`, 0010). Signup mails a 7-day link
  (`typ: reader_verify`, naming the address); `POST /readers/verify` confirms it and
  `/readers/verify/resend` shares the forgot-password budget. Until then a reader cannot suggest
  a topic (403). **Tests must never send mail**: `tests/conftest.py` forces `SMTP_PASSWORD=""`
  and so does `frontend/playwright.config.ts`. Before that, two suite runs mailed made-up
  addresses through the real account in the root `.env`.
- **The API process runs nothing but a timer.** The MeSH scan moved to the worker
  (`CollectionKind.MESH_SCAN`), next to the weekly `RETRACTION_SWEEP`; both are model-free. The
  API and the worker each paced PubMed to NCBI's whole per-IP budget, so a scan landing on a
  RETRIEVE sent twice the allowance. **Restart the worker after adding a collection kind**: it
  does not reload, and an old one fails the run with "unknown collection kind".
- **Observability.** Every response carries `X-Request-ID` and every log line it caused has the
  same id in brackets (`logging_setup.request_id`; "-" outside a request). `GET /console/status`
  is what the desk's status line reads: queue depths, what is running, and `stalled` when work
  has waited two minutes with nothing running (worker stopped, or holding for the model server).
- **Public reads are cacheable for a minute** when anonymous (`Cache-Control: public,
  max-age=60`, `Vary: Authorization`), and `api/public/seo.py` serves `/api/sitemap.xml`,
  `/api/robots.txt` and `/api/share/{slug}` (link-preview HTML; `ops/Caddyfile` routes bots there).
- **`llm/pricing.py`**: Opus 5 and Sonnet 5 were priced at an older generation's rates; corrected,
  and the 5.5 models added. Still no Anthropic call has been made from this codebase.
- **The login throttles are still per process.** `docker-compose.prod.yml` runs one API process
  for that reason; a second replica needs `LoginThrottle` on shared storage first.
- **Production**: `docker-compose.prod.yml` + `ops/Caddyfile` + `frontend/Dockerfile` (not yet
  run anywhere), `ops/backup.sh`, `.github/workflows/ci.yml` (not yet run on GitHub).
