"""Scan newly-indexed PubMed literature for emerging wellness/sports trends.

Ranks substances by how fast their evidence base is growing and proposes the top
ones to a reviewer, who decides which become articles. **Nothing here enqueues a
run or spends a token** — the output is rows in ``discovery_candidates``, and a
human turns one into an article from the console.

Dry by default::

    python -m scripts.scan_trends                      # report only
    python -m scripts.scan_trends --apply              # write observations + candidates
    python -m scripts.scan_trends --bootstrap --apply  # build the baseline, propose nothing
    python -m scripts.scan_trends --seed botanicals    # one net, for debugging

**The scan itself lives in `app/discovery/scan.py`**, not here. This file is
argparse and printing around ``run_scan``; the console's "Run scan" button is a
background task around the same function. A second implementation behind the
button would be a second answer to "what is a scan", free to drift from the one
the cron slot runs.

Two things differ from the sibling maintenance scripts, both worth knowing
before running this:

**The dry run still makes the network calls.** There is no way to score without
them — unlike ``reclassify_sources``, whose dry run is free. It writes nothing.

**``--bootstrap`` refuses to run without ``--apply``**, because a dry bootstrap
is 400+ PubMed requests producing output nobody keeps.

Schedule it weekly rather than fortnightly. Cron has no fortnightly expression
and the usual parity hack needs ``%`` escaped or the line silently never runs —
the most common way a cron job dies unnoticed. Cadence is not a correctness
variable here: the observation ledger is keyed (descriptor, pmid), so a scan
re-reading a window a previous scan already counted changes nothing, and
``--limit`` controls how much reaches the desk independently.

    15 6 * * 1 cd /srv/youth/backend && /srv/youth/.venv/bin/python \\
        -m scripts.scan_trends --apply >> /var/log/youth/scan_trends.log 2>&1

06:15 Monday on purpose: outside the hours a reviewer is queueing runs, so the
scan and the worker are not competing for the same NCBI per-IP ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.discovery.contracts import ScoredCandidate
from app.discovery.scan import ScanReport, run_scan
from app.discovery.seeds import SEEDS_BY_NAME
from app.discovery.topics import humanise_descriptor
from app.domain.enums import DiscoveryDescriptorKind, DiscoveryScanMode
from app.retrieval.query_builder import MESH_HINTS


def _print_candidates(
    candidates: list[ScoredCandidate], total_above_floor: int, min_papers: int
) -> None:
    print(
        f"\ncandidates ({len(candidates)} of {total_above_floor} above the "
        f"{min_papers}-paper floor):\n"
    )
    if not candidates:
        return
    print("  score  now  base  lift  topic")
    unhinted = []
    for candidate in candidates:
        known = humanise_descriptor(candidate.substance_name) in MESH_HINTS
        mark = "" if known else "  *"
        if not known:
            unhinted.append(candidate.substance_name)
        mix = ", ".join(f"{count} {name}" for name, count in candidate.study_mix.items())
        print(
            f"  {candidate.score:5.2f} {candidate.paper_count:4d} "
            f"{candidate.baseline_count:5.1f} {candidate.lift:4.1f}x  "
            f"{candidate.topic:<38} {candidate.substance_ui}  [{mix}]{mark}"
        )
    if unhinted:
        # Grows that vocabulary from what discovery actually finds rather than
        # from guesses — and it is what TemplateQueryStrategy will use to build
        # a real MeSH query if the candidate is promoted.
        print(
            f"\n  * not in MESH_HINTS — consider adding "
            f"(retrieval/query_builder.py): {', '.join(sorted(set(unhinted)))}"
        )


def _print_report(report: ScanReport, *, min_papers: int, quiet: bool) -> None:
    counts = report.kind_counts
    print(
        f"\nrecords: {report.records_seen} indexed\n"
        f"descriptors: {report.descriptors_seen} seen — "
        f"{counts[DiscoveryDescriptorKind.SUBSTANCE]} substance, "
        f"{counts[DiscoveryDescriptorKind.OUTCOME]} outcome, "
        f"{counts[DiscoveryDescriptorKind.STOPLISTED]} stoplisted"
    )

    if report.mode is not DiscoveryScanMode.BOOTSTRAP:
        _print_candidates(report.candidates, report.above_floor, min_papers)
        if report.suppressed and not quiet:
            print("\nsuppressed:")
            for topic, reason in report.suppressed:
                print(f"  {topic:<38} {reason}")

    # Printed at the end rather than inline, in both modes: a truncated sweep is
    # a ranking computed over an arbitrary slice of the window, and it must not
    # be the line that scrolled past twenty seed rows ago.
    for note in report.notes:
        print(f"\n  ! {note}")

    if not report.applied:
        print(
            f"\nWould write {report.observations_written} observation(s) and "
            f"{len(report.candidates)} candidate(s)."
        )
        print("Dry run. Re-run with --apply to write.")
        return

    print(
        f"\nWrote {report.observations_written} observation(s), refreshed "
        f"{report.candidates_proposed} candidate(s), expired "
        f"{report.candidates_expired}. Substances tracked: "
        f"{report.substances_tracked}."
    )


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the results")
    parser.add_argument(
        "--window-days", type=int, help="override the window (no prior scan only)"
    )
    parser.add_argument("--limit", type=int, help="candidates to emit")
    parser.add_argument("--quiet", action="store_true", help="counts only")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="backfill observations only; propose nothing",
    )
    # Six months, not two years. The scorer only ever reads
    # `discovery_baseline_windows` buckets (six fortnights ≈ 3 months), so a
    # deeper backfill writes hundreds of thousands of rows nothing reads and
    # spends a proportional amount of somebody else's API budget to do it.
    # Raise it only alongside that setting.
    # Four months: the ledger keeps `discovery_observation_retention_days` (120)
    # and a succeeded scan prunes the rest, so a deeper backfill is deleted as
    # soon as it is written.
    parser.add_argument("--backfill-months", type=int, default=4)
    parser.add_argument(
        "--seed",
        action="append",
        default=[],
        choices=sorted(SEEDS_BY_NAME),
        help="restrict to one net; repeatable",
    )
    parser.add_argument("--max-records", type=int, help="hard request budget")
    args = parser.parse_args()

    if args.bootstrap and not args.apply:
        parser.error(
            "--bootstrap needs --apply: a dry backfill is hundreds of PubMed "
            "requests producing output nobody keeps."
        )

    settings = get_settings()
    report = await run_scan(
        settings=settings,
        factory=get_session_factory(),
        apply=args.apply,
        bootstrap=args.bootstrap,
        window_days=args.window_days,
        limit=args.limit,
        max_records=args.max_records,
        seed_names=args.seed,
        backfill_months=args.backfill_months,
        on_progress=None if args.quiet else print,
    )
    _print_report(report, min_papers=settings.discovery_min_papers, quiet=args.quiet)
    return 0


async def main() -> int:
    """Wraps ``_run`` so the engine is disposed inside the same event loop.

    Disposing from a ``finally`` around ``asyncio.run`` starts a *second* loop
    and asyncpg's connections belong to the first, which surfaces as "attached
    to a different loop" after the work has already succeeded.
    """
    try:
        return await _run()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
