# Retrieval: queries, providers, cache, re-rank, full text, retractions

How a claim becomes a search, how results are merged, cached and ranked, and what is refused.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/04-retrieval.md](../../../docs/design/04-retrieval.md).

## Shape: APIs are the index, pgvector re-ranks

**Retrieval: the scholarly APIs are the index; pgvector is re-rank plus cache.** Pass 1 fans
out per claim across PubMed, Europe PMC, Semantic Scholar and OpenAlex, normalising into
`CandidatePaper`. Pass 2 upserts into `sources`, embeds new abstracts in overlapping chunks, and
ranks by best-chunk cosine + grade bonus + recency bonus. Those bonus constants in
`retrieval/rerank.py` are explicitly tunable starting values, not settled numbers.

**Chunking is one function, `retrieval/chunking.py::chunk_text`: 300-word windows, each
repeating the last 30 words of the one before.** The size is set by the embedding model, not
chosen to force a split: 300 words is ~420 tokens, inside `mxbai-embed-large`'s 512-token
window — and past that window Ollama truncates **silently**, dropping the end of a long
abstract, which is usually its results. It was 120 words for a day (2026-09-29), which split
94% of cached abstracts for no gain: the median is ~250 words and fits whole. At 300, three in
four are one chunk and rank exactly as a whole-abstract vector did; only the long ones split.
Every chunk gets its own vector in `source_chunks` (no text: a chunk is re-cut from the
abstract when needed), and a paper's similarity
to a claim is its **best** chunk's — not the mean, which lets the rest of a long abstract
dilute the one passage that answers the claim, and not the sum, which rewards length. The
model is still shown the whole abstract; chunks exist only for ranking.

