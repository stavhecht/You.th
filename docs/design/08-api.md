# 8. API surface

Full request/response models are in `backend/app/api/*/schemas.py`. Summary:

## Public (unauthenticated, read-only)

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

## Reader accounts (bearer token, own data only)

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

## Console (JWT required)

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
