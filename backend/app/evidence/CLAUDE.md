# Evidence: grading, classification, validation

Verdict caps, study-type classification, and the checks VALIDATE runs on a draft.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/04-retrieval.md](../../../docs/design/04-retrieval.md) (classification, quorum).

## The verdict cap is a ceiling

**The verdict cap is a ceiling, and nothing raises a verdict toward it.** This asymmetry is
correct as a safety property and it hides a whole class of failure: an over-confident draft fails
loudly, an under-confident one is indistinguishable from a correct cautious call. Measured
2026-09-07 — all three articles in the database were `weak`, every one of them citing only
meta-analyses and systematic reviews, every validation report clean. Two of the three had
ceilings permitting `supported`; the model simply declined. `ValidationReport.verdict_ceiling`
is recorded on every draft, pass or fail, and shown beside the verdict in the desk, because
without it "always weak" has no signal anywhere in the system. It is `Verdict | None` — reports
written before it existed must read as "not recorded", never as `no_evidence`, which is a real
verdict and the falsest thing a default could say.

## Validation checks beyond citations

**A cited sentence that shares few words with its sources is flagged, never failed**
(`low_source_overlap`, `validation.py::check_sentences_share_source_words`, 2026-10-24). The
number check cannot see a finding with no number cited to the wrong paper. Below
`LOW_SOURCE_OVERLAP` (0.35) of its content words in what it cites, after dropping digit-free
bracketed explanations, a beat-2 or section sentence goes to the reviewer as a warning.
Measured over 90 judged sentences: the one misattributed sentence scored 0.31, the lowest
supported one 0.44. One example set that threshold, so it stays a warning. `evidence/overlap.py`
is the one definition of the overlap; the eval's groundedness score imports it.

