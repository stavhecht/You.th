# 10. Infrastructure: local now, AWS later

**Now:** Docker Compose (Postgres 16 + pgvector), FastAPI via uvicorn, worker as a separate
process, Vite dev server. Everything runs on one machine, and with the Ollama defaults, with
no API keys at all.

**The API is containerised; the worker and the migrations are not.** `backend/Dockerfile` is a
two-stage build — toolchain and a venv in the builder, `libpq5` and a non-root `youth` user
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
