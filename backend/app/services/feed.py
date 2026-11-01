"""Public feed queries.

**Every query in this module filters ``status = 'published'``.** The filter
lives in ``_published_only()`` rather than being spelled out per method, so
there is one place to audit rather than several — and no way to add a new
method that forgets it.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import (
    Select,
    Text,
    and_,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ArticleStatus, Subject, Verdict
from app.domain.models import Article, ArticleSource, Source


class ArticleNotFound(LookupError):
    """No published article with that slug. Surfaces as HTTP 404."""


def _published_only(statement: Select[Any]) -> Select[Any]:
    """The single audit point for the public surface's visibility rule."""
    return statement.where(Article.status == ArticleStatus.PUBLISHED)


#: What a public search answered with. ``text``: full-text matches.
#: ``similar``: nothing matched, and these are titles spelled like the query.
SearchMatch = Literal["text", "similar"]

#: Deepest a search can be paged. Search is offset-paged, so this bounds the
#: work one request can ask for; nobody reads past the tenth page of results.
MAX_SEARCH_RESULTS = 200

#: The card columns, shared by the feed and search so the two cannot render a
#: tile differently. ``Article.id`` is not part of the card; it is the keyset
#: tiebreak.
_CARD_COLUMNS = (
    Article.slug,
    Article.card_headline,
    Article.card_excerpt,
    Article.card_verdict,
    Article.verdict_qualifier,
    Article.card_image,
    Article.card_image_alt,
    Article.subject,
    Article.published_at,
    Article.id,
)

#: Must match ``articles_title_trgm_idx`` in migration 0006 expression for
#: expression, or the planner cannot use the index. The space is a literal, not
#: a bound parameter, for the same reason.
_TYPO_TITLE = Article.card_headline.op("||")(literal_column("' '")).op("||")(
    Article.product
)


def _full_text(query: str) -> tuple[Any, Any]:
    """Predicate and rank for full text. ``websearch_to_tsquery`` never raises."""
    tsquery = func.websearch_to_tsquery(
        literal_column("'english'::regconfig"), literal(query, Text)
    )
    return (
        Article.search_document.op("@@")(tsquery),
        func.ts_rank_cd(Article.search_document, tsquery),
    )


def _similar_title(query: str) -> tuple[Any, Any]:
    """Predicate and score for the typo fallback.

    ``<%`` is word similarity at ``pg_trgm.word_similarity_threshold`` (0.6
    by default) — whether the query resembles *some run of words* in the
    title, which is the question a misspelt search asks. Whole-string
    similarity (``%``) compares a one-word query against a whole headline and
    scored a plain typo at 0.21; migration 0006 has the measurement.
    """
    bound = literal(query, Text)
    return bound.op("<%")(_TYPO_TITLE), func.word_similarity(bound, _TYPO_TITLE)


def _may_be_misspelt(query: str) -> bool:
    """False when the query uses ``websearch_to_tsquery`` syntax."""
    words = query.lower().split()
    return '"' not in query and "or" not in words and not any(
        word.startswith("-") for word in words
    )


def _card(row: Any) -> dict[str, Any]:
    return {
        "slug": row.slug,
        # Fall back to the article headline if a legacy row predates
        # card materialisation; a card with an empty title is worse
        # than one whose excerpt is missing.
        "headline": row.card_headline or "",
        "excerpt": row.card_excerpt or "",
        "verdict": row.card_verdict,
        "verdict_qualifier": row.verdict_qualifier,
        "image": row.card_image,
        "image_alt": row.card_image_alt,
        "subject": row.subject,
        "published_at": row.published_at,
    }