**A finding worded like another source than the one it cites fails, once**
(`misattributed_citation`, `check_citations_point_at_best_source`, 2026-10-26). The number check
is fooled when the wrong source happens to hold the number: wt_007 cited a memory study ("21
highlighted words") for an opioid trial of 10 + 11 patients. A cited sentence of at least
`MISATTRIBUTION_MIN_TERMS` content words fails when its own sources share under
`LOW_SOURCE_OVERLAP` of its words and another source shares at least
`MISATTRIBUTION_BETTER_SOURCE` and `MISATTRIBUTION_MARGIN` more. It is revisable and repairable
(`_REPAIRABLE`), and a warning in the final round, because word overlap is evidence, not proof.

**A verdict above a conclusion that disowns the claim is lowered to `weak`, in code**
(`validation.py::claim_not_backed`, `ValidateStage`, warning `verdict_lowered_by_text`,
`metrics.validate.verdict_lowered_from`, 2026-11-06). On the pilot, all six wrong verdicts were too high and
four were `mixed` over a bottom line saying the research does not back the claim. It reads the bottom line
and summary only, acts only on a draft that passed (a revision is shown the draft it corrects, and this way
no model request changes, so recordings replay), and only ever lowers. It re-validates so the warnings describe the stored verdict.
It is the weakest-claim rule applied to the article's own words, and it cannot tell the whole claim from part of it ("not supported
across all populations"), so the reviewer is shown the sentence. Two sentences do not count: a split between
studies ("others found no clear effect"), and on a claim saying "safe", "no clear evidence of harm" (`_NO_HARM_FOUND`), which
backs it; "insufficient evidence of harm" says nobody knows, so it still lowers. Since 2026-11-08 "does not support worries
that it harms" and "has not been shown to damage" back it too (both lowered a safety article before), but only with the harm
word before any "safe" or "harmless": "not proven harmless" still lowers. The quoted sentence no longer ends at the point in
"0.5" (`_SENTENCE_END`). **Replayed on the pilot: verdicts 13 → 18
of 19, answer correctness 52.6% → 68.4% (10 → 13), proportionate certainty 9 → 12; the phrases were written on
these drafts, so that flatters it.** On 86 other Oct 26 drafts: verdicts 64 → 74, correct answers 49 → 58 of
86 (fires on 24: 11 fixed, 12 no change, 1 broken). On the pilot's own Oct 26 drafts: no gain (2 fixed, 2 broken).

**Skipping a sentence that narrows the claim ("all", "every", "broad", "universal", "always") was tried and
rejected (2026-11-09)**, though it would have fixed wt_087 ("does not support a broad claim that it prevents all
skin cancer"). Replayed on 326 distinct passing articles from 183 non-pilot questions: the rule fires 46 → 38
times, acceptable verdicts 283 → 282, 1 fixed (wt_022b), 2 broken (wt_018, wt_078: "not strong enough to say the
bottles work for everyone" and "does not support the claim that … broadly improves mental health" are the article
disowning the claim), 5 unchanged. A narrowing word is how these drafts disown a claim as often as how they limit it.

## Classification

That data corrected the classifier twice (DESIGN.md §4) and left one thing open.

**Abstract heuristics must never run on a paper whose tags already say something.** A trial
protocol and a narrative review both describe randomised trials at length — that is what they
*are* — so falling through to prose scored three protocols as `rct` and one plainly-tagged
`Review` as `systematic_review`, and the latter set a real article's evidence grade.
`NEGATIVE_PUBLICATION_TYPES` now ends classification at `unknown` for those tags, checked
*after* the map so a positive identification still wins. Protocols are refused in
`RetrieveStage` before the cache sees them. Watch both directions when editing this: mapping
`Review` straight to `NARRATIVE_REVIEW` was tried and reverted, because it demoted genuine
systematic reviews PubMed had tagged only with the supertype. `Review` is a floor the text may
raise **within the review family only**.

Classification is Python, so no migration can backfill it — run
`python -m scripts.reclassify_sources` (dry by default, `--apply` to write) after touching
`PUBLICATION_TYPE_MAP`, `NEGATIVE_PUBLICATION_TYPES`, `RETRACTION_PUBLICATION_TYPES` or
`TEXT_PATTERNS`. Skipping it means the fix applies only to papers nobody has fetched yet.

**An untyped paper titled a review is a review, and one is an animal study only when its animals are the
study** (`grading.py::_REVIEW_TITLE_RE`, `_ANIMAL_SUBJECTS_RE`, 2026-11-09). OpenAlex and Europe PMC send no
publication type, and the text fallback took "randomized" anywhere: of 367 papers it graded `rct` across the
latest pilot and retrieval traces, at least 28 were not trials (19 titled "…: A Scoping Review" and the like, a
podcast summary, a comment), and wt_098 cited a rat study as a trial. A review title now takes the `Review` tag's
path; "systematic (literature) review" and "umbrella review" in the *title* of such a paper, or a title starting
"Cochrane Review", keep it systematic (in an abstract the wider pattern raised 66 narrative reviews). Animal words
demote only `rct`/`observational` guesses, only as the study's subjects ("Male Wistar rats (n=30) were
randomized", "30 mice", "in a rat model"), and never beside participants, patients, adults, men or women: "studies
in mice suggest…" in a human trial changes nothing. Over 28,887 trace papers it changes 429 types: 11 to animal
(all animal studies on reading), the rest down to review types, and three up (a Cochrane review, a systematic
literature review, an umbrella review). Retrieval suites: no pass changed; wellness recall@3 27.6% → 31.0%.
`reclassify_sources --apply` changed 8 dev rows. The eval classifies fresh (`MemorySourceCache`), so it needs no
backfill.

**Closed 2026-08-21:** a `supported` verdict could rest on a single cited systematic review,
because `check_verdict_within_grade` read the strongest cited study and never counted them.
`max_verdict_for_claims` now requires `QUORUM_FOR_SUPPORTED` sources per claim — see invariant
#3 in the root [CLAUDE.md](../../../CLAUDE.md). The queued article that was in exactly that state still passes: it cites an RCT *and*
a systematic review for its one claim.
