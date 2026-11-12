# 3. Decisions made, with reasoning — §3.5, §3.6, §3.7, §3.8


## 3.5 Embeddings: Voyage, behind an interface

`voyage-3` tier: strong on scientific text, cheap enough to embed thousands of cached
abstracts. `EmbeddingProvider` is a Protocol with one method, so OpenAI is a config swap.

**Vector width is a single config constant** (`settings.embedding_dim`, default 1024) because
changing it is a table migration and an index rebuild, not a config change. The migration
templates the dimension.

## 3.6 LLM: a model setting per job, structured outputs, no sampling params

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

## 3.7 Cost: store tokens, price at read time

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

## 3.8 The visual system: You.th over Modernist

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
