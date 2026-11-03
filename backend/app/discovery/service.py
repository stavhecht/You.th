"""Persistence for trend discovery: the ledger, the baselines, the proposals.

Everything that touches the database lives here, and everything that decides
*what* is worth proposing lives in ``scoring.py``. The split is deliberate —
``suppression_reason`` in particular is a pure function over history the caller
has already loaded, so the dry run can explain a suppression without a second
query and the rule can be tested without a database.

One rule governs every write in this module: **counts are never incremented.**
Observations are keyed ``(descriptor_ui, pmid)`` and written ``ON CONFLICT DO
NOTHING``, so a scan re-reading weeks a previous scan already covered — which
every scan does, because MeSH indexing lags PubMed entry — is a no-op rather
than double counting.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.discovery.contracts import MeshRecord, ScoredCandidate
from app.discovery.scoring import baseline_from_buckets, window_starts
from app.discovery.topics import outcome_keyword_for
from app.discovery.vocab import classify_descriptor, is_too_common
from app.domain.enums import (
    DiscoveryCandidateStatus,
    DiscoveryDescriptorKind,
    DiscoveryScanMode,
    DiscoveryScanStatus,
)
from app.domain.models import (
    DiscoveryCandidate,
    DiscoveryDescriptor,
    DiscoveryObservation,
    DiscoveryScan,
    PipelineRun,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    """What a reviewer once did about a substance.

    Loaded from ``discovery_candidates`` and handed to ``suppression_reason``.
    A dataclass rather than the ORM row so the rule can be exercised without a
    database — which matters, because the rule is the difference between a
    proposal queue people read and one they learn to ignore.
    """

    status: DiscoveryCandidateStatus
    decided_at: datetime
    #: Papers in the window at the time of the decision, from the candidate's
    #: ``rationale``. The column will have moved on; this is what was known.
    paper_count_at_decision: int
    #: Whether the run this candidate produced ended up as a live article. A
    #: promotion whose run failed is not a reason to never propose it again.
    produced_article: bool = False


def angle_key(outcome_ui: str | None, outcome_name: str | None) -> str:
    """How two proposals are judged to be asking the same question.

    Prefers the ``OUTCOME_HINTS`` keyword the descriptor maps to, so the several
    MeSH headings that mean "skin" collapse to one angle. Falls back to the UI
    when there is no keyword, and to the empty string for a bare topic — both
    stricter than a keyword, never looser, which is the safe direction: a false
    match holds one proposal back for a cooloff, a missed one lets a dismissed
    question return next fortnight under a synonym.
    """
    if outcome_name:
        keyword = outcome_keyword_for(outcome_name)
        if keyword:
            return keyword
    return outcome_ui or ""


@dataclass(frozen=True, slots=True)
class Window:
    """The Entrez-date range one scan asks PubMed for."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def suppression_reason(
    history: list[Decision],
    *,
    current_count: int,
    now: datetime,
    cooloff_days: int,
) -> str | None:
    """Why this substance must not be proposed again, or ``None``.

    Two rules, and the asymmetry between them is the point.

    **A promotion is permanent** (unless its run never produced an article). An
    article exists or is being written; proposing the same substance again is
    the flooding failure in its purest form, and the reviewer has no way to tell
    from the desk that they already said yes.

    **A dismissal is conditional on two gates**, both of which must open: the
    cooloff has to have elapsed *and* the literature has to have at least
    doubled since. Time alone is not enough — a substance that trickles along at
    five papers a fortnight would come back every cooloff period with nothing
    new to say. Growth alone is not enough either, because a reviewer who said
    no last week should not be asked again this week on the strength of three
    more papers.

    Returns a human-readable reason rather than a bool, because the dry run
    prints it. "Suppressed" with no explanation is the output that makes someone
    delete the suppression logic.
    """
    if not history:
        return None
    latest = max(history, key=lambda decision: decision.decided_at)

    if latest.status is DiscoveryCandidateStatus.PROMOTED:
        if latest.produced_article:
            return f"promoted {latest.decided_at.date().isoformat()}"
        # The run failed and left nothing behind, so there is no article this
        # would duplicate. Falls through to be proposed again — which is the
        # only way a topic that died at an UnanchoredQuery ever gets retried.
        return None

    if latest.status is DiscoveryCandidateStatus.DISMISSED:
        until = latest.decided_at + timedelta(days=cooloff_days)
        if now < until:
            return (
                f"dismissed {latest.decided_at.date().isoformat()}, "
                f"cooloff to {until.date().isoformat()}"
            )
        threshold = latest.paper_count_at_decision * 2
        if current_count <= threshold:
            return (
                f"dismissed {latest.decided_at.date().isoformat()} at "
                f"{latest.paper_count_at_decision} papers; needs more than "
                f"{threshold} to return, has {current_count}"
            )
    return None


