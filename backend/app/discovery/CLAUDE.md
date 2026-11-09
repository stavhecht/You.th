# Discovery: the MeSH trend scan

The first proposer: counts MeSH tags on new PubMed records and proposes topics.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies.

## Rules

**Trend discovery proposes; it never enqueues.** `discovery/scan.py::run_scan` counts MeSH tags on
newly-indexed PubMed records, ranks substances by acceleration, and writes `discovery_candidates`.
A reviewer promotes one from the console and *that* creates the run. This is invariant #1's
reasoning moved one step earlier — a human decides what gets written, not just what gets published
— and it is why there is no `--auto-enqueue` flag. The scan itself makes **no model calls at all**,
so it costs nothing; the spend is entirely in what a reviewer chooses to promote, and
`pipeline_runs.origin` is what makes that spend one `WHERE` against the token ledger.

**Two callers, one scan.** `scripts/scan_trends.py` is argparse and printing around `run_scan`;
`POST /console/discovery/scan` is a background task around it, driven by the desk's Run scan
button. The endpoint must **not** shell out to the script — `scripts/` is deliberately outside the
deployed image ([.dockerignore](../../.dockerignore)), so that button would work on a laptop and
500 in a container. Exposing a manual trigger at all is safe only because of the paragraph above:
a scan proposes, so the button spends PubMed requests and nothing of ours. `discovery/manual.py`
holds the runner, and three things about it are deliberate — it is **single-flight per process**
(two concurrent scans double this process's draw on NCBI's per-IP ceiling, which the worker shares,
to compute the same answer twice; the ledger's idempotence is what makes the per-*process* scope
merely wasteful rather than corrupting), its state lives **in memory, not in `discovery_scans`**
(that table is the window ledger — `next_window` reads only its succeeded rows — so a press must
not write to it), and a scan that raises lands on that state as an error string rather than as an
unhandled task exception. A restart therefore reports `idle`; `last_scan_at`, read from the ledger,
is what still answers "when did one last finish".

**The scan runs on a timer, and the button is now the exception rather than the only way.**
`discovery/schedule.py` starts a task in the **API process's** lifespan that fires
`manual_scan.start()` when the ledger's last *succeeded* scan is older than
`DISCOVERY_SCAN_INTERVAL_HOURS` (24). Automating this side is safe for exactly one reason, and it
is the paragraph above: **a scan proposes and never enqueues**, so the timer spends PubMed
requests and no tokens at all — the whole generative bill still sits behind a reviewer pressing
Generate draft. Automating the *promotion* would be a different decision entirely, and there is
still no `--auto-enqueue`. Four things about it:

- **It goes through `manual_scan`, not a second path.** One runner means the single-flight guard
  already covers press-versus-timer, and the desk's status line describes whichever scan is in
  flight without knowing what started it. A `ScanAlreadyRunning` from a race is caught and the
  tick does nothing.
- **Due-ness is read from the ledger, never from a timer since process start.** So a restart does
  not scan, a deploy loop does not scan per deploy, and a slot missed while the process was down
  heals on the next tick. Same shape as `discovery_overlap_days`.
- **The loop cannot die on a bad tick.** The symptom of a dead scheduler is *nothing happening*,
  which reads exactly like a quiet fortnight — so every tick is wrapped, and a naive `finished_at`
  from an older row is coerced to UTC rather than raising.
- **It lives in the API process because `manual_scan` does.** Running more than one API replica
  would give each its own scheduler and its own single-flight scope: wasteful, not corrupting,
  because the observation ledger is idempotent — but scaling out wants an advisory lock first.

**A suppression is explained on the desk, not in a terminal.** `ScanReport.suppressed` carries
every held-back topic with its reason, and `ManualScanState` used to drop it, leaving the note to
end "Run the CLI for the per-topic list" — a dead end on a deployed stack, since `scripts/` is
outside the image by design. The one screen that needed the list was the one screen that could not
have it. It now renders under the scan note: "0 proposals" and "0 proposals, and here are the three
I am holding back" are different screens, and only the second is trustworthy when it is empty.

