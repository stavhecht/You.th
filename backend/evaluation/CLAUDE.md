# Evaluation harness

Running the eval, what its recordings key on, and the measurement log behind current pipeline rules. See also [README.md](../../README.md).

Loaded when you work under this directory; the root [CLAUDE.md](../../CLAUDE.md) (commands, invariants, map) still applies.

## Running it

- **The eval keys appraisal recordings by prompt too** (`harness/llm.py::_APPRAISAL_PROMPTS`), so
  a prompt edit re-runs APPRAISE instead of replaying the old prompt's answers. Synthesis
  recordings are keyed by the system prompt and the output grammar too (since 2026-10-14;
  before that a synthesis prompt edit silently replayed the old prompt's drafts). Extraction
  recordings are keyed by its prompt too since 2026-10-23, for the same reason.
  **A failed synthesis call is recorded too** (2026-10-28): the nth identical request in a case
  has its own slot (`key#n`; the first keeps the plain key, so older recordings replay as
  before), and a permanent failure is stored there with the answer `fit_to_contract` may fit.
  Before, only the success was recorded, so a replay answered the first call with it and took a
  different path from the live run (wt_072, wt_009). Outages are never stored.
- **The eval judge is `qwen2.5:14b-instruct`** (`evaluation/config.yaml`; fallback
  `llama3.1:8b`). Measured 2026-10-26 on the same runs: citation correctness 92% vs 73%
  (regression) and 86% vs 58% (pilot), no false hallucination flag where llama had one, ~2.5×
  slower. Its window grows with the prompt (`llm_judge.py::_context_for`, up to 32K), because
  Ollama silently keeps only ~num_ctx/2 of a longer prompt and the judge answered blind.
  Its certainty label is the weakest part: on the 107-case run about half of its
  "overconfident" calls were a missing topic, not overclaiming.
- **Keep the Mac awake through a long eval** (`caffeinate -i -s`). macOS stops the monotonic
  clock in sleep, so `case_timeout_seconds` cannot fire and durations lie: on 2026-10-27, wt_072
  did 21 minutes of work over 9.3 hours of wall clock, and wt_070's embedding call timed out
  across a sleep. A case far past the timeout is the sign; compare the trace's `duration_ms`
  with `finished_at - started_at`. **`-s` holds only on mains power**: on 2026-11-10 the Mac went to
  battery at 06:30 and slept on and off until 07:18, mid-case, with caffeinate running. Plug it in.

**A cached re-run replays any case whose requests did not change, in seconds** (wt_090 2 s, wt_011 12 s), so
it reports the same article again: a pilot after a small gate change measures only the cases it changes.
A new sample of every case needs `--mode live`, which also overwrites their recordings. The cached 19-case
re-run (2026-11-06, 3.5 min with the judge) changed only wt_098: citation correctness 89.0% → 90.2%,
citation recall 63.3% → 65.6%, groundedness 95.7% → 97.1%, verdicts still 13 of 19, certainty still 9.

**OpenAlex's key buys $1 a day, 10 credits a search, so ~1,000 searches, reset at midnight UTC, and suites whose terms changed spend it
fast: read `provider_outages` before trusting a report.** Three suites on 2026-11-07 ran without it on 34, 41 and 94 cases, which read as a
regression in the search change. The pair's literal phrase led to name splitting and
`POPULATION_HINTS` (see "One field can hold several names" in [app/retrieval/CLAUDE.md](../app/retrieval/CLAUDE.md)); this pilot's numbers predate them.
**A live case can pass `case_timeout_seconds` (1500) when little of it is recorded**: 5 of these 19 did
on first run, none of them caused by another run. Completed calls are recorded, so re-run them: delete
those traces, then `--resume`. **A fully live Bonsai suite needs 4800 s, not 1500**: wellness_core live on
2026-11-09 timed out 21 of 49 at 1500 s (median case 1375 s; appraisal ~194 s and synthesis ~393 s a call,
the same speeds as Oct 26). The Oct 26 run that is its comparison used 4800 s (`config_long.yaml`, only the
timeout differs), so re-run its timeouts with `--config <copy with 4800>` to compare like with like. `--rescore` also needs `--suite`, or it selects another suite's cases and
exits 2 with "no saved traces".

## Measurement log

**wellness_core, fully live, with the classifier and best-match fixes (2026-11-09/10, 50 cases): answer correctness 62.0% (31 of
50), against 64.0% (32) on 2026-10-26 and a target of 85%.** Neither fix moved it. On the pilot's 19: 14 (pilot 2026-11-08: 15; wt_087 fixed,
wt_061 and wt_095 lost on judge scores 5 → 3 with the same verdict, i.e. sample noise); on the other 31: 17 against 18 (3 gained, 4 lost).
Verdicts 39 of 50 (Oct 26: 41), judge correctness ≥4 38 (40). Same-code retrieval metrics: P@5 87.0%, nDCG 90.8%, MRR 97.4%, recall@5 36.1% on 18
labelled cases. No provider outage; code hashes unchanged from start to judge. Left failing: uncertainty 21, verdict 11, retrieval 11,
citation_correctness 9 (84.3%, critical, 90% needed), groundedness 8. `agent_loop_rate` 2%: wt_072 made 4 synthesis calls. wt_098 still
`supported` over a quorum of two small trials. One live sample per case moves ±2 cases by itself, so a one-case difference means nothing.
**Why 21 cases fail "uncertainty" and 11 fail the verdict (read from that run, nothing changed):** uncertainty = 4 are the keyword test
(`metrics/answers.py::hedged`, needs 2 of ~25 words) failing prose that plainly hedges (wt_001, 025, 044, 055: 0-1 hits in 236-724
words); 7 are a verdict above the label's range; 12 are the judge's "overconfident", every one of them paired with correctness 3 (it
never says "underconfident"), including `weak` articles whose summary says "not proven" (wt_015, wt_095). Verdict = 7 are
mixed/supported where the label says no_evidence/weak (judge correctness 5 on four of them), and 3 (wt_023b, 072, 092) are `claim_not_backed`
lowering a `mixed` the label accepts: it fired on 17, 3 needed it (wt_002, 055, 095), 3 were broken by it, so net 0 here against +10 in the
Oct 26 replay.
**Changed 2026-11-10: the uncertainty check no longer counts hedge words when the judge answered** (`metrics/answers.py::uncertainty_appropriate`):
on a hedge-expecting case it is now verdict in range and the judge not "overconfident"; the word count is the fallback only when the judge is off.
Re-scoring that run offline: `uncertainty_appropriate_rate` 58% → 66% (29 → 33 of 50), exactly the four plain-worded cases, nothing else moved;
answer correctness does not use it. Saved baselines were scored with the old rule, so a later uncertainty comparison against them is not like for like.
**The 7 verdicts above their label, read against their cited sources (titles and summaries, not full texts):** pipeline too high on 5 —
wt_098 (headline "real benefits" for rosemary water, which the label says no trial tested; cites a rat study and a hair-greying study),
wt_056 (cites 6 narrative reviews, 2 untyped, a rat study; the article itself says it "does not support the broad claim", but it ended
`answered_unvalidated`, and `claim_not_backed` only acts on a passing draft), wt_068 (the question compares zone 2 with intervals; no source
is about zone 2 and the `supported` verdict is for intervals alone), wt_035 (answers 16:8 eating, not the one-meal-a-day question; S12, a
mortality association abstract, is typed `rct`), wt_081 (six reviews of small studies, headline "shows promise" over a `supported` verdict).
Label too strict on 1 (wt_039: three meta-analyses do show ginger and turmeric lower inflammatory markers) and borderline on 1 (wt_082:
`mixed` over a body that says the evidence is too early). So two of the pipeline's faults are answering a neighbouring question (wt_035,
068) and a verdict taken from the strength of the sources, not from what they were about.
**Looked into (2026-11-10, read-only): the neighbouring-question pattern starts at extraction.** For wt_068 the extractor wrote
`product: "zone 2 training or high-intensity intervals"` and one claim, "improves VO2max": no subject and no comparison, so retrieval
found the plentiful side (intervals), 12 of 12 ranked sources are about it, and a `supported` verdict on that claim is the weakest-claim
rule working as designed. wt_035 is the same ("OMAD or 16:8" as the product, "affects safety" as a claim). It is rare: of 7 comparison
questions in the 50, 6 keep the comparison in a claim (5 verdicts right), only wt_068 loses it. No zone 2 or one-meal-a-day trial was in
either candidate pool, so the right answer was "thin", and nothing downstream knew a named side had no evidence. A crude "an alternative in
the product that no cited source mentions" detector flags 7 of 50 cases, but 5 of them already ended `weak`, and it misses wt_068/wt_035
(loose word match), so it is not a rule yet. Not built; the fix would be per-alternative claims in `ExtractionOutput` (its docstring is
prompt text), so the weakest-claim ceiling does the rest, and it reruns every retrieval suite. Conference abstracts typed as trials ("Abstract
P192: ...", wt_035 S12) are 2 of 3 conference-style titles across three runs: too rare to act on.
**Checked and not built (2026-11-10): a verdict ceiling from study size.** The appraisal records only a stance per source (no size, no
control group), so the only signal is the participant count in the abstract. Over the 12 `supported` articles in three runs (5 wrong),
it does not separate them: wt_061 is right with RCTs of n = 11-54, and wt_068 and wt_081 are wrong with meta-analyses and reviews of
hundreds to thousands. The wrong ones fail for other reasons (a neighbouring question, a side with no source, a label). Sample size is not a usable ceiling.
**Tried and reverted (2026-11-10): a "comparison note" in the per-question extraction prompt** (like `safety_note`, matched on
"better than / versus / compared with / as well as", 15 of 244 questions) asking for one named claim per side plus the comparison. Direct
extraction named both sides for most (wt_035: "OMAD is safe", "16:8 ... is safe", "OMAD is more effective than 16:8"), but not wt_068
after three wordings ("improves VO2max" twice, unnamed), and mt_007 got "unspecified reduces back pain". Both retrieval suites, honestly
measured: retrieval 90 → 89 of 114 (mp_008 lost), wellness 88 → 85 of 113 (wt_029 and wt_071 lost; wt_015 also fell, but all three providers
were down for it, so it proves nothing), no case gained, irrelevant-in-top-k per case 0.14 → 0.55 and 1.16 → 1.65: more claims, more
searches, more off-topic papers. **Trap found on the way: the extraction recording key
(`harness/llm.py::EvalExtractionClient`) holds only the notes it names (the safety note).** A new per-question note that is not added
there replays the old answers with 0 misses and the first suite run "showed no change" while measuring nothing. Add any new note to
the key, with a test that plants a recording under the old key.

**Removing "leave it out" from the coverage feedback was tried and reverted (2026-11-06, 4 cases).** No
verdict improved, and failed checks went 8 → 11. wt_098 fell back to 225 words and three failed checks more
(its re-prompt cited 3, so the first draft stayed), wt_095 gained a groundedness failure and "What other
studies found", wt_090 did not move; only wt_011 gained, 213 → 884 words and 3 → 19 cited. **One sentence of
feedback swung citations from 10 to 3 on one case and 3 to 19 on another, so one re-prompt sample cannot rank
two wordings.** Compare wordings over several samples per case, or not at all.

**Lowering everything to `weak` beat it on those 86 (78 verdicts, 62 correct, 64 changed): the labels lean
weak, so a rise in this metric alone proves little.** Still wrong: wt_002 (extraction dropped "people without
diabetes", so reviews of diabetics read as `supported`), and five the judge scores 3/5 "overconfident".
**Fresh 19-case pilot, 2026-11-06 (`--mode live`, report `20261106-150128`): correct answers 13 of 19 (68.4%), verdicts 16,
certainty 12, hallucination 0, citation correctness 87.3% (under its 90%), 6.5 h.** Without the rule the same drafts score 11
correct and 15 verdicts, assuming the judge ignores the badge, as the replay showed. It fired on 6: fixed wt_055 and wt_095, broke
wt_058's verdict, and changed nothing the labels count on wt_021, wt_044 and wt_061. **wt_061 is a false positive: "others found no
clear effect" describes a split, which is what `mixed` means.** It misses "not yet fully supported" (wt_082). The same 19 cases scored
12 and 14 correct in two Oct 26 runs, so 13 is no measured gain on them. No fresh draft said `supported`, though 14 had that ceiling.
**Reading all 19 (2026-11-07): 9 sound, 7 partial, 3 wrong (wt_002, wt_058, wt_087), and all three passed validation.** The judge's
3/5s are mostly right despite naming missing topics (wt_058, wt_087 right; wt_044, wt_082 partly; wt_090 wrong), so leave it alone; its
real flaw is false passes. **Extraction dropped part of the question in 5 of 19, and `query_understanding_accuracy` still reads 100%**:
"without diabetes" (wt_002, judged 5/5), "women" (wt_021), "528 Hz" (wt_055), "as retinol" (wt_090), "rosemary water" (wt_098). **An
activity or a pair as `product` searches as one literal phrase**: PubMed hits for hand-written alternatives, wt_025 0 → 147, wt_044 1 →
109, wt_058 3 → 105. So wt_058's 21 sources were all rightly off-topic, its article told readers the research does not support cutting
alcohol to lower blood pressure, and wt_025's template said "simply not been studied". **wt_087 said no large randomised trial shows
sunscreen prevents skin cancer while "Reduced melanoma after regular sunscreen use: randomized trial follow-up" (2011) sat in its pool,
outside the 22 sources the model was shown.** Traced 2026-11-07: second of 51 by similarity (0.863), 18th after the bonuses, 0.013 under
the top-12 cut, behind sun-behaviour reviews at ~0.78, because a review earns 0.15 to a trial's 0.08 and a 2011 paper no recency. Ranking
was left alone: one case. `claim_not_backed` now skips a sentence with "others" in it, which is wt_061's only change on replay.