class FeedService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(
        self,
        cursor: str | None = None,
        limit: int = 24,
        verdict: Verdict | None = None,
        subject: Subject | None = None,
        interests: Sequence[Subject] = (),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of published cards, newest first.

        Reads the materialised card columns only — the feed never touches
        article content, which is what keeps it a single index scan
        (``articles_feed_idx``) at any feed size.

        ``interests`` lifts a signed-in reader's subjects to the top **without
        hiding anything**: the sort gains a leading tier — 0 for a subject they
        picked, 1 for everything else — and the rest of the feed follows below
        it. Filtering instead would make an account quietly narrow the world,
        which is the thing a feed of health claims should least do; it also
        means an unclassified article (``subject IS NULL``) is still reachable
        by scrolling rather than invisible to anyone with an interest set.

        The consequence to know: the tiered sort cannot use
        ``articles_feed_idx``, because the leading key is a CASE expression.
        That is affordable while the feed is small and is the reason the tier
        is skipped entirely — same statement as before — for a reader with no
        interests, which is every anonymous request. If personalised paging
        ever becomes hot, the fix is a materialised tier column, not a
        different sort.
        """
        # Constant-folded away when nobody has picked a subject, so the
        # anonymous feed keeps exactly the query (and the index scan) it had.
        tier = (
            case((Article.subject.in_(list(interests)), 0), else_=1)
            if interests
            else None
        )

        statement = _published_only(select(*_CARD_COLUMNS))

        if tier is not None:
            statement = statement.add_columns(tier.label("tier"))
            statement = statement.order_by(
                tier.asc(), Article.published_at.desc(), Article.id.desc()
            )
        else:
            statement = statement.order_by(
                Article.published_at.desc(), Article.id.desc()
            )

        if verdict is not None:
            statement = statement.where(Article.card_verdict == verdict)
        if subject is not None:
            statement = statement.where(Article.subject == subject)

        if cursor:
            cursor_tier, published_at, article_id = _decode_cursor(cursor)
            within_page = tuple_(Article.published_at, Article.id) < (
                published_at,
                article_id,
            )
            if tier is None:
                statement = statement.where(within_page)
            else:
                # Written as an OR rather than a row comparison because the two
                # keys sort in opposite directions — tier ascending, date
                # descending — and `(a, b) < (x, y)` can only express one.
                statement = statement.where(
                    or_(
                        tier > cursor_tier,
                        and_(tier == cursor_tier, within_page),
                    )
                )

        result = await self._session.execute(statement.limit(limit + 1))
        rows = result.all()

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(
                getattr(last, "tier", 0), last.published_at, last.id
            )

        return [_card(row) for row in rows], next_cursor

    async def search(
        self,
        query: str,
        cursor: str | None = None,
        limit: int = 24,
        verdict: Verdict | None = None,
        subject: Subject | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, SearchMatch]:
        """Published cards matching ``query``, best match first.

        Full text over ``search_document`` first (migration 0006 says what is
        in it and why). Only when that finds **nothing** does it fall back to
        trigram word similarity over headline and product, which is what makes
        "magnesum" find magnesium. The third return value says which one
        answered, because the fallback can be wrong ("stres" matches
        "strength") and the page has to be able to say "close matches" rather
        than present them as results.

        A query using search syntax — quotes, ``-word``, ``or`` — gets no
        fallback. The reader said exactly what they wanted, and trigram
        similarity cannot honour it: measured, ``creatine -muscle`` excluded
        the creatine article and the fallback then returned it.

        The mode is decided on the first page and carried in the cursor, so a
        reader paging through typo matches is not switched to full text when a
        later page happens to be empty, or the reverse.

        Interests play no part here. Relevance is the order a search promises;
        lifting a weaker match above a stronger one because of its subject
        would break that promise, and the "order, never narrow" rule is about
        the feed, which a search is not.

        Offset-paged, unlike the feed, because the sort key is a float rank
        that a keyset cursor would have to round-trip exactly — and capped at
        ``MAX_SEARCH_RESULTS``, so a reader cannot page an offset deep enough
        to make the scan expensive. The feed's reason for keyset paging (the
        head moves while you scroll) applies far less to a ranked list.
        """
        if cursor:
            match, offset = _decode_search_cursor(cursor)
        else:
            match, offset = "text", 0

        async def rows_for(mode: SearchMatch) -> list[Any]:
            clauses = _full_text(query) if mode == "text" else _similar_title(query)
            return await self._search_rows(clauses, verdict, subject, offset, limit)

        rows = await rows_for(match)
        if not rows and match == "text" and offset == 0 and _may_be_misspelt(query):
            match = "similar"
            rows = await rows_for(match)

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            if offset + limit < MAX_SEARCH_RESULTS:
                next_cursor = _encode_search_cursor(match, offset + limit)
        return [_card(row) for row in rows], next_cursor, match

    async def _search_rows(
        self,
        clauses: tuple[Any, Any],
        verdict: Verdict | None,
        subject: Subject | None,
        offset: int,
        limit: int,
    ) -> list[Any]:
        predicate, score = clauses
        statement = (
            _published_only(select(*_CARD_COLUMNS))
            .where(predicate)
            .order_by(score.desc(), Article.published_at.desc(), Article.id.desc())
        )
        if verdict is not None:
            statement = statement.where(Article.card_verdict == verdict)
        if subject is not None:
            statement = statement.where(Article.subject == subject)
        result = await self._session.execute(statement.offset(offset).limit(limit + 1))
        return list(result.all())

    async def facets(self) -> dict[str, Any]:
        """How many published articles sit under each subject and each verdict.

        Two grouped counts in one round trip, for the browse drawer. The counts
        exist because a category row with no number is a door a reader has to
        open to find out it is empty — and this feed fills slowly by design, so
        empty categories are the normal case rather than an edge one.

        Counted over the whole published set, not over the loaded page: a
        client-side tally of what has been fetched so far would climb as the
        reader scrolls, which reads as the numbers being wrong.

        Unclassified articles are counted in ``total`` and in no subject. That
        is deliberate — the subject counts must not sum to the total, because
        "everything" genuinely holds more than the five categories do.
        """
        subject_rows = await self._session.execute(
            _published_only(select(Article.subject, func.count()))
            .where(Article.subject.is_not(None))
            .group_by(Article.subject)
        )
        verdict_rows = await self._session.execute(
            _published_only(select(Article.card_verdict, func.count()))
            .where(Article.card_verdict.is_not(None))
            .group_by(Article.card_verdict)
        )
        total = await self._session.execute(
            _published_only(select(func.count()).select_from(Article))
        )

        return {
            "total": total.scalar_one(),
            "subjects": {str(subject): count for subject, count in subject_rows.all()},
            "verdicts": {str(verdict): count for verdict, count in verdict_rows.all()},
        }

    async def article(self, slug: str) -> dict[str, Any]:
        """Full article with citations and resolved sources.

        Returns ``edited_content`` when present — the human-approved version.

        Raises:
            ArticleNotFound: no *published* article with this slug. The caller
                must not distinguish "no such article" from "not published
                yet": leaking the existence of unpublished drafts turns the
                slug space into a preview channel.
        """
        result = await self._session.execute(
            _published_only(select(Article).where(Article.slug == slug))
        )
        article = result.scalar_one_or_none()
        if article is None:
            raise ArticleNotFound(slug)

        source_result = await self._session.execute(
            select(ArticleSource, Source)
            .join(Source, Source.id == ArticleSource.source_id)
            .where(
                ArticleSource.article_id == article.id,
                # Public readers see what the article cites. Retrieved-but-
                # uncited sources are internal review context.
                ArticleSource.was_cited.is_(True),
            )
            .order_by(ArticleSource.citation_handle)
        )

        seen: set[str] = set()
        sources: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        claims: dict[str, list[str]] = {}

        for link, source in source_result.all():
            if link.citation_handle not in seen:
                seen.add(link.citation_handle)
                sources.append(
                    {
                        "title": source.title,
                        "journal": source.journal,
                        "year": source.year,
                        "study_type": source.study_type,
                        "citation_handle": link.citation_handle,
                        "url": source.resolved_url,
                        "pmid": source.pmid,
                        "doi": source.doi,
                        # Marked on the source, not only in the article banner:
                        # a reader who scrolls to the citations should not have
                        # to guess which one the notice is about.
                        "retracted": source.retracted_at is not None,
                    }
                )
            claims.setdefault(link.claim, []).append(link.citation_handle)

        citations = [
            {"claim": claim, "handles": sorted(set(handles), key=_handle_sort_key)}
            for claim, handles in claims.items()
        ]

        return {
            "slug": article.slug,
            "headline": article.headline,
            "summary": article.summary,
            "subject": article.subject,
            "verdict": article.verdict,
            "verdict_qualifier": article.verdict_qualifier,
            "product": article.product,
            "target_claims": article.target_claims,
            "ingredients": article.ingredients,
            "content": article.display_content,
            "sources": sorted(sources, key=lambda s: _handle_sort_key(s["citation_handle"])),
            "citations": citations,
            "evidence_grade": article.evidence_grade,
            "published_at": article.published_at,
            # Derived from the flag rather than recomputed from the sources
            # above, so the banner and the console agree on when it was raised.
            # The article stays readable — a retracted source does not
            # automatically invalidate a conclusion — but the reader is told
            # before they read it rather than after.
            "retraction_notice": article.retraction_flagged_at is not None,
        }


def _handle_sort_key(handle: str) -> int:
    digits = handle[1:]
    return int(digits) if digits.isdigit() else 0


def _encode_cursor(tier: int, published_at: datetime, article_id: str) -> str:
    """Keyset cursor: the full sort key, tier first.

    Keyset rather than offset because the feed grows at the head: an offset
    would skip or duplicate cards whenever something publishes mid-scroll.

    The tier is always encoded, even on the anonymous feed where it is always
    zero, so a reader who signs in or out mid-scroll hands back a cursor the
    other mode can still read.
    """
    return base64.urlsafe_b64encode(
        f"{tier}|{published_at.isoformat()}|{article_id}".encode()
    ).decode()


def _encode_search_cursor(match: SearchMatch, offset: int) -> str:
    return base64.urlsafe_b64encode(f"search|{match}|{offset}".encode()).decode()


def _decode_search_cursor(cursor: str) -> tuple[SearchMatch, int]:
    """Refuses a feed cursor, a negative offset, or one past the cap."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        kind, match, offset_text = raw.split("|", 2)
        offset = int(offset_text)
    except ValueError as exc:
        raise ValueError("malformed cursor") from exc
    if kind != "search" or match not in ("text", "similar"):
        raise ValueError("malformed cursor")
    if not 0 <= offset < MAX_SEARCH_RESULTS:
        raise ValueError("malformed cursor")
    return cast(SearchMatch, match), offset


def _decode_cursor(cursor: str) -> tuple[int, datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        tier, timestamp, article_id = raw.split("|", 2)
        uuid.UUID(article_id)  # a non-UUID would reach Postgres and 500
        return int(tier), datetime.fromisoformat(timestamp), article_id
    except ValueError as exc:  # binascii and Unicode errors are ValueErrors too
        raise ValueError("malformed cursor") from exc