Six things about it that are load-bearing:

- **Counts are `COUNT(DISTINCT pmid)` over a ledger, never incremented counters.** MeSH indexing
  lags PubMed entry by days to weeks, so every scan must re-read weeks a previous scan already
  covered — `discovery_overlap_days` (21) against a 14-day window. `discovery_observations` is
  keyed `(descriptor_ui, pmid)` and written `ON CONFLICT DO NOTHING`, which makes that overlap
  free, makes a missed cron slot self-heal, and makes the whole script idempotent. Verified
  against the live index: a second `--apply` run writes 0 observations and refreshes the same
  candidates. Records bucket on `entrez_date`, not on which scan found them, so a backlog cannot
  read as a surge.
- **The window is measured on `datetype=edat`, and the harvester is deliberately not a
  `ScholarlyProvider`.** `ScholarlyProvider.search` is contractually required to drop records with
  no abstract, and discovery wants exactly those — a MEDLINE record with no abstract carries its
  full heading list, and there were **21,689** such records in the supplements and vitamins nets
  when this was built. `discovery/harvest.py` therefore has its own esearch/efetch parse and shares
  only the *throttle* — which it must, because NCBI's ceiling is per IP and shared with the worker.
  Same shape as `scripts/check_retractions.py`. It also uses `sort=date`, not the pipeline's
  `sort=relevance`; putting either on a shared `SearchQuery` would let a future caller silently
  degrade every article's evidence base.
- **When the desk looks empty, the floor is the suspect, never the cap.** `rank_candidates` is
  asked for `limit * 3` precisely so suppression can drop already-decided topics and the next-best
  ones get pulled up — that behaviour has always been there. `discovery_max_candidates` (6) is a
  reviewer-flooding ceiling and is almost never what binds. Measured 2026-09-07 with the floor at
  4: the substance counts over a live 14-day window ran 9, 5, 5, 4, then 3, 3, 3, 3, then a long
  tail at 2 and 1 — so **four substances in the whole corpus** cleared it, three of their angles
  were already promoted, and a desk of 8 showed 3. At a floor of 3 the pool is ten and the desk
  fills. The dry run's "N of M above the floor" line is the number to read before touching either.
  Lowering it also makes the gaps in the hand-maintained `STOPLIST_UIS` visible — the first scan at
  3 proposed `antiviral agents for antioxidants`, two category abstractions pointed at each other.
- **No candidates until the baseline is deep enough.** Below `discovery_min_baseline_windows` (4)
  the scan writes observations and proposes *nothing*, with no volume-ranked fallback — a first
  scan can only rank by raw volume, which proposes vitamin D, creatine and omega-3, i.e. the three
  things a reviewer would have named unaided, arriving with the authority of a ranking.
  `--bootstrap --apply` builds it (~3 min, 6 months; the scorer only reads
  `discovery_baseline_windows` buckets, so a deeper backfill writes rows nothing reads).
- **`score = log2(lift) × log2(1 + n) × quality_weight`, and the product is the point.** Lift alone
  is dominated by tiny denominators (1→4 beats 30→90); volume alone re-proposes vitamin D forever.
  Every constant was guessed before any data existed, which is why the console shows the arithmetic
  ("17 papers, usually 3") and not the score.
- **A descriptor a seed query *names* can never be a finding**, and those anchors live on the seed
  in `discovery/seeds.py`, not in the stoplist — so editing a seed's terms cannot leave its own
  subject topping its own ranking. Measured: before this, "Plant Extracts" ranked first among
  botanicals with 146 of 665 papers. Substances are separated from biomarkers by MeSH *intervention
  qualifiers* (/administration & dosage, /therapeutic use…) plus `<ChemicalList>` membership, with
  `BOTANICAL_UIS` and `BIOMARKER_UIS` covering what that rule cannot see. Category abstractions
  (`Antineoplastic Agents`, `Capsules`) pass the rule cleanly and are stoplisted by hand; the real
  fix is a MeSH tree lookup against D27, which is a network hop per descriptor.
