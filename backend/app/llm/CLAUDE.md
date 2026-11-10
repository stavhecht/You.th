# LLM clients, providers and cost

Which model runs where, the traps in each provider, and the token ledger.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03c-models-cost-visual.md](../../../docs/design/03c-models-cost-visual.md) §3.6-3.7.

## Choosing and sizing a local model

**The synthesis coverage problems described in [pipeline/CLAUDE.md](../pipeline/CLAUDE.md) were one root cause: `LLM_PROVIDER` was unset, so synthesis ran on
`llama3.1:8b`.** It was handed 13 systematic reviews, cited 2, wrote 110 words and said `weak`.
Synthesis is now `qwen2.5:7b-instruct`; extraction stays on the 8B (326 input / ~48 output
tokens, never the bottleneck).

**Sizing a local synthesis model on this machine: read `ollama ps`, not the model card.**
Measured 2026-09-07 against 17 GB of RAM at `SYNTHESIS_NUM_CTX` 32768:

| model | resident | processor | outcome |
|---|---|---|---|
| `qwen2.5:14b-instruct` | 15 GB | 27% CPU / 73% GPU | 7% memory free, ~580k pageouts, synthesis hit the 600s `ollama_timeout_seconds` without finishing |
| `qwen2.5:7b-instruct` | 6.4 GB | 100% GPU | ~20s per synthesis call |

**The PROCESSOR column is the signal.** Anything short of 100% GPU means the weights are
partially in system RAM, and the run does not fail in a way that names the cause — it times out,
and `ollama_client` reports it as "is `ollama serve` running?", which is the one thing that was
never wrong. `llama3.3:70b` (~40 GB) is not reachable here at all. If a bigger model is wanted,
drop `retrieval_top_k` to 10 and `SYNTHESIS_NUM_CTX` to 24_576 **together** — never one alone —
or move to the hosted path, which needs the workspace header fix in the provider traps below.

## Provider and model traps

- **The Anthropic key is identity-linked and needs a workspace id.** Auth itself succeeds
  (a `count_tokens` probe returns 400, not 401), but the API answers
  `anthropic-workspace-id is required when authenticating with an identity-linked API key`.
  `anthropic_client.py:294` constructs `AsyncAnthropic(api_key=…)` with no workspace header,
  so **`LLM_PROVIDER=anthropic` will fail on the first call** until that is passed. Invisible
  today only because the provider defaults to `ollama`.
- **Model calls run locally by default.** `LLM_PROVIDER=ollama` and
  `EMBEDDING_PROVIDER=ollama` (see [.env.example](../../.env.example)) — free, no key,
  needs `ollama serve` plus `ollama pull llama3.1:8b mxbai-embed-large`. The hosted clients
  are fully wired and one setting away: `LLM_PROVIDER=anthropic`, `EMBEDDING_PROVIDER=voyage`.
  Providers are selected in [llm/factory.py](factory.py) and
  [llm/embeddings/factory.py](embeddings/factory.py); nothing else knows
  which one is live.
