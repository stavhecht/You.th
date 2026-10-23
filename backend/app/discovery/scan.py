"""The trend scan itself — harvest, classify, score, propose.

Lifted out of ``scripts/scan_trends.py`` when the console grew a "run it now"
button. The script is now a printer around this function, and the console
endpoint is a background task around it, so there is **one** definition of what
a scan does. The alternative — an endpoint shelling out to ``python -m
scripts.scan_trends`` — cannot work here: ``scripts/`` is deliberately outside
the deployed image (see [.dockerignore](backend/.dockerignore)), so the button
would work on a laptop and 500 in a container.

Nothing here enqueues a run or spends a token. The output is rows in
``discovery_candidates``; a human turns one into an article. That is invariant
#1's reasoning one step earlier, and it is why a manual trigger is safe to put
on the desk at all.

Two properties the callers rely on:

**It is idempotent.** ``discovery_observations`` is keyed (descriptor, pmid) and
written ``ON CONFLICT DO NOTHING``, and candidates are ``DO UPDATE``. So a
reviewer pressing the button twice, or pressing it an hour after the cron slot,
changes nothing but the timestamps — which is what makes a manual trigger
something other than a way to corrupt the baseline.

**Progress is cosmetic; the report is the result.** ``on_progress`` exists for
the CLI's live output. Everything a caller needs to act on is on ``ScanReport``,
including the notes progress would have printed, so a caller that passes no sink
loses nothing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.discovery.contracts import MeshRecord, ScoredCandidate
from app.discovery.harvest import PubMedIndexHarvester
from app.discovery.scoring import rank_candidates, study_mix_of
from app.discovery.seeds import SeedQuery, select_seeds
from app.discovery.service import (
    DiscoveryService,
    Window,
    angle_key,
    classify_all,
    suppression_reason,
)
from app.domain.enums import (
    DiscoveryDescriptorKind,
    DiscoveryScanMode,
    DiscoveryScanStatus,
)
from app.retrieval.factory import (
    PUBMED_ANONYMOUS_RPS,
    PUBMED_KEYED_RPS,
    USER_AGENT,
    throttled_client,
)

logger = logging.getLogger(__name__)

#: A line of live output. Called during the scan; never load-bearing.
Progress = Callable[[str], None]


@dataclass
class ScanReport:
    """What one scan saw and wrote.

    Carries the intermediate counts as well as the outcome because the CLI
    prints them and the console shows two of them — and because "records: 0
    indexed" is the difference between a quiet fortnight and a broken harvester,
    which a bare candidate count cannot express.
    """

    window: Window
    mode: DiscoveryScanMode
    applied: bool

    seeds_queried: int = 0
    records_seen: int = 0
    descriptors_seen: int = 0
    kind_counts: dict[DiscoveryDescriptorKind, int] = field(default_factory=dict)
    substances_tracked: int = 0

    #: Above ``discovery_min_papers`` before ranking and suppression.
    above_floor: int = 0
    #: Whole windows of baseline on record. Under ``discovery_min_baseline_windows``
    #: the scan writes observations and proposes nothing — see ``rank_candidates``.
    baseline_windows: int = 0
    candidates: list[ScoredCandidate] = field(default_factory=list)
    #: ``(topic, why)`` for candidates a previous decision holds back.
    suppressed: list[tuple[str, str]] = field(default_factory=list)

    observations_written: int = 0
    #: Rows past `discovery_observation_retention_days`, deleted on success.
    observations_pruned: int = 0
    candidates_proposed: int = 0
    candidates_expired: int = 0

    #: Things that would have been shouted at a terminal — a truncated seed, an
    #: exhausted record budget. Kept so a caller with no progress sink still
    #: learns the ranking was computed over a slice of the window.
    notes: list[str] = field(default_factory=list)


def _suppression_note(suppressed: list[tuple[str, str]]) -> str:
    """One line saying why the desk is empty.

    Zero proposals is the *ordinary* outcome — a floor, a quorum and a
    suppression rule exist precisely to make it so — but on the console it was
    indistinguishable from a scan that had failed to run. This splits the count
    the two ways a reviewer can act on: things already written (nothing to do),
    and things they said no to (which come back, on a date).

    The nearest cooloff is named rather than the whole list, because it is the
    only actionable date in the set: it is when this screen can next change
    without new literature.
    """
    already_written = sum(1 for _, why in suppressed if why.startswith("promoted"))
    dismissed = len(suppressed) - already_written
    parts = []
    if already_written:
        parts.append(f"{already_written} already written")
    if dismissed:
        # The reasons carry "cooloff to YYYY-MM-DD"; surface the soonest.
        dates = sorted(
            why.split("cooloff to ")[1].strip()
            for _, why in suppressed
            if "cooloff to " in why
        )
        soonest = f", next returns {dates[0]}" if dates else ""
        parts.append(f"{dismissed} dismissed{soonest}")
    return (
        f"{len(suppressed)} candidate(s) suppressed by an earlier decision "
        f"({'; '.join(parts)})."
    )


def build_harvester(
    settings: Settings, http: httpx.AsyncClient
) -> PubMedIndexHarvester:
    """The harvester behind the shared throttle.

    Reuses ``retrieval/factory.py``'s pacing rather than opening a raw client,
    for the same reason ``check_retractions`` does: NCBI's ceiling is per IP and
    shared with the pipeline worker, and a sweep must not be the thing that gets
    the address blocked. That matters more now than it did as a cron-only
    script — the console button can be pressed while a run is retrieving.
    """
    api_key = settings.pubmed_api_key or None
    return PubMedIndexHarvester(
        throttled_client(
            settings,
            http,
            "pubmed",
            PUBMED_KEYED_RPS if api_key else PUBMED_ANONYMOUS_RPS,
        ),
        api_key,
    )


async def harvest_window(
    harvester: PubMedIndexHarvester,
    seeds: tuple[SeedQuery, ...],
    window: Window,
    *,
    max_records: int,
    on_progress: Progress | None = None,
    notes: list[str] | None = None,
) -> tuple[list[MeshRecord], int]:
    """Every indexed record in the window, across every seed.

    Seeds are queried **sequentially**, not concurrently. Sequential is fast
    enough at this volume, and concurrency inside one process is one more way to
    exceed a ceiling that nothing here can observe.

    ``max_records`` is a hard request budget rather than a tuning knob. It is
    reported — to ``notes`` as well as to the progress sink — because a silently
    truncated sweep is a ranking computed over an arbitrary slice of the window.
    """
    seen: set[str] = set()
    records: list[MeshRecord] = []
    queried = 0

    def note(message: str) -> None:
        """Structured, not printed.

        Notes go to the report and the caller decides how to surface them —
        ``on_progress`` already marks a truncated seed inline, and a sink that
        also received these would print each one twice.
        """
        if notes is not None:
            notes.append(message)

    for seed in seeds:
        if len(seen) >= max_records:
            note(f"record budget ({max_records}) reached; {seed.name} not queried")
            continue
        queried += 1
        total, ids = await harvester.count_and_ids(seed, window.start, window.end)
        collected = list(ids)
        while len(collected) < total and len(seen) + len(collected) < max_records:
            _, page = await harvester.count_and_ids(
                seed, window.start, window.end, retstart=len(collected)
            )
            if not page:
                break
            collected.extend(page)

        fresh = [pmid for pmid in collected if pmid not in seen]
        seen.update(fresh)
        batch = await harvester.fetch_mesh(fresh[: max(0, max_records - len(records))])
        records.extend(batch)
        if len(collected) < total:
            note(f"{seed.name}: {len(collected)} of {total} records fetched")
        if on_progress is not None:
            truncated = " (truncated)" if len(collected) < total else ""
            on_progress(
                f"  {seed.name:<18} {total:>6} matched, {len(fresh):>5} new, "
                f"{len(batch):>5} indexed{truncated}"
            )
    return records, queried


def bootstrap_windows(window: Window, window_days: int, months: int) -> list[Window]:
    """Window-sized buckets walking back ``months`` from the current window.

    Bucketed rather than one huge range because esearch caps ``retmax`` at
    10,000 per query — a two-year sweep of the supplements net is well past
    that, and the overflow would be silent.
    """
    buckets = []
    end = window.end
    earliest = window.end - timedelta(days=months * 30)
    while end > earliest:
        start = end - timedelta(days=window_days)
        buckets.append(Window(start, end))
        end = start
    return buckets


def aggregate(
    records: list[MeshRecord], kinds: dict[str, DiscoveryDescriptorKind]
) -> tuple[
    dict[str, int],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
    dict[str, list[str]],
]:
    """Per-substance counts, study mixes, co-occurring outcomes and PMIDs.

    One pass over the records. Counts are over *distinct* PMIDs because a paper
    tagging creatine twice — once as a major topic, once with a qualifier — is
    still one paper's worth of evidence.
    """
    papers: dict[str, set[str]] = defaultdict(set)
    types: dict[str, list] = defaultdict(list)
    cooccurrence: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for record in records:
        present = {hit.ui for hit in record.descriptors}
        subs = [ui for ui in present if kinds.get(ui) is DiscoveryDescriptorKind.SUBSTANCE]
        outs = [ui for ui in present if kinds.get(ui) is DiscoveryDescriptorKind.OUTCOME]
        for ui in subs:
            if record.pmid not in papers[ui]:
                types[ui].append(record.study_type)
            papers[ui].add(record.pmid)
            for outcome in outs:
                cooccurrence[ui][outcome] += 1

    return (
        {ui: len(seen) for ui, seen in papers.items()},
        {ui: study_mix_of(values) for ui, values in types.items()},
        {ui: dict(inner) for ui, inner in cooccurrence.items()},
        {ui: sorted(seen)[:5] for ui, seen in papers.items()},
    )


async def run_scan(
    *,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    apply: bool,
    bootstrap: bool = False,
    window_days: int | None = None,
    limit: int | None = None,
    max_records: int | None = None,
    seed_names: Sequence[str] = (),
    backfill_months: int = 4,
    today: date | None = None,
    on_progress: Progress | None = None,
) -> ScanReport:
    """One scan, start to finish.

    The network phase runs outside any session — it is minutes long, and holding
    a connection open across it would tie up the pool for the API and the worker
    both. The write phase is a single transaction, so a scan that fails partway
    leaves no half-written ledger; ``apply=False`` rolls it back after doing all
    the same work, which is what makes the dry run a real rehearsal rather than
    an estimate.
    """
    window_days = window_days or settings.discovery_window_days
    limit = limit if limit is not None else settings.discovery_max_candidates
    max_records = max_records or settings.discovery_max_records_per_scan
    seeds = select_seeds(list(seed_names) or settings.discovery_seeds)
    today = today or date.today()

    async with factory() as session:
        window = await DiscoveryService(session).next_window(
            today, window_days, settings.discovery_overlap_days
        )

    mode = DiscoveryScanMode.BOOTSTRAP if bootstrap else DiscoveryScanMode.SCAN
    report = ScanReport(window=window, mode=mode, applied=apply)
    windows = (
        bootstrap_windows(window, window_days, backfill_months) if bootstrap else [window]
    )

    if on_progress is not None:
        on_progress(
            f"window {window.start} -> {window.end} (edat), overlap "
            f"{settings.discovery_overlap_days}d, mode {mode.value}"
        )
        if bootstrap:
            on_progress(f"backfilling {len(windows)} window(s) of {window_days}d each\n")

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as http:
        harvester = build_harvester(settings, http)
        records: list[MeshRecord] = []
        for bucket in windows:
            if bootstrap and on_progress is not None:
                on_progress(f"  -- {bucket.start} -> {bucket.end}")
            batch, queried = await harvest_window(
                harvester,
                seeds,
                bucket,
                max_records=max_records,
                on_progress=on_progress,
                notes=report.notes,
            )
            records.extend(batch)
            report.seeds_queried = max(report.seeds_queried, queried)

    kinds, names = classify_all(
        records, frequency_ceiling=settings.discovery_document_frequency_ceiling
    )
    report.records_seen = len(records)
    report.descriptors_seen = len(kinds)
    report.kind_counts = {
        kind: sum(1 for value in kinds.values() if value is kind)
        for kind in DiscoveryDescriptorKind
    }
    report.substances_tracked = report.kind_counts[DiscoveryDescriptorKind.SUBSTANCE]

    async with factory() as session:
        service = DiscoveryService(session)
        scan = await service.start_scan(window, mode)

        # Descriptors first: discovery_observations has a foreign key into
        # discovery_descriptors, so the vocabulary has to exist before anything
        # can reference it.
        await service.upsert_descriptors(names, kinds)
        report.observations_written = await service.record_observations(records, kinds)

        if not bootstrap:
            # Count the trend over the trailing `window_days` only, not over
            # everything harvested. The harvest deliberately spans the overlap
            # so late-indexed records reach the ledger, but counting them into
            # `current` compares a 21-to-35-day span against 14-day baseline
            # buckets — inflating every lift by the ratio, and silently, because
            # both numbers look reasonable on their own.
            counted_from = window.end - timedelta(days=window_days)
            in_window = [r for r in records if r.entrez_date >= counted_from]
            current, mixes, _, pmids = aggregate(in_window, kinds)
            report.above_floor = sum(
                1 for n in current.values() if n >= settings.discovery_min_papers
            )
            report.baseline_windows = await service.completed_window_count(window_days)
            baselines = await service.baselines(
                list(current), window.end, window_days, settings.discovery_baseline_windows
            )
            # Angles come from the ledger over a much longer look-back than the
            # trend window. A sub-topic is a slice of an already-small count, so
            # a fortnight cannot see one — measured 2026-09-06, a 21-day window
            # produced four usable angles across the whole corpus against
            # sixty days' worth of caffeine-by-strength, creatine-by-body-
            # composition and vitamin-D-by-bone-density. Reads written
            # observations, so it costs no PubMed requests.
            cooccurrence, angle_names = await service.angle_cooccurrence(
                [ui for ui, n in current.items() if n >= settings.discovery_min_papers],
                window.end - timedelta(days=settings.discovery_angle_lookback_days),
                window.end,
            )
            names = {**angle_names, **names}

            ranked = rank_candidates(
                current_counts=current,
                baseline_counts=baselines,
                descriptor_names=names,
                study_mixes=mixes,
                outcome_cooccurrence=cooccurrence,
                pmids=pmids,
                min_papers=settings.discovery_min_papers,
                min_papers_per_angle=settings.discovery_min_papers_per_angle,
                max_angles_per_substance=settings.discovery_max_angles_per_substance,
                baseline_windows_available=report.baseline_windows,
                min_baseline_windows=settings.discovery_min_baseline_windows,
                # Over-fetch so suppression can remove some without silently
                # shrinking the desk below the cap the reviewer expects.
                limit=limit * 3,
            )

            if report.baseline_windows < settings.discovery_min_baseline_windows:
                report.notes.append(
                    f"no candidates emitted; baseline has {report.baseline_windows} "
                    f"of {settings.discovery_min_baseline_windows} windows. "
                    "Build it with --bootstrap --apply."
                )
            else:
                history = await service.decisions_for(
                    [candidate.substance_ui for candidate in ranked], names
                )
                now = datetime.now(UTC)
                for candidate in ranked:
                    reason = suppression_reason(
                        history.get(
                            (
                                candidate.substance_ui,
                                angle_key(candidate.outcome_ui, candidate.outcome_name),
                            ),
                            [],
                        ),
                        # The angle's own depth, not the substance's: the growth
                        # gate asks whether *this question* has more behind it
                        # than when it was turned down.
                        current_count=candidate.angle_paper_count,
                        now=now,
                        cooloff_days=settings.discovery_dismiss_cooloff_days,
                    )
                    if reason:
                        report.suppressed.append((candidate.topic, reason))
                    elif len(report.candidates) < limit:
                        report.candidates.append(candidate)

                # Suppression is the *usual* reason a scan proposes nothing, and
                # until this note existed it was the one reason the desk could
                # not see: the console shows `notes`, not `suppressed`, so "0
                # proposals" read exactly like a broken scan. Counted rather than
                # listed — a reviewer needs to know why there is nothing, not
                # which six things they already dealt with.
                if report.suppressed:
                    report.notes.append(_suppression_note(report.suppressed))

        if not apply:
            await session.rollback()
            return report

        report.candidates_proposed = await service.propose(
            scan.id, report.candidates, window
        )
        report.candidates_expired = (
            await service.expire_absent(
                [(c.substance_ui, c.outcome_ui) for c in report.candidates]
            )
            if not bootstrap
            else 0
        )
        report.observations_pruned = await service.prune_observations(
            window.end - timedelta(days=settings.discovery_observation_retention_days)
        )
        await service.finish_scan(
            scan,
            status=DiscoveryScanStatus.SUCCEEDED,
            seeds_queried=report.seeds_queried,
            records_seen=report.records_seen,
            descriptors_seen=report.descriptors_seen,
            candidates_emitted=report.candidates_proposed,
        )
        await session.commit()

    # The scan's only trace in the log stream: the scheduler logs that it started,
    # and a zero here is what tells a quiet fortnight from a broken harvester.
    logger.info(
        "trend scan finished: %d records, %d proposed, %d expired, %d observations pruned",
        report.records_seen,
        report.candidates_proposed,
        report.candidates_expired,
        report.observations_pruned,
    )
    return report
