# 5. The AI contracts

Both calls are typed end to end: a Pydantic input model, a Pydantic output model used as the
structured-output schema, and a deterministic validator downstream.

## Call 1 — claim + ingredient extraction

Input: product/trend name + optional marketing blurb.
Output (`ExtractionOutput`): `product`, `target_claims[]`, `ingredients[]`.

Cheap, low-risk, no retrieval involved. The only failure mode worth guarding is the model
inventing claims the blurb never made, so the prompt forbids inferring unstated claims and the
schema caps `target_claims` at 6.

## Query generation (swappable step)

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

## Call 2 — synthesis (the RAG generation)

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

## Call 2, zero-source branch — the `no evidence` article

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
