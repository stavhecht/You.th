"""The weekly social analysis: read new posts, link topics, measure, propose.

1. **Read** posts from the last three weeks that have no cached reading
   (``intel/reader.py``): the topic each is about, and any products it names.
   Products go to product discovery's tables from here, so a post is read once.
2. **Measure** every topic with posts (``social/analytics.py``), all numbers
   deterministic, coverage read from the daily runs.
3. **Propose** topics with at least ``social_min_posts`` posts this week, that
   are more than a category we search for (``too_broad``), ranked by momentum,
   then independent creators, then view velocity — after
   **suppression**, which is novelty only: a promoted topic is never proposed
   again, a dismissed one not within the cooloff unless its creators doubled.
   Suppression never changes a number; it decides what is shown again.
4. **Write the question** each proposed topic's article would answer
   (``social/questions.py``), once per topic.
5. **Assess evidence** for the top few: how much the search for the question's
   article would find (PubMed, Europe PMC when it is down), on the research
   agent's ``evidence_status`` ladder. Stored beside the metrics, never folded into
   them: popularity says nothing about efficacy, and literature volume says
   nothing about whether a claim is true.

Nothing here enqueues a pipeline run. A reviewer promoting a candidate does.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.contracts import ExtractionInput
from app.domain.enums import RunStatus
from app.domain.models import PipelineRun, SocialCandidate, SocialPost, SocialTopic
from app.intel.reader import ReadCall, read_texts, worth_reading
from app.llm.base import ExtractionClient
from app.products.contracts import CATEGORY_INFO
from app.products.store import Mention, record_mentions, refresh_statuses
from app.research.contracts import EvidenceCounts
from app.research.evidence import DEEP_CLAIMS, TermsEvidence
from app.research.normalize import normalize_query, singular
from app.research.providers.base import ProviderUnavailable
from app.research.scoring import evidence_score, evidence_status
from app.research.seeds import SEEDS
from app.research.trends.phrases import GENERIC_WORDS
from app.retrieval.query_builder import QueryStrategy, TemplateQueryStrategy, UnanchoredQuery
from app.social import analytics
from app.social.contracts import EvidenceAssessment
from app.social.questions import QuestionCall, same_question, write_questions
from app.social.store import (
    RunReport,
    link,
    load_coverage,
    load_topic_posts,
    post_text,
    tag_known_topics,
    upsert_topic,
)

MOMENTUM_ORDER = ("rising", "steady", "insufficient_history", "declining")
EVIDENCE_RECENT_DAYS = 365
#: An outcome made only of these promises nothing an article could check. Kept
#: tiny: GENERIC_WORDS also holds "muscle" and "heart", and "protein for muscle"
#: is a real angle (simulated on the 2026-11-05 run).
_EMPTY_OUTCOME_WORDS = frozenset(
    {
        "health",
        "healthy",
        "fitness",
        "nutrition",
        "wellness",
        "wellbeing",
        "workout",
        "exercise",
    }
)


def _words(text: str) -> set[str]:
    return {singular(word) for word in normalize_query(text).split()}


#: What the daily run searches for by name (not the recent topics it also watches).
_SEARCHED = [
    _words(phrase)
    for phrase in (
        *(seed for group in SEEDS.values() for seed in group),
        *(phrase for phrase, _ in CATEGORY_INFO.values()),
    )
]


def too_broad(subject: str, outcome: str) -> bool:
    """A category with no real angle: "Protein", "Smart ring", "Sleep for sleep".

    The daily run searches these words, and the word tagger then hands such a
    topic every post that search found, so it tops the ranking by measuring our
    own search budget (measured 2026-11-05: 6 of the top 10).
    """
    subject_words = _words(subject)
    if not subject_words or _words(outcome) - subject_words - _EMPTY_OUTCOME_WORDS:
        return False
    return subject_words <= GENERIC_WORDS or any(subject_words <= words for words in _SEARCHED)


#: A promotion whose draft failed left no article, so nothing holds its topic back. It is
#: the only way such a topic is ever tried again, as for the MeSH scan's. A deleted run
#: (NULL) or one still in flight counts as a decision that stands.
_draft_failed = exists().where(
    PipelineRun.id == SocialCandidate.pipeline_run_id,
    PipelineRun.status.in_((RunStatus.FAILED, RunStatus.CANCELLED)),
)


def suppression_reason(
    status: str,
    decided_at: datetime,
    *,
    creators_then: int,
    creators_now: int,
    now: datetime,
    cooloff_days: int,
) -> str | None:
    """Why a decided topic is not proposed again, or None. Novelty only."""
    if status == "promoted":
        return f"promoted on {decided_at.date()}"
    if status == "dismissed" and now - decided_at < timedelta(days=cooloff_days):
        if creators_now >= 2 * max(1, creators_then):
            return None
        return (
            f"dismissed on {decided_at.date()}; {creators_now} creators now, "
            f"{creators_then} then (needs twice as many to return)"
        )
    return None


async def analyse_week(
    session: AsyncSession,
    report: RunReport,
    *,
    read_call: ReadCall | None,
    question_call: QuestionCall | None,
    evidence: LiteratureCheck | None,
    run_id: str,
    settings: Settings,
    now: datetime,
) -> None:
    since = now - timedelta(days=analytics.WINDOW_DAYS * analytics.WINDOWS)
    posts = {
        post.id: post
        for post in (
            await session.execute(
                # Newest first: the reader's budget is a few hundred texts a week,
                # and the backlog that does not fit should be the oldest posts.
                select(SocialPost)
                .where(SocialPost.published_at >= since)
                .order_by(SocialPost.published_at.desc())
            )
        ).scalars()
    }
    texts = {pid: post_text(p.title, p.description) for pid, p in posts.items()}
    result = await read_texts(
        session,
        read_call,
        {pid: text for pid, text in texts.items() if worth_reading(text)},
        max_batches=settings.social_max_reading_batches,
    )
    report.sources["reader"] = result.log(configured=read_call is not None)
    mentions: list[Mention] = []
    for post_id, reading in result.readings.items():
        post = posts[post_id]
        topic = reading.get("topic")
        if topic:
            topic_id = await upsert_topic(session, now=now, **topic)
            await link(session, post_id, topic_id)
        mentions.extend(
            Mention(
                **product,
                source_kind=post.platform,
                source_url=post.url,
                publisher=post.creator_name,
                published_at=post.published_at,
                social_post_id=post.id,
                text=texts[post_id],
            )
            for product in reading.get("products") or []
        )
    touched = await record_mentions(session, mentions, now=now)
    await refresh_statuses(session, touched, now=now)
    # Count every week by the same rule: a topic found this week gets last
    # week's matching posts too, or its first full week reads as a surge.
    report.stats["tagged"] = await tag_known_topics(session, texts, now=now)
    await session.commit()

    topics = {
        t.id: t
        for t in (
            await session.execute(select(SocialTopic).where(SocialTopic.last_seen_at >= since))
        ).scalars()
    }
    by_topic = await load_topic_posts(session, list(topics), since=since)
    coverage = await load_coverage(session, now=now)
    week_start = analytics.windows(now)[0][0]
    eligible = []
    broad: list[str] = []
    for topic_id, topic_posts in by_topic.items():
        this_week = sum(
            1 for p in topic_posts if p.published_at and p.published_at >= week_start
        )
        if this_week < settings.social_min_posts:
            continue
        if too_broad(topics[topic_id].subject, topics[topic_id].outcome):
            broad.append(topics[topic_id].label)
            continue
        metrics = analytics.compute(topic_posts, coverage, now=now)
        eligible.append((topics[topic_id], metrics, analytics.momentum(metrics)))

    held = await _suppressed(
        session,
        {topic.id: metrics.independent_creators for topic, metrics, _ in eligible},
        now=now,
        cooloff_days=settings.social_dismiss_cooloff_days,
    )
    for topic_id, reason in held.items():
        report.notes.append(f"held back {topics[topic_id].label}: {reason}")
    # Twice the slots: repeats are dropped below, and the next-best must fill their places.
    ranked = sorted(
        (e for e in eligible if e[0].id not in held),
        key=lambda e: (
            MOMENTUM_ORDER.index(e[2].status),
            -e[1].independent_creators,
            -(e[1].view_velocity or 0.0),
        ),
    )[: settings.social_max_candidates * 2]
    report.sources["questions"] = await write_questions(
        session, question_call, [topic for topic, _, _ in ranked]
    )
    kept, duplicates = without_repeated_questions(
        [row[0] for row in ranked], decided=await _decided_topics(session, held)
    )
    for repeat, first in duplicates:
        report.notes.append(f"same question as {first.label}, not proposed: {repeat.label}")
    by_id = {row[0].id: row for row in ranked}
    assessed = []
    for position, topic in enumerate(kept[: settings.social_max_candidates], start=1):
        checked = (
            await assess_evidence(evidence, topic.question or topic.label, now=now)
            if position <= settings.social_evidence_top
            else EvidenceAssessment(status="not_checked", reason="outside the weekly top")
        )
        assessed.append((*by_id[topic.id], checked))
    # A search that found nothing goes last, and the desk marks it. Stable, so the
    # rest keep their order; a count nobody could make is unknown, not zero, and stays.
    assessed.sort(key=lambda row: found_no_papers(row[3]))
    for rank, (topic, metrics, assessment, checked) in enumerate(assessed, start=1):
        session.add(
            SocialCandidate(
                collection_run_id=run_id,
                topic_id=topic.id,
                rank=rank,
                metrics=metrics.model_dump(mode="json"),
                momentum=assessment.model_dump(mode="json"),
                evidence=checked.model_dump(mode="json"),
                status="proposed",
            )
        )
    await session.commit()
    report.stats.update(
        posts=len(posts),
        read=len(result.readings),
        reading_backlog=result.backlog,
        topics_measured=len(by_topic),
        eligible=len(eligible),
        too_broad=len(broad),
        suppressed=len(held),
        candidates=len(assessed),
        repeated_questions=len(duplicates),
        product_mentions=len(mentions),
    )
    if broad:
        report.notes.append(f"too broad to propose: {', '.join(sorted(broad)[:20])}")
    if result.backlog:
        report.notes.append(f"{result.backlog} posts wait for the next run's reading")
    if not assessed:
        report.notes.append(
            f"no topic had {settings.social_min_posts}+ posts this week after suppression"
        )


async def _decided_topics(session: AsyncSession, held: Iterable[str]) -> list[SocialTopic]:
    """Topics whose question must not come back under another topic: every promoted
    one whose draft did not fail, posts this week or not, and those held back this run."""
    promoted = select(SocialCandidate.topic_id).where(
        SocialCandidate.status == "promoted", ~_draft_failed
    )
    rows = await session.execute(
        select(SocialTopic).where(
            or_(SocialTopic.id.in_(promoted), SocialTopic.id.in_(list(held)))
        )
    )
    return list(rows.scalars())


def without_repeated_questions(
    topics: list[SocialTopic], *, decided: Iterable[SocialTopic] = ()
) -> tuple[list[SocialTopic], list[tuple[SocialTopic, SocialTopic]]]:
    """Best-ranked first: the topics to propose, and each repeat with the topic it repeats.

    ``decided`` is compared too. Suppression works per topic, so without it a topic
    asking what a promoted one asked is proposed as soon as its twin is held back.
    """
    decided = list(decided)
    kept: list[SocialTopic] = []
    repeats: list[tuple[SocialTopic, SocialTopic]] = []
    for topic in topics:
        first = next(
            (
                other
                for other in (*decided, *kept)
                if topic.question
                and other.question
                and same_question(topic.question, other.question)
            ),
            None,
        )
        if first is None:
            kept.append(topic)
        else:
            repeats.append((topic, first))
    return kept, repeats


def found_no_papers(evidence: EvidenceAssessment) -> bool:
    """The search ran and found nothing. Not the same as a search that could not run."""
    return evidence.counts is not None and evidence.counts.get("total") == 0


@dataclass(frozen=True, slots=True)
class LiteratureCheck:
    """What counts a proposed question: the article pipeline's extraction and query."""

    extraction: ExtractionClient
    #: Asked in order; the next answers when one is down.
    sources: tuple[TermsEvidence, ...]
    strategy: QueryStrategy = field(default_factory=TemplateQueryStrategy)