- **`LLM_PROVIDER=llamacpp` runs every generative call on one `llama-server`** (Ternary-
  Bonsai-2-27B, PTQ1_0, PrismML's llama.cpp fork; `llm/llamacpp_client.py`). Embeddings stay
  on Ollama. Measured 2026-10-14/15 on the 16 GB M2 Pro: better articles than qwen2.5:7b
  (groundedness 0.92 vs 0.74 over 18 cases) and better appraisal than llama3.1:8b on
  identical inputs (77.0% vs 67.8% exact over 121 hand labels, 0 flips vs 5), at ~10–40
  minutes per run. Four traps:
  - **It cannot share the GPU with an Ollama *language* model, in either direction.** Both
    resident: Ollama answers with empty bodies, and llama-server fails with `Compute error`
    and then fails *every* later request until restarted, while `/health` still says ok.
    The embedding model fits beside it. That is why it is all three calls, not just
    synthesis, why the client unloads Ollama's language models before each call, why
    research triage runs on llama-server too (`LlamaCppTriageClient`), and why the eval
    turns its judge (an Ollama model) off unless `--rescore`. Triage was skipped on this
    provider until 2026-10-23, and the raw queries became topics: that run selected
    "care" and "cognitive" and ranked "benefit" first. With Bonsai triage the next run
    selected six real topics, all labelled wellness, in 16 minutes (3 batches, ~3.5 min
    each). `python -m evaluation.trend_check` checks a run's links and topics.
  - **A dead server looks healthy.** Check it with a real completion, as the eval's
    preflight does, never `/health`. It runs under launchd with a watchdog
    ([llama-server/](../../../llama-server/README.md), `./manage.sh install`) that probes with a
    completion every 2 minutes and restarts it after two failures — the worker's heartbeat
    cannot see this failure, because the worker is fine. Do not start it by hand while the
    LaunchAgent is loaded (port clash); `./manage.sh stop` before anything that needs an
    Ollama language model, such as the eval judge.
  - **It overruns the 8-sentence section cap on long source lists** (one sentence per source)
    and quotes numbers the cited source does not contain (`unsourced_number`); both are
    caught, both cost repair calls. It also occasionally loops to the 8,000-token cap
    (PrismML known issue), reported as a truncation.
  - **`LLAMACPP_REASONING_EFFORT=none`** is what was measured. Bonsai's template rejects
    `high` with a 500, `low` thinks as long as `xhigh`, and a budget of 0 does not turn
    thinking off.
- **Ollama's `format` is a decoding grammar, not a validator.** It guarantees JSON of the
  right shape and nothing else — the Pydantic validators in `domain/contracts.py` (sentence
  ceilings, 12-word headline) are still checked in Python, and small local models break them
  often, so `ollama_client.py` retries once with the errors fed back, telling the model to change
  only the fields named: an unscoped "shorten" invites a small model to cut the whole article to
  fix one over-long section (reasoned, not measured). Also: `num_ctx` is set
  explicitly on both calls because the server default is 4K and overflow silently drops the
  front of the prompt — for synthesis that means sources vanish while the instruction to cite
  them survives.
- **`EMBEDDING_DIM` is load-bearing, and matching widths are a trap, not a safety net.**
  The migration templates the vector column width from it, so changing the *dimension* needs a
  migration. Changing only the *provider* does not — and that is the dangerous case:
  `voyage-4` and `ollama/mxbai-embed-large` are both 1024-d, so the column accepts either and
  pgvector will compute a cosine distance between them without complaint. The number is noise.
  A provider switch therefore degrades retrieval **silently**, with `sources.embedding_model`
  as the only signal. `SourceCache` repairs rows it touches (`had_embedding` compares the
  recorded model to `embedder.model_id`), so a re-retrieved paper fixes itself; everything
  else stays stale. Run `python -m scripts.reembed_sources` (dry by default, `--apply` to
  write) after any provider or model change. This is not hypothetical — the cache ran with
  288 `voyage-4` rows against 65 `ollama` ones until 2026-09-04.
- **`claude-opus-5` rejects `temperature` / `top_p` / `top_k`.** Steer behaviour by prompt only.
  `max_tokens` caps thinking **plus** output, which is why synthesis is sized at 16K for a
  ~700–950-word article; `_check_truncation` names that cause explicitly, because a truncated
  structured response has no parsed output and otherwise reads as a schema failure.
- **`retrieval_top_k` and `SYNTHESIS_NUM_CTX` move together.** Top-k is 12 ranked sources *per
  claim*, deduplicated across claims into the synthesis prompt, and it is the one knob deciding
  how many sources an article can cite. Raise it and Ollama's context window (32K, sized for
  it) has to follow: overflow there is **silent** — the front of the prompt is dropped, so
  sources vanish while the instruction to cite them survives, and the run fails as
  `hallucinated_handle` with nothing pointing at the real cause. A 32K window on an 8B model is
  also several GB of KV cache; if local runs crawl, drop to top-k 10 and 24K rather than
  leaving them mismatched. **`full_text_max_papers` draws on the same window**: six papers at
  two ~300-word excerpts is about 5,000 tokens. Measured 2026-09-29, the largest synthesis
  prompts ran ~15K input tokens, so with 8K reserved for output six still fits; raising it, or
  top-k, eats the margin. **Since 2026-10-25 SYNTHESIZE refuses to overflow it**
  (`synthesize.py::_fit_context`, `metrics.synthesize.context_fit`): past
  `SYNTHESIS_CONTEXT_TOKENS` less `SYNTHESIS_RESERVED_TOKENS` (estimated at
  `CHARS_PER_TOKEN` 4.2; Bonsai measured 4.55-4.84) it drops excerpts, then sources, worst-ranked
  first, never the last source, and logs what went. Measured on broad questions before the
  4-claim cut, prompts reached ~24K tokens. `SYNTHESIS_CONTEXT_TOKENS` is a constant that must
  match llama-server's `-c` (32768).
- **`thinking` is passed explicitly on both Anthropic calls, and must stay that way.** Whether
  an omitted `thinking` means "think" varies across the range (Sonnet 5 / Opus 5 do, Opus 4.8 /
  4.7 do not), and the model is config — leaving it implicit lets `EXTRACTION_MODEL` silently
  decide whether the token budget is spent on thinking. It also means **extraction cannot run on
  `claude-haiku-4-5`**: it is pre-adaptive-thinking, so the argument 400s. Both models therefore
  default to `claude-sonnet-5` — the other reason being that Haiku left `ingredients` empty in 9
  of 10 samples, which silently unanchors the PubMed query.
