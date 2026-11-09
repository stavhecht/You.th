# 2. System shape

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
