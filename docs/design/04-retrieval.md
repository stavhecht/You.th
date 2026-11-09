# 4. Retrieval design

**The scholarly APIs are the search index. pgvector is a re-ranking and caching layer.**
No attempt is made to pre-index PubMed.

## Pass 1 — keyword recall (external APIs)

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

## Pass 2 — semantic re-rank (pgvector)

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

## The `sources` table as a growing library

Once fetched and embedded, a paper is persisted and reused for every future article. The store
becomes a reusable library of cited literature: the tenth ashwagandha article costs almost no
embedding calls. `sources.last_seen_at` supports a later staleness sweep (citation counts drift).

## Classification: what the first live runs corrected

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

## The evidence quorum

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

## Retractions: refused at ingest, re-checked on a schedule

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

## Pass 3 — full-text excerpts (open access only)

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

## Provenance

`article_sources(article_id, source_id, claim, citation_handle)` records which paper backed
which claim under which `S`-handle. This is what makes the review UI fast: the sources panel
is one indexed query, and each claim in the editor renders with its backing citations already
attached rather than resolved client-side.