- **The `cache_control` breakpoint is a no-op on extraction.** The minimum cacheable prefix is
  per-model and not monotonic (512 on Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5), and a prompt
  under it fails to cache silently — `cache_creation_input_tokens: 0`, no error. Measured with
  `messages.count_tokens`: extraction is **306 tokens** and caches on nothing; synthesis was
  **1285**, caching on Sonnet 5 with only ~260 tokens of headroom. The section and specificity
  rules roughly doubled the synthesis prompt (~1,390 words as of 2026-09-04, not re-counted
  against `count_tokens`), so that headroom is now comfortable rather than marginal — growing
  this prompt is safe, and it is *trimming* it that would break caching without saying so.
  Confirm against `usage.cache_read_input_tokens` on a real call rather than assuming.
- **Do not compress the synthesis prompt. It was tried, measured, and reverted.** A rewrite to
  1,302 words from 1,503 kept all 70 rules present — verified by probe, nothing dropped — and
  still cost output quality on `llama3.1:8b`: inline citations fell from 5.0 to 3.3 handles per
  article and sections from 3.0 to 2.0, with 43% of runs emitting no sections at all against
  17% before (n=14 compressed vs n=6, same 12 sources). The redundancy and the stated rationale
  are load-bearing on a small model, not padding. And the trade is bad even before quality:
  the system prompt is only ~13% of a synthesis call (the 12 abstracts are the rest), so a 13%
  trim of it saves **2% of the call**. If this needs revisiting, the lever is `retrieval_top_k`
  or a larger model, never these words.
- **Pointing a model setting at something new needs a price-table entry.** Nothing breaks
  without one — the run just reports `estimatedCostUsd: null` forever, which is honest and
  easy to miss. `test_cost_accounting.py` asserts the clients' four default models are all in
  `PRICES`, so the defaults are covered; a value set only in `.env` is not.

- **Both LLM clients log full prompts at `INFO`.** Useful while the prompts are unexercised,
  but it puts every system prompt and source block in the log stream — reconsider the level
  before anything ships where logs are retained.

## Tokens and cost

**Tokens are stored; cost is computed at read time and never written down.** The ledger is
`model` + four token columns on `pipeline_stage_runs` — that table is already one row per run ×
attempt × stage, which is the grain cost surprises actually have. `pipeline_runs` keeps a
rollup, written as an **increment** because `ctx` is rebuilt per attempt and assigning would
report the cheapest attempt as the whole run. Prices live in `llm/pricing.py` keyed by
**provider-namespaced** model id (`anthropic/claude-sonnet-5`), so a local model — genuinely
free — is never confused with a hosted one nobody has priced. Two rules that look like
politeness and are not: an unpriced model returns **`None`, never `0`** (they are both falsy
and mean opposite things, and the zero would be believed on the first run after someone edits
`SYNTHESIS_MODEL`), and a run total is all-or-nothing, because extraction alone is ~1% of a run
and would read as a plausible answer. Do not add a `cost` column — a stored price freezes
whichever number was current that day, with no record of which one it was.

**Tokens burned by a *failed* call are recorded too, and that write outlives the rollback.**
`LLMError` carries `usage`; the extract and synthesize stages record it before converting to a
`StageError`; `_abandon` writes it while rolling back everything else the stage did. The
rollback undoes our writes, not the provider's charge — and the case that matters is truncation,
which spends the entire 8K synthesis budget and produces nothing, so a zero here makes the
pipeline look cheapest exactly when it is burning the most. This is also why
`PipelineContext.record_usage` mutates in place instead of returning a copy: a raising stage
returns no context. `ctx.usage` is a **derived property** over `usage_by_stage`, not a field —
don't reintroduce a separately-summed one, the two drift the first time a call is added and
only one side is updated.

Embeddings are **outside** the ledger on purpose (~1% of a run, and widening
`EmbeddingProvider` changes its return type and every caller). Say "not measured", not "free".
**Generated images are outside it for a stronger reason**: an image is billed per render, and
this table prices four token columns. `IllustrateStage` therefore never calls `record_usage` —
a model id written against four zeros is exactly the "unpriced model reads as free" mistake
`pricing.py` exists to refuse. Count, seed, latency and model go into `metrics` instead, and a
run's `estimatedCostUsd` excludes image spend.

The two pre-existing runs were backfilled from the old `metrics` JSONB, and their stage rows sum
to ~328 fewer input tokens than the run rollup: extraction's *input* count was never in
`metrics` (only `"tokens"`, its output). Expected, not drift — don't "fix" it. Their `model` is
NULL, so they report an unknown cost rather than a guessed one.