async def _count(sources: tuple[TermsEvidence, ...], terms: str, since: str) -> EvidenceCounts:
    failure: ProviderUnavailable | None = None
    for source in sources:
        try:
            return await source.profile_terms(terms, since)
        except ProviderUnavailable as exc:
            failure = exc
    assert failure is not None  # sources is never empty
    raise failure


async def assess_evidence(
    check: LiteratureCheck | None, asked: str, *, now: datetime
) -> EvidenceAssessment:
    """How much literature the search for this question's article would find.

    The same calls as the research agent's ``_deep``. Counting subject + outcome
    instead answers something broader than the question ("smart ring" + "sleep"
    for "can smart rings detect sleep apnea?"). A count that could not be made is
    ``not_checked`` with the reason, never "no literature".
    """
    if check is None:
        return EvidenceAssessment(status="not_checked", reason="no model to extract claims")
    since = (now - timedelta(days=EVIDENCE_RECENT_DAYS)).date().isoformat()
    try:
        extracted = (await check.extraction.extract(ExtractionInput(topic=asked))).output
        built = [
            check.strategy.build(claim, extracted.product, extracted.ingredients)
            for claim in extracted.target_claims[:DEEP_CLAIMS]
        ]
        # The general pass; the review pass shares its terms.
        counts = [await _count(check.sources, queries[-1].terms, since) for queries in built]
    except UnanchoredQuery:
        return EvidenceAssessment(
            status="not_checked",
            reason="the article pipeline could not build a search for this question",
        )
    except Exception as exc:  # a model or literature outage costs the assessment, not the run
        return EvidenceAssessment(status="not_checked", reason=f"{type(exc).__name__}: {exc}")
    if not counts:
        return EvidenceAssessment(status="not_checked", reason="extraction named no claims")
    best = max(counts, key=lambda c: (evidence_score(c) or 0.0, c.total))
    status = evidence_status(best)
    return EvidenceAssessment(
        status=status.value if status else "none",
        source=best.source,
        counts={
            "total": best.total,
            "recent": best.recent,
            "reviews_or_meta": best.reviews_or_meta,
            "rcts": best.rcts,
        },
        query=best.query,
    )


async def _suppressed(
    session: AsyncSession, creators_now: dict[str, int], *, now: datetime, cooloff_days: int
) -> dict[str, str]:
    if not creators_now:
        return {}
    rows = (
        await session.execute(
            select(SocialCandidate)
            .where(
                SocialCandidate.topic_id.in_(creators_now),
                SocialCandidate.status.in_(("promoted", "dismissed")),
                ~_draft_failed,
            )
            .order_by(SocialCandidate.decided_at.desc())
        )
    ).scalars()
    held: dict[str, str] = {}
    seen: set[str] = set()
    for row in rows:
        if row.topic_id in seen or row.decided_at is None:
            continue
        seen.add(row.topic_id)
        reason = suppression_reason(
            row.status,
            row.decided_at,
            creators_then=int(row.metrics.get("independent_creators") or 0),
            creators_now=creators_now[row.topic_id],
            now=now,
            cooloff_days=cooloff_days,
        )
        if reason:
            held[row.topic_id] = reason
    return held
