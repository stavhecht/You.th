# You.th — Architecture & Design

Evidence-checked wellness content. A Pinterest-style public feed of short articles that
state what a product or trend claims, what the research actually shows, and a plain-language
verdict — every factual sentence carrying a citation to a real paper, every article approved
by a human before publication.

**The product is You.th; the repository, the Python package, the database and the containers
use the slug `youth`**, because a dot cannot appear in a package name or a DSN.

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

## Contents

The design is split by section under [docs/design/](docs/design/). Section numbers are kept, so a
code comment citing "DESIGN.md §3.4" means the row for §3.4 below. Day-to-day rules and traps
for each area are in the nested `CLAUDE.md` files listed in the root [CLAUDE.md](CLAUDE.md).

| § | Section |
|---|---|
| §1 | [The four invariants](docs/design/01-invariants.md) |
| §2 | [System shape](docs/design/02-system-shape.md) |
| §3.1, §3.2, §3.3 | [Vite SPA, shaped for a Next.js port (your call, recorded) / Auth: two kinds of account, minimal, with a real audit trail / Pipeline: local worker with a `pipeline_runs` table](docs/design/03a-frontend-auth-pipeline.md) |
| §3.4, §3.4b, §3.4c | [Article body as TipTap JSON, not markdown / Article media: uploaded or generated, never linked / Generated illustration: two frames, one locked prompt builder, no claim](docs/design/03b-article-body-and-media.md) |
| §3.5, §3.6, §3.7, §3.8 | [Embeddings: Voyage, behind an interface / LLM: a model setting per job, structured outputs, no sampling params / Cost: store tokens, price at read time / The visual system: You.th over Modernist](docs/design/03c-models-cost-visual.md) |
| §4 | [Retrieval design](docs/design/04-retrieval.md) |
| §4b | [Topic discovery: the research agent](docs/design/04b-topic-discovery.md) |
| §5 | [The AI contracts](docs/design/05-ai-contracts.md) |
| §6 | [Article format](docs/design/06-article-format.md) |
| §7 | [Human-in-the-loop review](docs/design/07-review.md) |
| §8 | [API surface](docs/design/08-api.md) |
| §9 | [Data model](docs/design/09-data-model.md) |
| §10 | [Infrastructure: local now, AWS later](docs/design/10-infrastructure.md) |
| §11 | [Phased roadmap](docs/design/11-roadmap.md) |
| §12 | [Known risks](docs/design/12-risks.md) |