class DiscoveryService:
    """Reads and writes the four discovery tables.

    Does **not** commit. The script owns its transaction boundary, the same way
    the orchestrator owns the pipeline's — so a scan that fails partway leaves
    no half-written ledger.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the window ---------------------------------------------------------

    async def next_window(self, today: date, window_days: int, overlap_days: int) -> Window:
        """Where the next scan should look.

        Starts from the last *successful* scan's end, minus the overlap. A
        failed or half-finished scan must not move the mark, or the records it
        never got to would be skipped forever — which is why
        ``discovery_scans_recent_idx`` is partial on ``succeeded``.
        """
        last_end = await self._session.scalar(
            sa.select(sa.func.max(DiscoveryScan.window_end)).where(
                DiscoveryScan.status == DiscoveryScanStatus.SUCCEEDED
            )
        )
        if last_end is None:
            return Window(today - timedelta(days=window_days + overlap_days), today)
        return Window(min(last_end, today) - timedelta(days=overlap_days), today)

    async def completed_window_count(self, window_days: int) -> int:
        """How many whole windows of observations are on record.

        Measured from the ledger rather than by counting scan rows, because a
        bootstrap writes months of history in one scan — counting scans would
        say "1 window" after a backfill that actually built two years of it.
        """
        span = await self._session.execute(
            sa.select(
                sa.func.min(DiscoveryObservation.entrez_date),
                sa.func.max(DiscoveryObservation.entrez_date),
            )
        )
        earliest, latest = span.one()
        if earliest is None or latest is None:
            return 0
        return (latest - earliest).days // window_days

    # --- writing the ledger -------------------------------------------------

    async def start_scan(self, window: Window, mode: DiscoveryScanMode) -> DiscoveryScan:
        scan = DiscoveryScan(
            window_start=window.start,
            window_end=window.end,
            mode=mode,
            status=DiscoveryScanStatus.RUNNING,
        )
        self._session.add(scan)
        await self._session.flush()
        return scan

    async def finish_scan(
        self,
        scan: DiscoveryScan,
        *,
        status: DiscoveryScanStatus,
        records_seen: int = 0,
        descriptors_seen: int = 0,
        error: dict | None = None,
    ) -> None:
        scan.status = status
        scan.records_seen = records_seen
        scan.descriptors_seen = descriptors_seen
        scan.error = error
        scan.finished_at = datetime.now(UTC)
        await self._session.flush()

    async def record_observations(
        self, records: list[MeshRecord], kinds: dict[str, DiscoveryDescriptorKind]
    ) -> int:
        """Write one row per (descriptor, paper). Returns rows actually inserted.

        **Call ``upsert_descriptors`` first.** Observations carry a foreign key
        into the vocabulary table, so writing them first fails on the very first
        check tag — and it fails loudly, at the end of a scan that has already
        spent its whole PubMed budget.

        ``ON CONFLICT DO NOTHING`` is what makes the window overlap free, and
        what makes running the script twice in a row change nothing. The return
        value is genuinely useful for that reason: a second run over the same
        window reporting 0 new observations is the design working, not a bug.
        """
        rows = []
        for record in records:
            for hit in record.descriptors:
                kind = kinds.get(hit.ui, DiscoveryDescriptorKind.OUTCOME)
                rows.append(
                    {
                        "descriptor_ui": hit.ui,
                        "pmid": record.pmid,
                        "entrez_date": record.entrez_date,
                        "is_substance": kind is DiscoveryDescriptorKind.SUBSTANCE,
                        "intervention_qualifier": hit.intervention_qualifier,
                        "study_type": record.study_type,
                    }
                )
        if not rows:
            return 0

        written = 0
        # Chunked because a fortnight is ~30k rows and one statement that size
        # is a large parameter list for asyncpg to bind at once.
        for start in range(0, len(rows), 2000):
            statement = (
                insert(DiscoveryObservation)
                .values(rows[start : start + 2000])
                .on_conflict_do_nothing(index_elements=["descriptor_ui", "pmid"])
                .returning(DiscoveryObservation.pmid)
            )
            written += len((await self._session.execute(statement)).all())
        return written

    async def prune_observations(self, before: date) -> int:
        """Delete ledger rows with an entrez_date before ``before``. Returns how many.

        Nothing reads them: the scorer's oldest bucket and the angle lookback
        are both newer (``discovery_observation_retention_days`` is validated
        against both). The partial window index does not cover this predicate,
        so it is a sequential scan — once a day over a table that pruning keeps
        at a few months of rows.
        """
        result = await self._session.execute(
            sa.delete(DiscoveryObservation).where(DiscoveryObservation.entrez_date < before)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def upsert_descriptors(
        self, names: dict[str, str], kinds: dict[str, DiscoveryDescriptorKind]
    ) -> None:
        """Record the vocabulary, refreshing ``name`` and ``kind``.

        ``kind`` is deliberately overwritten rather than kept: it is our
        classification of a descriptor, and retuning ``vocab.py`` should take
        effect on the next scan rather than needing a manual repair.
        """
        rows = [
            {
                "ui": ui,
                "name": name,
                "kind": kinds.get(ui, DiscoveryDescriptorKind.OUTCOME),
            }
            for ui, name in names.items()
        ]
        if not rows:
            return
        for start in range(0, len(rows), 2000):
            chunk = rows[start : start + 2000]
            statement = insert(DiscoveryDescriptor).values(chunk)
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=["ui"],
                    set_={"name": statement.excluded.name, "kind": statement.excluded.kind},
                )
            )

    # --- baselines ----------------------------------------------------------

    async def baselines(
        self, uis: list[str], window_end: date, window_days: int, windows: int
    ) -> dict[str, float]:
        """Mean papers per historical window, per substance.

        One query for every descriptor and every bucket, rather than a query per
        descriptor: a fortnight yields a few hundred substances, and the
        per-descriptor version was a few hundred round trips for arithmetic
        Postgres can do in one pass.

        The *current* window is excluded — the earliest bucket boundary is
        ``window_end - window_days``, so a surge is never compared against
        itself.
        """
        if not uis or windows <= 0:
            return {}
        starts = window_starts(window_end, window_days, windows)
        earliest = starts[-1]
        rows = await self._session.execute(
            sa.select(
                DiscoveryObservation.descriptor_ui,
                # Integer division by the window length turns a date into its
                # bucket index, so bucketing is one expression rather than N
                # BETWEEN clauses.
                sa.cast(
                    (sa.literal(window_end) - DiscoveryObservation.entrez_date)
                    / window_days,
                    sa.Integer,
                ).label("bucket"),
                sa.func.count(sa.distinct(DiscoveryObservation.pmid)).label("papers"),
            )
            .where(
                DiscoveryObservation.descriptor_ui.in_(uis),
                DiscoveryObservation.is_substance.is_(True),
                DiscoveryObservation.entrez_date >= earliest,
                DiscoveryObservation.entrez_date < window_end - timedelta(days=window_days),
            )
            .group_by(DiscoveryObservation.descriptor_ui, "bucket")
        )

        buckets: dict[str, dict[int, int]] = defaultdict(dict)
        for ui, bucket, papers in rows:
            buckets[ui][int(bucket)] = papers

        # Absent buckets are zeros, not gaps. A substance nobody published on
        # for four fortnights has a baseline of zero for those, and dropping
        # them would compute the mean over only its busy windows — which is the
        # baseline of a different, much more active substance.
        result = {}
        for ui in uis:
            counts = [buckets[ui].get(index + 1, 0) for index in range(windows)]
            result[ui] = baseline_from_buckets(counts)
        return result

    async def angle_cooccurrence(
        self, uis: list[str], since: date, until: date
    ) -> tuple[dict[str, dict[str, int]], dict[str, str]]:
        """Which questions each substance's papers are asking, over a long look-back.

        Read from the ledger, not from this scan's harvest, and over a much wider
        span than the trend window — which is the whole point. The two are
        different questions with different natural timescales:

        * *Is this substance surging?* is a short-window question. Measured over
          a fortnight against a fortnightly baseline, or the comparison is
          meaningless.
        * *What is this substance studied for?* is a slow fact. Omega-3 has been
          a cardiovascular and an inflammation question for years.

        Measured 2026-09-06: a 21-day window yielded four usable angles across
        the whole corpus, all resting on two papers; sixty days yielded caffeine
        against strength, endurance, cognition and heart rate, creatine against
        body composition and strength, vitamin D against bone density and mood.
        Same literature, same rules — the short window simply cannot see a
        sub-topic, because a sub-topic is a slice of an already-small count.

        Costs no API requests: every observation was written by an earlier scan.

        **An outcome is a descriptor whose current kind is ``OUTCOME``**, not any
        observation with ``is_substance`` false. That flag is frozen when the row
        is written and is false for stoplisted descriptors too, so filtering on it
        alone admitted the stoplist as outcomes. Measured 2026-10-02: "Antioxidants"
        (stoplisted as a category abstraction) became the angle of "dietary fiber
        for antioxidants". Extraction turned that into the claim "increases
        antioxidant intake", and APPRAISE judged 11 of the 12 ranked papers
        off-topic. The resulting one-sentence article went to review as
        ``supported``, resting on fiber and cardiovascular meta-analyses.

        Returns:
            ``(cooccurrence, names)`` — substance UI → outcome UI → distinct
            papers, and the descriptor names both halves need.
        """
        if not uis:
            return {}, {}
        substances = (
            sa.select(DiscoveryObservation.descriptor_ui, DiscoveryObservation.pmid)
            .where(
                DiscoveryObservation.descriptor_ui.in_(uis),
                DiscoveryObservation.is_substance.is_(True),
                DiscoveryObservation.entrez_date >= since,
                DiscoveryObservation.entrez_date <= until,
            )
            .subquery()
        )
        outcomes = (
            sa.select(DiscoveryObservation.descriptor_ui, DiscoveryObservation.pmid)
            .where(
                DiscoveryObservation.is_substance.is_(False),
                DiscoveryObservation.entrez_date >= since,
                DiscoveryObservation.entrez_date <= until,
            )
            .subquery()
        )
        rows = await self._session.execute(
            sa.select(
                substances.c.descriptor_ui.label("substance"),
                outcomes.c.descriptor_ui.label("outcome"),
                DiscoveryDescriptor.name,
                sa.func.count(sa.distinct(substances.c.pmid)).label("papers"),
            )
            .select_from(substances)
            .join(outcomes, outcomes.c.pmid == substances.c.pmid)
            .join(DiscoveryDescriptor, DiscoveryDescriptor.ui == outcomes.c.descriptor_ui)
            .where(DiscoveryDescriptor.kind == DiscoveryDescriptorKind.OUTCOME)
            .group_by(
                substances.c.descriptor_ui,
                outcomes.c.descriptor_ui,
                DiscoveryDescriptor.name,
            )
        )
        cooccurrence: dict[str, dict[str, int]] = defaultdict(dict)
        names: dict[str, str] = {}
        for substance, outcome, name, papers in rows:
            cooccurrence[substance][outcome] = papers
            names[outcome] = name
        return dict(cooccurrence), names

    # --- the desk -----------------------------------------------------------

    async def decisions_for(
        self, uis: list[str], descriptor_names: dict[str, str] | None = None
    ) -> dict[tuple[str, str], list[Decision]]:
        """What reviewers have already said, keyed by **angle**.

        The key is ``(substance_ui, angle_key(outcome_name))``, not the
        substance — dismissing "omega-3 for skin" must not hold back "omega-3
        for muscle recovery", because they are different articles resting on
        different papers.

        The outcome half is the *spoken keyword*, not the descriptor UI. MeSH is
        granular enough that "Skin Aging", "Skin Physiological Phenomena" and
        "Skin Absorption" are three UIs for what a reader would call one
        question, and a per-UI rule would let the same angle come back under six
        names — the nagging the cooloff exists to prevent, wearing a disguise.
        The database's unique index still keys on the UI, because that is the
        duplicate *row* it can see; this is the editorial judgement on top.

        Args:
            uis: Substance UIs to load decisions for.
            descriptor_names: UI → MeSH heading, so a stored ``outcome_ui`` can
                be reduced to its keyword. Rows whose outcome is absent from it
                fall back to the raw UI, which is stricter, not looser.
        """
        if not uis:
            return {}
        rows = await self._session.execute(
            sa.select(
                DiscoveryCandidate.substance_ui,
                DiscoveryCandidate.outcome_ui,
                DiscoveryCandidate.status,
                DiscoveryCandidate.decided_at,
                DiscoveryCandidate.rationale,
                # Whether the promoted run actually produced an article. An
                # outer join, not a subquery on the candidate: a promotion whose
                # run failed — an UnanchoredQuery, a truncated synthesis — left
                # nothing behind, and suppressing it forever would make one bad
                # topic string permanently unwritable.
                PipelineRun.article_id,
            )
            .outerjoin(PipelineRun, PipelineRun.id == DiscoveryCandidate.pipeline_run_id)
            .where(
                DiscoveryCandidate.substance_ui.in_(uis),
                DiscoveryCandidate.status.in_(
                    [
                        DiscoveryCandidateStatus.PROMOTED,
                        DiscoveryCandidateStatus.DISMISSED,
                    ]
                ),
                DiscoveryCandidate.decided_at.is_not(None),
            )
        )
        names = descriptor_names or {}
        history: dict[tuple[str, str], list[Decision]] = defaultdict(list)
        for ui, outcome_ui, status, decided_at, rationale, article_id in rows:
            blob = rationale or {}
            history[(ui, angle_key(outcome_ui, names.get(outcome_ui or "")))].append(
                Decision(
                    status=status,
                    decided_at=decided_at,
                    # The angle's own count, falling back to the substance's for
                    # rows written before angles existed. The growth gate asks
                    # whether *this question* has more behind it than when it was
                    # turned down, so the substance total would set a bar the
                    # angle can never clear.
                    paper_count_at_decision=int(
                        blob.get("angle_paper_count") or blob.get("paper_count", 0)
                    ),
                    produced_article=article_id is not None,
                )
            )
        return dict(history)

    async def propose(self, candidates: list[ScoredCandidate], window: Window) -> int:
        """Write the ranked candidates, refreshing any already on the desk.

        The ``ON CONFLICT`` here is the anti-repeat guarantee doing its work: an
        angle that clears the bar again next fortnight updates its existing row
        — new score, new counts, same id, same place in the reviewer's list —
        rather than appearing twice.

        Conflict target is ``(substance_ui, coalesce(outcome_ui, ''))``, matching
        ``discovery_candidates_one_live_per_angle``. The coalesce is not
        cosmetic: ``outcome_ui`` is nullable and NULL never conflicts with NULL,
        so without it a substance with no usable outcome would insert a fresh
        bare proposal on every scan forever.
        """
        if not candidates:
            return 0
        rows = [
            {
                "substance_ui": candidate.substance_ui,
                "outcome_ui": candidate.outcome_ui,
                "topic": candidate.topic,
                "score": candidate.score,
                "paper_count": candidate.paper_count,
                "baseline_count": candidate.baseline_count,
                "rationale": candidate.rationale(window.start, window.end),
                "status": DiscoveryCandidateStatus.PROPOSED,
            }
            for candidate in candidates
        ]
        statement = insert(DiscoveryCandidate).values(rows)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    DiscoveryCandidate.substance_ui,
                    sa.func.coalesce(DiscoveryCandidate.outcome_ui, ""),
                ],
                index_where=DiscoveryCandidate.status == DiscoveryCandidateStatus.PROPOSED,
                set_={
                    "topic": statement.excluded.topic,
                    "score": statement.excluded.score,
                    "paper_count": statement.excluded.paper_count,
                    "baseline_count": statement.excluded.baseline_count,
                    "rationale": statement.excluded.rationale,
                },
            )
        )
        return len(rows)

    async def expire_absent(self, keep: list[tuple[str, str | None]]) -> int:
        """Retire live proposals this scan no longer ranks.

        Keyed on the **angle**, not the substance. Keying on the substance was
        correct while one substance meant one proposal and is now a leak: a scan
        that proposes "omega-3 for skin" but no longer ranks "omega-3 for muscle
        recovery" would leave the second sitting on the desk at last fortnight's
        score, indistinguishable from a live one.

        Expired rather than deleted: ``proposed`` has to mean "live now" or the
        desk accumulates stale scores, but the trail of what was offered is what
        makes the scorer tunable after the fact.

        Args:
            keep: ``(substance_ui, outcome_ui)`` pairs this scan is proposing.
                Empty expires every live proposal, which is correct — it means
                the scan ranked nothing.
        """
        angle = sa.tuple_(
            DiscoveryCandidate.substance_ui,
            sa.func.coalesce(DiscoveryCandidate.outcome_ui, ""),
        )
        statement = (
            sa.update(DiscoveryCandidate)
            .where(
                DiscoveryCandidate.status == DiscoveryCandidateStatus.PROPOSED,
                # Coalesced on both sides: a bare proposal has a NULL outcome,
                # and NULL never matches anything in an IN, so it would survive
                # every sweep and never expire.
                sa.not_(angle.in_([(ui, outcome or "") for ui, outcome in keep]))
                if keep
                else sa.true(),
            )
            .values(status=DiscoveryCandidateStatus.EXPIRED)
        )
        return (await self._session.execute(statement)).rowcount or 0


def classify_all(
    records: list[MeshRecord], *, frequency_ceiling: float
) -> tuple[dict[str, DiscoveryDescriptorKind], dict[str, str]]:
    """Classify every descriptor in a scan, and return their names.

    Aggregates the three per-paper signals across the whole corpus before
    deciding, because the substance rule is a *ratio* — a single paper tagging
    creatine with /administration & dosage says much less than sixty of them
    doing so.

    The document-frequency ceiling is applied here rather than in
    ``vocab.classify_descriptor`` because it is the only rule that needs to know
    about the corpus rather than the descriptor.
    """
    papers: dict[str, set[str]] = defaultdict(set)
    chemical: dict[str, set[str]] = defaultdict(set)
    qualifier: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}

    for record in records:
        for hit in record.descriptors:
            papers[hit.ui].add(record.pmid)
            names[hit.ui] = hit.name
            if hit.in_chemical_list:
                chemical[hit.ui].add(record.pmid)
            if hit.intervention_qualifier:
                qualifier[hit.ui].add(record.pmid)

    corpus = len({record.pmid for record in records})
    kinds: dict[str, DiscoveryDescriptorKind] = {}
    for ui, seen in papers.items():
        if is_too_common(len(seen), corpus, frequency_ceiling):
            kinds[ui] = DiscoveryDescriptorKind.STOPLISTED
            continue
        kinds[ui] = classify_descriptor(
            ui,
            papers=len(seen),
            chemical_list_papers=len(chemical[ui]),
            intervention_qualifier_papers=len(qualifier[ui]),
        )
    return kinds, names
