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
  with `finished_at - started_at`.

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
those traces, then `--resume`. `--rescore` also needs `--suite`, or it selects another suite's cases and
exits 2 with "no saved traces".

## Measurement log

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