- **One proposal per (substance, *angle*), not per substance.** "Omega-3 for muscle
  recovery" and "omega-3 for skin" rest on different papers and reach different verdicts, and
  under the old identity promoting either silenced the substance so the other was never offered.
  Three things keep this from becoming "one substance, eight ways":
  `discovery_max_angles_per_substance` (2) caps exposure; an angle must clear
  `discovery_min_papers_per_angle` (3 — it sat below the substance floor until that floor came down
  to 3 on 2026-09-07, and equal is safe: it only means a substance at exactly the floor needs *all*
  its papers on one outcome to earn an angle, and otherwise falls back to a bare topic. Never set it
  *above* `discovery_min_papers`, which is unreachable by construction); and **`is_usable_angle`
  requires the outcome to
  map into `OUTCOME_HINTS`**, because everything not identified as a substance falls through to
  "outcome" and that is far too permissive for naming an article. Real proposals it now refuses:
  `vitamin d for cross-sectional studies` (a study design), `vitamin d for vitamin d deficiency`
  (tautological), `catechin for tea`. A refused angle is not a refused substance — it falls back to
  a bare topic, exactly as before angles existed.
- **Angles are read from the ledger over `discovery_angle_lookback_days` (90), not from the scan's
  own window.** Two different questions with two different timescales: "is omega-3 surging?" is a
  fortnight's question, "what is omega-3 studied for?" is a slow fact. Measured 2026-09-06 — a
  21-day window yielded four usable angles across the whole corpus, all on two papers; sixty days
  yielded caffeine against strength, endurance, cognition and heart rate. A sub-topic is a slice of
  an already-small count, so a short window structurally cannot see one. Costs no API requests.
  An angle's outcome must be a descriptor whose current `kind` is `outcome`, not merely an
  observation with `is_substance` false: stoplisted descriptors carry that flag too, and
  "Antioxidants" became "dietary fiber for antioxidants" that way.
- **The trend is counted over the trailing `discovery_window_days` only, not over everything
  harvested.** The harvest spans the overlap so late-indexed records reach the ledger; counting
  them into `current` compares a 21-to-35-day span against 14-day baseline buckets and inflates
  every lift by the ratio. It was doing exactly that until 2026-09-06 — vitamin D read as flat
  (1.0x) when it was in fact declining (0.8x). Both numbers look reasonable alone, which is why it
  survived a live run and a full test suite.
- **`discovery_candidates_one_live_per_angle` is the anti-repeat guarantee.** A trend that
  emerges keeps clearing the bar, so the scan writes `ON CONFLICT … DO UPDATE` and a re-detected
  trend *refreshes* its row rather than appearing three times at three scores. The index is partial
  on `proposed` so history is unconstrained — which is what `suppression_reason` reads: a promotion
  that produced an article suppresses permanently, a dismissal until **both** the cooloff elapses
  and the paper count has doubled. Time alone would let a slow-burn topic nag every fortnight.
  Suppression is keyed on the angle's *spoken keyword*, not its descriptor UI: MeSH is granular
  enough that "Skin Aging", "Skin Physiological Phenomena" and "Skin Absorption" are three UIs for
  one question, and a per-UI rule would let a dismissed angle return under six names. The index
  keys on the UI because that is the duplicate row it can see; the keyword is the editorial
  judgement on top.
- **Zero proposals is the ordinary outcome, and the scan now says why.** A floor, a quorum and a
  suppression rule exist to make most fortnights propose nothing — but on the console "0 proposals"
  read exactly like a scan that had failed. `_suppression_note` folds the count into
  `ScanReport.notes`, which the desk already renders. Without it the most common cause of an empty
  desk was the one cause the desk could not display.

The sharpest risk is `UnanchoredQuery`: a promoted topic that leads extraction to return an empty
`ingredients` and a vague `product` dies at a **non-retryable** stage failure, having spent the
extraction call. Topics are composed as `"<substance> for <outcome>"` with the substance bare and
first for that reason, and `tests/unitTest/test_discovery_topics.py` pins that every botanical still anchors
a query. It fails *visibly* — the run shows `failed` — so it costs one call, not a wrong article.