**RANK scores against the subject plus the claim** (`rerank.py::ranking_text`, e.g. "magnesium
improves sleep quality"), with the subject chosen the way the query chose it — ingredients,
else product (`claim_subject`). The bare claim names only the outcome, so every paper about the
outcome scored alike whatever it studied; measured 2026-10-01, 13 known-relevant papers reached
a pool and missed its top 12 that way, beaten by off-subject papers that loose providers
(OpenAlex, Europe PMC's full-text fallback) let in. FULL_TEXT keeps the bare claim for excerpts
on purpose — see below.

`sources.embedding_model` and `sources.chunk_settings` record which model
embedded a paper's chunks and how they were cut, and a paper is current only when **both**
match the live values — so editing `CHUNK_WORDS` re-chunks the cache instead of leaving it cut
the old way forever. `cache.py::write_chunks` is the only writer: the pipeline and
`scripts/reembed_sources.py` both go through it, and it deletes a paper's old chunks before
writing new ones, so a shorter re-chunk cannot leave stale ordinals behind.

## Full text (FULL_TEXT stage)

**Full text adds detail to ranked papers; it never ranks them.** `FULL_TEXT`
(`pipeline/steps/full_text.py`) asks Europe PMC which ranked papers are open access, picks up to
`full_text_max_papers` (6) with the claims taking turns so every claim gets one before any gets
a second, downloads them (`retrieval/full_text.py`, JATS body only), splits them with the same
`chunk_text`, and hands SYNTHESIZE the `EXCERPTS_PER_PAPER` (2) chunks closest to each paper's
claims. Ranking stays on abstracts on purpose: measured 2026-09-29, only 144 of the 309 cached
papers are open access, and best-chunk scoring over a ~20,000-word paper's sixty chunks
against an abstract's one is a length bias — it would reward being open access, not being good
evidence. Five things about it:

- **It never fails a run**, like ILLUSTRATE and for the same reason: without excerpts the
  article is written from abstracts, exactly as before the stage existed — the status quo, not a
  hidden degradation. `metrics.full_text.cause` says why there were none (`disabled`,
  `lookup_failed`, `no_open_access`, `download_failed`, `unexpected`). Europe PMC's search
  endpoint was measured returning 503 for every query for a stretch of 2026-09-29, so
  `lookup_failed` is a real state, not a theoretical one.
- **Introduction and Background are never excerpted**, and this was measured, not guessed: the
  first live run picked *both* its excerpts from a Background section, because background text
  restates the claim's own wording. It also summarises *other* papers — "[3, 5, 21]" — so an
  excerpt from it invites the model to credit this paper with its reference list's findings.
  With them excluded the same paper yielded Methods (57 recruited, 25 per arm) and Conclusion.
  Discussion is kept, though it too cites others; watch it.
- **The prompt says a source without excerpts is not weaker evidence.** Handing the model more
  detail about open-access papers pulls it toward them; that sentence and the cap of six are the
  counterweight. The verdict cap is untouched — it reads study types and counts, never text — so
  excerpts cannot raise how confident an article may sound.
- **Excerpts are recorded from `synthesis_input`, not from `ctx.excerpts`** (`persist.py::
  excerpts_shown`) — what the model saw, for the reason VALIDATE checks handles against it — onto
  `article_sources.excerpts` (`NullableJSONB`), and shown in the desk's sources
  panel. An article can quote a number that is in the paper but not its abstract, and the desk
  shows no abstracts, so without that list a reviewer had to open the paper to check one.
- **Full text is fetched per run, not cached.** Six downloads is cheap next to a synthesis call,
  and there is nothing to keep fresh. `source_passages` in `0001_initial.sql` is still only a
  sketch; build it if fetching becomes the bottleneck, not before.
- **A paper with no PMID is looked up by its DOI**, in the same Europe PMC request
  (`retrieval/identifiers.py`, `metrics.full_text.looked_up_by_doi`). It used to be skipped
  before Europe PMC was asked. The paper's own reference numbers (`[34,35,36]`) are stripped
  from excerpts in `parse_sections`: they point at a list the model never sees, and a copied
  square bracket fails the draft (see the citation-marker paragraph in [domain/CLAUDE.md](../domain/CLAUDE.md)).
- **Passages describing a review's literature search are never excerpted**
  (`describes_literature_search`, `metrics.full_text.search_passages_skipped`). "The literature
  search identified 4788 articles…" names the claim's words as search terms, so it scored 0.71
  against a findings passage's 0.712 and was chosen. Ranking against `"<product>: <claim>"`
  instead of the bare claim was measured the same day and made it worse: it chose two such
  passages for the review and swapped an RCT's Discussion for an off-topic one. Keep the bare
  claim. The filter needs two distinct search phrases and deliberately omits "were screened" and
  "eligibility criteria", which also describe a trial's recruitment and its sample size.

## Query building (`query_builder.py`)

**A question is not a product.** Extraction was copying the user's question into `product`
("Is it bad for you?"), which then searched as a quoted phrase, found nothing, and published
`no_evidence` on a well-studied question. The extraction prompt now says to name the substance
or practice a question asks about (or `unspecified`), and `query_builder._is_a_name` refuses a
question, a sentence of more than eight words, or `unspecified` as an anchor — after
`_mesh_within` has had its chance to find a known substance inside it. **An unmapped product is cut at " for "** (`_subject_group`, 2026-11-05): "Smart ring for sleep"
searched as one quoted phrase; measured on 8 real trend and MeSH labels, 2 came back whole from
extraction and were saved only by a known substance inside them. A device has no such rescue.
**An unmapped name is searched singular and plural** (`_word_forms`, 2026-11-05), since a quoted
phrase matches one form: "smart rings" counted 17 papers on Europe PMC and either form 47. Words in
-ing stay single ("foam rolling"), short ones aside ("smart ring"). A lone plural stays as written
(2026-11-06): "dates" searched `"date"`, in every abstract saying "to date", and "greens" `"green"`.
That costs "wearables" and "mushrooms" their singular, and the suite figures below predate it. **Verbs a question words its
claim with are stopwords** ("replaces", "detects", "measures", "tracks", "accurately"). Both change
the terms of every search for an unmapped subject, so recordings keyed by those terms miss. Retrieval
suite re-run on them 2026-11-05 against 2026-10-24: 89 of 114 cases pass both times, nDCG@10 94.0% →
94.9%, precision@5 94.0% → 94.4%, recall@3 35.0% → 33.0%, recall@5 unchanged at 42.9%.
**One field can hold several names, and splitting it must never loosen the anchor**
(`_names_within`, `_split_name`, 2026-11-07). The search is an OR, so one bad part finds papers off the
subject and every later signal reads as success: the first version turned "vitamin d and depression" into
`"Vitamin D" OR "depression"`, "16/8 intermittent fasting" into `"16" OR "8 intermittent fasting"`, and split
43 of 242 names in eval traces, many badly. The rules: **a bracket beside a known substance only names if it is
known too** ("melatonin (low dose)" searches Melatonin only), and any unmapped bracket stating a dose goes:
"intermittent fasting (low dose)" counted 357,271 PubMed papers with it, 3,472 without. `_mesh_within` returns
every known substance in the field, so "creatine and beta-alanine" searches both, but not in the context after
"during" or "with" ("creatine during the menstrual cycle" is creatine). **An "or" alternative stays beside a known
substance** ("fish oil or krill oil" searches both). **A plain "and" never splits**, because it joins a subject to its outcome
("protein timing and muscle hypertrophy") as often as two names, and extraction rule 5 now says to write "or"
between alternatives; `/` splits only between letters; **a lone word beside a longer name takes its last
word** ("hot or cold compresses" searches "hot compresses" and "cold compresses"; whole, it matched no abstract once
names were tagged [tiab]), unless it is a known substance, and except in a comma list ("saunas, steam rooms").
The cost is on purpose: an unmapped "ice packs and cold wraps" stays one name (3 PubMed papers) and may draft
`no_evidence`, which now says it may be our search's words, while a wrong anchor drafts a confident article on the
wrong papers. A field over `_MAX_NAME_WORDS` is still a name when it splits into parts of at most `_MAX_LISTED_NAME_WORDS`
("green juices or celery juice or ginger and turmeric shots", wt_039, searched turmeric alone); a longer part is half
a sentence. One known gap, so add a hint rather than loosen a rule: "coffee or green tea" searches "coffee tea" and
"green tea". Over 200 real extracted names (2026-11-07): 2
unmapped pairs joined by "and" (45 and 933 papers), 3 fields with two known substances, none wrong, and 4 unmapped
brackets, all real synonyms.
**Who a claim is about narrows the search** (`_population_group`, `POPULATION_HINTS`, AND-ed in, kept by
`broaden`): "without diabetes" had no review in PubMed, and 9 with "nondiabetic" beside it. Several groups OR
("men and women"), but "pregnant women" is one group, not any study of women. Neither rule has been through the
retrieval suite yet, and names whose terms changed miss their recordings. **Identical searches
run once per RETRIEVE** (`searches_shared`): two claims reducing to the same terms used to send
every provider the same request twice.

**Every query must name a substance, and failing to is a stage failure — not a warning.**
`QueryStrategy.build()` takes `product` and `ingredients` separately: ingredients name the
actives but are optional, `product` is required, so the product is the fallback anchor when a
model returns `ingredients: []` (local models do this constantly). If neither yields a subject
term, `TemplateQueryStrategy` raises `UnanchoredQuery` and `RetrieveStage` converts it to a
**non-retryable** `StageError` before any provider is called — nothing varies between attempts,
so a retry just recomposes the identical unusable query. This replaced a keyword fallback that
logged a warning and continued, and the difference is not academic: a run on creatine with no
ingredients searched `(workout AND performance)`, retrieved 50 real papers about CrossFit and
pre-workout blends, cited two of them, passed citation validation, and produced a `supported`
verdict at `systematic_review` grade on a product none of the sources mention. Every downstream
signal reads as success — this stage is the last place the mistake is visible. Do not soften it
back to a warning, and do not merge `product` into `ingredients` at the call site.

**Queries keep a single-outcome claim's own phrase, and the cap cuts nothing** (2026-10-23).
`_outcome_group` adds `"sleep latency"[tiab]` beside the generic sleep vocabulary, directional
verbs ("lowers", "increases", "causes") are stopwords, the reviews pass asks for 30 (was 15) and
`retrieval_max_candidates_per_claim` is 65, the two passes' sum: at 50 the extra reviews cut 10
gold papers out of the general pass. Measured on the retrieval suite: gold recall@10 48.6% →
54.1%, precision@5 93.2% → 95.3%, nDCG@10 93.7% → 95.8%. Gold papers ranked 6–12 sit 0.002–0.03
below other meta-analyses, so the grade and recency bonuses were left alone: tuning them to a
two-paper gold list would fit the list, not the ranking.
**So instead the three best-matching trials and systematic reviews are always kept** (`rerank.py::select_top`,
`KEEP_BEST_MATCHES`, `PROTECTED_TYPES`, 2026-11-09, used by the eval's `MemoryReranker` too): the lowest-scoring
others in the top 12 make room for them, and the bonuses still order the list. wt_087's melanoma trial (second best of 51 by
similarity, 0.013 under the cut behind sun-behaviour reviews) is now shown. Protecting any type was tried first: it
changed the 12 in 70 retrieval and 64 wellness cases and swapped conf_004's 2024 vitamin D meta-analysis for an
older narrative review titled like the question, which caps at `weak`. Restricted, it changes 10 and 8 cases;
passes stay 90 and 88, and gold papers among the 12 shown go 13 → 13 of 35 (wt_087 gained, wt_068 lost on a
0.001 score tie between two meta-analyses) and 45 → 45 of 77. The swapped-in papers keep their low scores and sit
at 11-12, so the suites' top-5/top-10 metrics cannot see this; only an article run can. **wt_087 live with both
fixes (report `20261109-102238`, one sample):** the trial is cited ("11 new melanomas compared with 22", 1,621
Nambour residents), "no large randomised trial" is gone, judge correctness 3/5 → 5/5 with both expected claims. It
still fails on the verdict: the model wrote `mixed` (acceptable) and `claim_not_backed` lowered it to `weak` on
"the research does not support a broad claim that it prevents all skin cancer", the part-of-the-claim false positive
that rule is known for.
An unmapped product drops usage words before it is quoted (`_USAGE_WORDS`: "daily sunscreen
use" searched as one phrase missed the trial titled "regular sunscreen use", 2026-10-25), and
the longest matching outcome key wins ("skin cancer" no longer brings in "skin"'s aging terms).
Since 2026-10-26: a subject naming examples ("wearables like WHOOP", "such as", "including")
searches each example; outcome keys match at a word start only; and a harm claim ("causes",
"damages", "toxic", "side effects") ORs in safety vocabulary (`"adverse effects"[sh]`,
"adverse events", "side effects"), because harm is reported in safety tables, not under the
outcome's own name.

**What followed changed extraction and every search for an unmapped name; its retrieval suites and one fresh pilot are measured (below).** Extraction now keeps who and what-versus in the claim, writes two things as "A or B", and uses a paper's short name (251 saved
questions, ~7 s each, no population invented). **PubMed drops the quotes from a phrase it has not indexed** and searches loose words,
unknown ones dropped: `"glazed-donut skin"` found 1,059,975 papers, on skin, and the singular/plural forms triggered it (`"simplified
skincares"`, 125,885). So names are tagged (`_name_term`): `"a b"[tiab]`, plus the words apart when two are 4+ letters, since a tagged
phrase alone emptied real topics ("whole-body vibration plate training": phrase 0, words 46, "glazed-donut skin" still 0; "one meal a
day": words 4,411, phrase 21). **OpenAlex was sent PubMed's field tags, which wreck its search**: "alcohol reduction" and blood pressure,
4 unrelated papers tagged, 8,407 plain, led by the right one; it gets them stripped now. **Measured 2026-11-08 with OpenAlex answering
on every case, the wider searches alone did not help the wellness suite**: passes 80 → 75 of 113, off-topic papers per case 2.26 → 3.57,
MRR 93.2% → 88.6%, recall@3 34.5% → 27.6% (the retrieval suite: 89 → 89, recall@3 33.0% → 39.1%, off-topic 0.62 → 0.69). PubMed's
off-topic papers fell 104 → 43, so tagging worked; the rise was OpenAlex (87 → 238) and Europe PMC (64 → 122). When a name is in no title
or abstract, PubMed and scoped Europe PMC rightly find 0, and the rest filled the list from full text: OpenAlex's plain `search` reads
full text ("bulletproof coffee": 497 works, gun adverts among them; 5 in title and abstract, all on the drink), and Europe PMC retried
unscoped ("under-desk treadmills": sit-stand desks). **So OpenAlex now searches `title_and_abstract.search` and Europe PMC never retries
in full text** (`providers.py`). Re-run on the 60 cases that had changed: wellness passes 24 → 35 of 44 (29 before the search change),
off-topic per case 6.84 → 1.98 (3.73), MRR 79.3% → 90.4%; retrieval suite off-topic 3.19 → 0.38. Gold papers ranked on those cases
6 → 6 and 4 → 3; wt_052 lost its gold trial ("Cold Showering") only because the name rule cut "with cold water" from "ending a shower
with cold water", leaving the subject "ending a shower", and full text had found it by luck. **Both full suites re-run 2026-11-09 after OpenAlex's reset (reports `20261109-000546`,
`20261109-002006`, no provider outages, files unchanged mid-run), against 2026-11-08:** wellness passes 75 → 88 of 113 (80
before the search change), off-topic per case 3.57 → 1.17 (2.26), precision@5 78.9% → 87.1%, nDCG@10 81.6% → 90.7%, MRR 88.6% →
95.0%, recall@5 unchanged at 36.2%, recall@10 39.7% → 36.2%; retrieval suite passes 89 → 90 of 114, off-topic 0.69 → 0.14,
nDCG@10 92.7% → 96.2%, recall@5 44.2% → 42.9%. New failures: basic_013 and basic_019 lost their gold papers from the top 10,
basic_028 (electrolytes, cramps) a smaller pool (41) and precision@5 0.4, wt_068 (zone 2 against intervals) precision@5 0.4.
recall@5's 85% threshold has never been met and stays the one critical miss. Still bad either way: wt_077 (ankle or wrist weights), wt_094 (glazed-donut skin). **Fresh 19-case pilot on all of it, 2026-11-08 (`--mode live`, report `20261108-093913`, no provider outages): correct answers 15 of 19 (78.9%, was 13), verdicts 16, certainty 14 (was 12), citation correctness 86.1% (under its 90%), hallucination 0, groundedness 97.9%, ~9 h with one timeout re-run.** Wrong: wt_023b (GLP-1 side effects, `weak`, judged 5/5), wt_082 (`mixed`, "not yet fully supported" still unread by `claim_not_backed`), wt_087 (sunscreen, the trial still outside the top 12), wt_098 (rosemary, `supported`). 85% needs two more. The wordings in `MESH_HINTS` for ice baths, alcohol
reduction, lemon water and glucose monitors were written on evaluation cases. **A prompt example gets copied**: "yoga before bed" turned
melatonin's "sleep latency" into "fall asleep faster" (basic_005), and a stretching one added an injury claim.

## Identity and the cache

**Identity is a union-find over every identifier a record carries** (`retrieval/dedup.py`), not
one preferred key per record. Providers report different identifier *subsets* for the same
paper, so a DOI-only and a PMID-only record only unify when some third record carrying both
bridges them — a single-key scheme leaves two candidates, two `sources` rows, and two handles
the model cites as independent corroboration. Titles are an identity key only for a record
carrying no identifier at all: errata and conference abstracts repeat their parent's title, and
over-merging silently deletes a study while under-merging only costs a prompt slot. This is
invisible with PubMed alone (its records carry both identifiers) and appears the day the other
providers come online — watch `retrieve.duplicates_merged`.

**A DOI-only paper is given its PMID when Europe PMC knows one**, in `RetrieveStage._fill_pmids`
after the first merge and before the cap, and the merge then runs again, so a filled PMID bridges
a DOI-only and a PMID-only record of one paper. A PMID is taken only from a record whose *own*
DOI matches, and a DOI answering to two PMIDs is left alone: guessing would merge two papers. A
lookup that fails never fails the stage (`retrieve.pmid_lookup_failed`), because the search
itself worked. Expect `pmids_filled` to be low: OpenAlex already reports a PMID when one exists,
and measured 2026-09-29 all nine DOI-only papers in the cache had none in PubMed, Europe PMC or
NCBI's ID converter. They are in journals MEDLINE does not index, so there is nothing to assign.
A cached DOI-only row picks up its PMID the next time a run retrieves it (the cache's
`COALESCE`).

**A DOI-only paper doi.org has never registered is dropped** (`RetrieveStage._screen_unregistered`,
`identifiers.py::DoiRegistry`, after the retraction screen). OpenAlex indexes journals that print
DOIs they never registered: a gut-health article cited a "systematic review" whose DOI doi.org and
Crossref both 404. Only DOI-only papers are asked (0–26 per run measured), a refinement round
skips DOIs that already passed, and an unanswered DOI is kept. The eval's recordings gained
doi.org answers on 2026-10-23; a replay of older recordings would skip cases on the miss.

`RetrieveStage` is the only stage that writes to the cache; it carries each candidate's row id
forward as a `CachedCandidate` on `ctx.candidates`, so **`RankStage` takes no `SourceCache` and
reads only**. Don't reintroduce a lookup there — it was a full re-upsert of the candidate set
to recover ids the previous stage already had.

**The cache upsert is deliberately not a single `ON CONFLICT`.** A paper's identity spans two
partial unique indexes (`doi`, `pmid`) and Postgres takes one inference clause per statement,
so inferring against whichever identifier the incoming record happens to carry breaks as soon
as a record arrives *more complete* than the row it matches — PMID-only row, DOI-bearing
record, infers on the DOI index, finds nothing, inserts, violates `sources_pmid_key`. Union-find
makes that the common path, not the edge case. `SourceCache` therefore resolves first (one read
matching every identifier at once), updates matched rows **by primary key**, and inserts only
new papers; the read-then-write race surfaces as an `IntegrityError` inside a `begin_nested()`
`SAVEPOINT` and is retried once. The savepoint is load-bearing — the pipeline session is
long-lived and a bare rollback would discard the rest of the run. Matched-row refreshes are one
`UPDATE ... FROM (VALUES ...)` per batch, not per row; warm is the normal case, and the per-row
loop made it the most expensive path instead of the cheapest.

**The re-rank is exact and there is no HNSW index.** Top-k
is applied in Python after the grade bonus, so no `LIMIT` reaches SQL and the planner cannot
choose an approximate scan — `tests/db/test_rerank_plan.py` pins this with `EXPLAIN`. That holds
for `source_chunks` exactly as it did for the old `sources.embedding`: creating an index there
means revisiting `rank_for_claim` first. `0001_initial.sql` records the statement and the
condition that would justify one, commented beside the index it declines to create.

## Throttling

**A throttled search must never look like an empty one.** Providers sit behind
`retrieval/throttle.py` (per-provider token bucket + bounded retries honouring `Retry-After`),
and `RetrieveStage` fails the stage when *any single claim* had every provider call fail.
Recall lost to a 429 produces a more cautious verdict, which reads as a correct answer — so the
degradation is invisible unless it is caught here. Rates are per process and the APIs limit per
IP; the constants in `retrieval/factory.py` are deliberately under the published ceilings. A
related consequence: Semantic Scholar without an API key is skipped **at construction** rather
than returning `[]` from `search()`, because a provider that answers without making a request
counts as a successful search and would mask a claim whose only real provider was throttled.

## Retractions

**A retracted paper is refused, not downgraded — and `retraction_checked_at IS NULL` means
nobody asked, not "clean".** Three mechanisms, and mixing them up is how this breaks:
`is_retracted()` drops the paper in `RetrieveStage` before the cache — and since 2026-10-02
RETRIEVE also asks PubMed (`esummary`, via `PubMedRetractionSource`) about every paper with a
PMID that a *non-PubMed* provider supplied, because an OpenAlex record of a retracted trial is
typed `article` and was cited with a `supported` verdict; that screen fails open
(`retraction_screen_failed`); `classify_study_type`
returns `UNKNOWN` for one already cached; `scripts/check_retractions.py` re-checks cited sources
against PubMed + Crossref on a schedule, because retractions land *after* publication and the
ingest screen only ever catches what was already withdrawn when we first saw it. **Since 2026-11-11 the worker
runs it weekly** as a collection run (`CollectionKind.RETRACTION_SWEEP`, model-free, so a
stopped model server does not hold it; the logic is `retrieval/retraction_sweep.py::sweep`, and
the script is the same sweep with a dry run and a printout). The desk shows the result: a flag
on the queue row, a banner on the draft, a mark on the source, and Publish asks before putting
out a draft that cites a retracted paper. Before that the schedule was
a host LaunchAgent, Mondays 07:00 (`./jobs/retractions.sh install`; log
`~/Library/Logs/youth/retractions.log`), which a deployed stack never had; uninstall it
(`./jobs/retractions.sh uninstall`) or the sweep runs twice a week. Nothing ran it before 2026-10-28; the first run
checked the 106 sources cited by published and pending articles, all clean.

Two rules with teeth. **Retractions are checked *before* the publication-type map**, unlike
every other entry in `NEGATIVE_PUBLICATION_TYPES` — a retracted trial also carries `Randomized
Controlled Trial`, so under the normal "positive identification wins" rule it graded `rct`,
ceiling `supported`, and the cap existed only for papers it did not matter for. Don't move it
back. And **`RetractionVerdict` has three states**: a provider that failed, 500'd, or cannot
resolve an id leaves the paper *unchecked*, never clean. NCBI in particular returns an `error`
record rather than omitting an unknown id, and reading that as an empty `pubtype` list marked
unlookuppable papers verified clean — then wrote a timestamp that suppressed the next sweep.
Same principle as `retrieval/throttle.py`: a search that could not run must never look like one
that found nothing.

**A flagged article stays `published`.** The sweep sets `retraction_flagged_at`, which raises a
reader-facing banner and a console row. It does not withdraw. That is invariant #1 pointed the
other way — a human decides what the public sees, in both directions — and one retracted source
among several need not invalidate a conclusion. Nothing is deleted either: the source row may be
referenced by `article_sources`, and provenance outranks tidiness.
