"""The human-in-the-loop service. Invariant #1 lives here.

``approve()`` is the **only** function in the system that sets
``status = 'published'``. Nothing in the pipeline can reach it; there is no
admin endpoint that bypasses it; the DB CHECK constraint refuses a published
row without ``reviewed_by`` and ``reviewed_at``. If you are reviewing this
design for the human-in-the-loop guarantee, this file and that constraint are
the two things to check.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ArticleStatus, StudyType, Subject
from app.domain.models import Article, ArticleSource, Source
from app.evidence.grading import is_weak_evidence
from app.services.card import derive_card
from app.services.media import UnsafeMediaError, assert_media_is_ours
from app.services.tiptap import cited_handles_in_doc

logger = logging.getLogger(__name__)


class ReviewError(RuntimeError):
    """An illegal review transition was attempted. Surfaces as HTTP 409."""


class EditConflict(ReviewError):
    """The draft changed since the editor loaded it. Surfaces as HTTP 412."""


#: ``base`` not sent: the save is unconditional, as before the check existed.
_UNCHECKED: Any = object()


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_edits(
        self, article_id: str, content: dict, reviewer_id: str, *, base: Any = _UNCHECKED
    ) -> None:
        """Autosave the editor's content into ``edited_content``.

        Writes ``edited_content`` only. ``original_content`` is never touched —
        the DB trigger will reject the statement if a bug tries, so the audit
        trail of "what the model wrote vs. what went live" holds even against
        application error.

        Called on a debounce from the editor, so it must be cheap and
        idempotent. **Citation validation does not run here** — failing a
        reviewer's autosave mid-sentence, while a citation is momentarily
        orphaned, would be hostile. That check happens at approve time, where
        it can block publication rather than typing.

        Media is the exception, and the difference is that there is no
        half-typed state to be caught in: an image or video block is inserted
        whole or not at all, so a bad one can only come from a paste of foreign
        HTML or from a bug. Refusing it here costs a reviewer nothing and is
        the difference between a broken `src` being noticed now and it being
        noticed by a reader.

        ``base`` is the ``edited_content`` the editor last loaded or saved (None
        for an unedited draft). The whole document is saved, so without it a
        second reviewer's autosave from an older copy silently undid the first
        reviewer's edits. Compared as dicts, so JSONB's key order is irrelevant.

        Raises:
            EditConflict: ``edited_content`` is no longer ``base``.
            ReviewError: the article is not in ``pending_review``, or the
                document references media this system did not store.
        """
        article = await self._get(article_id, lock=True)
        if article.status != ArticleStatus.PENDING_REVIEW:
            raise ReviewError(
                f"cannot edit an article in state '{article.status}'; "
                "only drafts pending review are editable"
            )
        if base is not _UNCHECKED and base != article.edited_content:
            raise EditConflict(
                "another reviewer saved this draft since you opened it; "
                "copy your change and reload"
            )

        try:
            assert_media_is_ours(content)
        except UnsafeMediaError as exc:
            raise ReviewError(str(exc)) from exc

        article.edited_content = content
        await self._session.flush()
        logger.debug("autosaved edits to article %s by reviewer %s", article_id, reviewer_id)

    async def approve(self, article_id: str, reviewer_id: str) -> None:
        """Publish an article. The only path to ``published``.

        Re-validates the *edited* content's citations before publishing. A
        reviewer can orphan a citation by deleting a sentence, or paste a handle
        that was never provided — invariant #2 has to survive human editing, not
        just generation.

        Media is re-checked here too. ``save_edits`` already refuses a foreign
        image or video, so this is the second lock on the same door: publishing
        is the moment content stops being ours and starts being every reader's,
        and it is the wrong moment to be relying on an earlier check having run.

        Raises:
            ReviewError: wrong status, or edited content that no longer
                validates. The message names the specific handles, because a
                reviewer needs to know *which* citation broke.
        """
        article = await self._get(article_id, lock=True)

        if article.status != ArticleStatus.PENDING_REVIEW:
            # Deliberately includes validation_failed: a draft that failed
            # validation cannot be approved into existence, it has to be
            # regenerated.
            raise ReviewError(
                f"cannot publish an article in state '{article.status}'; "
                "only drafts pending review can be published"
            )

        content = article.edited_content or article.original_content
        await self._assert_citations_still_resolve(article_id, content)
        try:
            assert_media_is_ours(content)
        except UnsafeMediaError as exc:
            raise ReviewError(str(exc)) from exc

        card = derive_card(
            article.headline,
            content,
            article.verdict,
            generated=article.generated_imagery,
        )
        now = datetime.now(UTC)

        article.card_headline = card.headline
        article.card_excerpt = card.excerpt
        article.card_verdict = card.verdict
        # Derived here rather than uploaded separately, so the feed tile cannot
        # show a picture that is not in the article the reader lands on.
        #
        # The pipeline's generated portrait cover is the one thing that is not
        # literally in the document, and `derive_card` only reaches for it while
        # the document's first image is still the landscape frame drawn beside
        # it — so the tile is a reframing of the article's own picture, never an
        # independent one. That predicate is also where the cover gets the
        # MEDIA_SRC_RE check `assert_media_is_ours` above cannot give it, since
        # that walks the document and the cover is not in it.
        article.card_image = card.image
        article.card_image_alt = card.image_alt
        article.status = ArticleStatus.PUBLISHED
        article.reviewed_by = reviewer_id
        article.reviewed_at = now
        article.published_at = now

        await self._session.flush()
        # After the flush, because the function reads the row as it now stands:
        # published, with the card columns just derived. Built in SQL by the
        # same function migration 0006 backfilled with, so a newly published
        # article is indexed exactly as the older ones were.
        await self._session.execute(_INDEX_FOR_SEARCH, {"article_id": article_id})
        logger.info("article %s published by reviewer %s", article_id, reviewer_id)

    async def set_subject(
        self, article_id: str, subject: Subject | None, reviewer_id: str
    ) -> None:
        """Classify what kind of thing this article assesses.

        Editable after publication, unlike the body: the subject is metadata a
        reviewer may get wrong or refine later, and correcting it changes a
        colour and a browse listing rather than a word the reader was shown.
        Set it to ``None`` to un-classify — an unclassified article renders in
        ink, which is the design's resting state, so blank is a real answer
        rather than a missing one.

        Deliberately not derived from ``product``: that is free text, and a
        guessed subject would put a confident colour on an unchecked
        classification.
        """
        article = await self._get(article_id, lock=True)
        if article.status in (ArticleStatus.REJECTED, ArticleStatus.DRAFT_FAILED):
            raise ReviewError(
                f"cannot classify an article in state '{article.status}'"
            )
        article.subject = subject
        await self._session.flush()
        logger.info(
            "article %s classified as %s by reviewer %s",
            article_id,
            subject or "unclassified",
            reviewer_id,
        )

    async def card_preview(self, article_id: str) -> dict[str, Any]:
        """What ``approve()`` would materialise onto the card columns, unwritten.

        The **same** ``derive_card`` the publish path runs — not a second
        implementation of the same rules, and pointedly not a TypeScript port
        of them in the console. A preview computed any other way is a second
        thing to keep in step with the first, and the day the two disagree is
        the day the preview stops being worth looking at.

        Deliberately its own method rather than four more fields on
        ``article_detail``: this has to reflect an edit made ten seconds ago,
        while the detail response is fetched once and cached for the length of
        a review session. Merging them would mean invalidating the whole draft
        on every autosave to keep one derived string fresh.
        """
        article = await self._get(article_id)
        content = article.edited_content or article.original_content
        card = derive_card(
            article.headline,
            content,
            article.verdict,
            generated=article.generated_imagery,
        )
        return {
            "slug": article.slug,
            "headline": card.headline,
            "excerpt": card.excerpt,
            "verdict": card.verdict,
            "verdict_qualifier": article.verdict_qualifier,
            "image": card.image,
            "image_alt": card.image_alt,
            "subject": article.subject,
            # Publishing is what sets this. Before then, the honest answer to
            # "when was this published" is "it would be now" — `created_at`
            # would read as a publication date this article does not have. The
            # tile does not render it either way; the field is here because the
            # preview renders through the public feed's own component and that
            # component's contract has it.
            "published_at": article.published_at or datetime.now(UTC),
            "image_is_generated_cover": card.image_is_generated_cover,
        }

    async def set_illustration(
        self, article_id: str, illustration: dict[str, Any], reviewer_id: str
    ) -> None:
        """Record a freshly generated pair of frames on the article.

        Only from ``pending_review``, the same gate ``save_edits`` uses and for
        the same reason: a published article's picture is something a reader
        has already been shown, and changing it is a publish decision rather
        than an edit.

        **This does not touch the document.** ``original_content`` is
        trigger-protected and ``edited_content`` is the reviewer's live editor
        buffer — writing either from here would either fail or clobber whatever
        they are typing. The console swaps the image node itself and lets
        autosave persist it, which also means the new ``src`` goes through
        ``assert_media_is_ours`` on the way in like any other edit.

        The consequence is a window where this column names a lead the document
        does not contain yet. That is safe by construction: the pairing rule in
        ``derive_card`` fails closed, so the worst outcome of a reviewer
        navigating away mid-swap is a typographic tile — never a tile showing a
        picture the article does not have.
        """
        article = await self._get(article_id, lock=True)
        if article.status != ArticleStatus.PENDING_REVIEW:
            raise ReviewError(
                f"cannot regenerate imagery for an article in state '{article.status}'; "
                "only drafts pending review can be changed"
            )
        article.generated_imagery = illustration
        await self._session.flush()
        logger.info(
            "article %s illustration regenerated by reviewer %s", article_id, reviewer_id
        )

    async def article_for_illustration(self, article_id: str) -> Article:
        """The row the regenerate route needs before it spends money.

        Separate from ``set_illustration`` because the generation happens
        between them and takes seconds: holding the article across a provider
        call would keep a transaction open for the length of it.
        """
        return await self._get(article_id)

    #: States a draft can be rejected from.
    #:
    #: ``validation_failed`` is included because rejection is the only way a
    #: draft leaves the queue — there is no delete — and without it those
    #: drafts have no available action at all and accumulate in their tab
    #: forever. They are still never approvable; this is the discard path, not
    #: a route around invariant #2.
    _REJECTABLE = frozenset(
        {ArticleStatus.PENDING_REVIEW, ArticleStatus.VALIDATION_FAILED}
    )

    async def reject(self, article_id: str, reviewer_id: str, reason: str) -> None:
        """Reject a draft. Terminal, with a required reason.

        There is no delete: rejection is a recorded state. The reasons drafts
        get rejected are the highest-value signal available for improving the
        synthesis prompt, and deleting them throws that away. That argument
        applies with most force to ``validation_failed`` drafts, which is why
        discarding one records a reason rather than removing the row.
        """
        article = await self._get(article_id, lock=True)
        if article.status not in self._REJECTABLE:
            raise ReviewError(
                f"cannot reject an article in state '{article.status}'"
            )

        article.status = ArticleStatus.REJECTED
        article.rejection_reason = reason
        article.reviewed_by = reviewer_id
        article.reviewed_at = datetime.now(UTC)
        await self._session.flush()
        logger.info("article %s rejected by reviewer %s", article_id, reviewer_id)

    async def _assert_citations_still_resolve(self, article_id: str, content: dict) -> None:
        """Every handle in the edited body must still map to a linked source."""
        emitted = cited_handles_in_doc(content)
        if not emitted:
            return

        result = await self._session.execute(
            select(ArticleSource.citation_handle).where(
                ArticleSource.article_id == article_id
            )
        )
        known = {row[0] for row in result.all()}

        unknown = sorted(emitted - known)
        if unknown:
            raise ReviewError(
                f"cannot publish: {', '.join(unknown)} "
                f"{'is' if len(unknown) == 1 else 'are'} not among this article's "
                "sources. Remove the citation or restore the original text."
            )

    async def queue(
        self,
        status: ArticleStatus = ArticleStatus.PENDING_REVIEW,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """The review queue, newest first in every tab.

        Pending review was oldest-first for a while, on the argument that
        newest-first lets a slow queue strand old drafts behind fresher ones.
        In practice the reviewer is watching the draft they just generated, and
        it landed at the bottom of the list. The staleness argument is real but
        it is a question about *one* row's age, which each row already answers
        itself ("queued 3 days ago"); burying the row the reviewer is waiting
        for is a cost paid on every visit.

        Also serves the ``validation_failed`` tab — those drafts are never
        approvable, but they are the system's prompt-bug feedback and must be
        visible.
        """
        # Keyset pagination: the tiebreaker must sort the same direction as the
        # key, or the ``<`` below skips rows sharing a ``created_at``.
        statement = (
            select(Article)
            .where(Article.status == status)
            .order_by(Article.created_at.desc(), Article.id.desc())
        )

        if cursor:
            created_at, article_id = _decode_cursor(cursor)
            keyset = tuple_(Article.created_at, Article.id)
            statement = statement.where(keyset < (created_at, article_id))

        result = await self._session.execute(statement.limit(limit + 1))
        articles = list(result.scalars())

        next_cursor = None
        if len(articles) > limit:
            articles = articles[:limit]
            last = articles[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        items = [
            {
                "id": article.id,
                "topic": article.topic,
                "headline": article.headline,
                "verdict": article.verdict,
                "status": article.status,
                "evidence_grade": article.evidence_grade,
                "validation_badge": _badge(article.validation_report),
                "has_weak_evidence": is_weak_evidence(StudyType(article.evidence_grade)),
                "subject": article.subject,
                "created_at": article.created_at,
            }
            for article in articles
        ]
        return items, next_cursor

    async def article_detail(self, article_id: str) -> dict[str, Any]:
        """Draft + sources + validation report for the review screen.

        One query joining ``article_sources`` → ``sources``, ordered by claim
        then handle, so the panel renders without client-side joining. This is
        the "fast source access" requirement: the reviewer's time is the scarce
        resource, and a waterfall of requests spends it.

        Returns cited **and** uncited sources. Uncited ones are what the model
        chose to leave out, and omission is the failure mode human review exists
        to catch.
        """
        article = await self._get(article_id)

        result = await self._session.execute(
            select(ArticleSource, Source)
            .join(Source, Source.id == ArticleSource.source_id)
            .where(ArticleSource.article_id == article_id)
            .order_by(ArticleSource.claim, ArticleSource.citation_handle)
        )

        sources = [
            {
                "source_id": source.id,
                "citation_handle": link.citation_handle,
                "claim": link.claim,
                "title": source.title,
                "journal": source.journal,
                "year": source.year,
                "study_type": source.study_type,
                "citation_count": source.citation_count,
                "url": source.resolved_url,
                "pmid": source.pmid,
                "doi": source.doi,
                "was_cited": link.was_cited,
                "relevance_score": link.relevance_score,
                "is_weak_evidence": is_weak_evidence(StudyType(source.study_type)),
                # Distinct from weak evidence, and shown separately. Weak means
                # a poor basis for confidence; retracted means not a basis.
                "retracted": source.retracted_at is not None,
                "concern": source.concern_at is not None,
                "retraction_note": source.retraction_note,
                "excerpts": link.excerpts or [],
                "stance": link.stance,
            }
            for link, source in result.all()
        ]

        return {
            "id": article.id,
            # Carried so the console can link a just-published draft straight
            # to where it landed on the public feed. Present before publication
            # too — the slug is assigned at persist time — but only a published
            # article resolves at /a/{slug}, which is why the console gates the
            # link on status rather than on the field being set.
            "slug": article.slug,
            "subject": article.subject,
            "status": article.status,
            "topic": article.topic,
            "product": article.product,
            "target_claims": article.target_claims,
            "ingredients": article.ingredients,
            "headline": article.headline,
            "summary": article.summary,
            "verdict": article.verdict,
            "verdict_qualifier": article.verdict_qualifier,
            "evidence_grade": article.evidence_grade,
            "original_content": article.original_content,
            "edited_content": article.edited_content,
            "validation_report": article.validation_report,
            "sources": sources,
            "pipeline_run_id": article.pipeline_run_id,
            "retraction_flagged_at": article.retraction_flagged_at,
            "retraction_detail": article.retraction_detail,
            "created_at": article.created_at,
        }

    async def _get(self, article_id: str, *, lock: bool = False) -> Article:
        """``lock`` for writes that check the status first: ``FOR UPDATE`` makes a
        racing approve, autosave or reject wait and re-read rather than act on
        a status that has already changed. Off for reads, and deliberately for
        ``article_for_illustration``, which must not hold the row across a
        provider call. ``populate_existing`` because the lock alone re-queries
        but keeps a copy this session already holds, stale status included."""
        article = await self._session.get(
            Article, article_id, with_for_update=lock, populate_existing=lock
        )
        if article is None:
            raise ReviewError(f"article {article_id} not found")
        return article


_INDEX_FOR_SEARCH = text(
    "UPDATE articles a SET search_document = article_search_document(a) "
    "WHERE a.id = :article_id"
)


def _badge(report: dict[str, Any] | None) -> str:
    if not report:
        return "not validated"
    resolved = report.get("citations_resolved", 0)
    total = report.get("citations_total", 0)
    return f"{resolved}/{total} citations resolve"


def _encode_cursor(created_at: datetime, article_id: str) -> str:
    """Opaque keyset cursor.

    Keyset rather than offset: the queue mutates while a reviewer works through
    it, and an offset would skip or repeat drafts as rows leave the filtered
    set. The id is the tiebreaker for rows sharing a timestamp.
    """
    raw = f"{created_at.isoformat()}|{article_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, article_id = raw.split("|", 1)
        uuid.UUID(article_id)  # a non-UUID would reach Postgres and 500
        return datetime.fromisoformat(timestamp), article_id
    except ValueError as exc:  # binascii and Unicode errors are ValueErrors too
        raise ReviewError("malformed cursor") from exc
