"""Feed cards are derived from the article. They are never generated.

There is no card-generation prompt and no card field the model controls
independently. One AI output, two renderings — so the card and the article
cannot contradict each other. A separate generation call is the obvious
alternative and it is exactly the thing that produces a confident card sitting
above a hedged article.

Derivation runs at publish time, from ``COALESCE(edited_content,
original_content)``, so the card reflects what the human approved rather than
what the model first wrote. The result is materialised onto the article row to
keep the feed query a single index scan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.enums import Verdict
from app.services.media import MEDIA_SRC_RE
from app.services.tiptap import beat_text, iter_nodes

CARD_EXCERPT_MAX_CHARS = 180

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class DerivedCard:
    headline: str
    excerpt: str
    verdict: Verdict
    #: The article's own first picture, or the paired portrait framing of it,
    #: or None. See ``_lead_image`` and ``_paired_cover``.
    image: str | None = None
    image_alt: str | None = None
    #: True when ``image`` is the generated portrait cover rather than the
    #: document's own first picture. The public feed neither knows nor needs
    #: to; the console's preview says so, because "why is the tile a different
    #: crop from the article" is otherwise a question with no answer on screen.
    image_is_generated_cover: bool = False


def derive_card(
    headline: str,
    body_doc: dict[str, Any],
    verdict: Verdict,
    *,
    generated: dict[str, Any] | None = None,
) -> DerivedCard:
    """Compute the card from the approved article content.

    The excerpt is the first sentence of beat 1 with citation markers stripped
    — ``[S1]`` in a feed card is noise to a reader with no source list in front
    of them.

    ``generated`` is ``articles.generated_imagery`` — both frames the pipeline
    drew, or ``None``. It defaults to ``None`` so every article written before
    the pipeline could draw, and every caller that does not care, keeps exactly
    the behaviour it had.
    """
    first_beat = beat_text(body_doc, beat=1, keep_citations=False)
    excerpt = _first_sentence(first_beat)
    image, image_alt = _lead_image(body_doc)
    cover = _paired_cover(generated, image)
    return DerivedCard(
        headline=headline,
        excerpt=_truncate(excerpt, CARD_EXCERPT_MAX_CHARS),
        verdict=verdict,
        image=cover or image,
        # The alt comes from the document's own node in both branches. The two
        # frames show the same still life, and a reviewer who rewrote the alt
        # was describing the picture, not the file.
        image_alt=image_alt,
        image_is_generated_cover=cover is not None,
    )


def _paired_cover(generated: dict[str, Any] | None, lead_in_doc: str | None) -> str | None:
    """The portrait cover, but only while it is still paired with the lead.

    The rule this function exists to keep true is that a feed tile cannot show
    a picture the article does not contain. A portrait frame held on the
    article row is, read literally, exactly such a picture — so what makes it
    honest is that both frames were drawn in one step, from one prompt, with
    one seed, for this article, and that the document's first image is still
    the landscape one of the pair.

    The moment a reviewer replaces that picture, deletes it, or places another
    above it, the pair is broken by the only person who could break it, and the
    card falls back to what their document actually shows. Failing toward "no
    cover" rather than "wrong cover" is the whole design.

    ``MEDIA_SRC_RE`` is re-checked here because **the cover is not in the
    document**, so ``assert_media_is_ours`` — which walks the document — never
    sees it. Without this line, approve-time media validation would have a hole
    exactly the width of the one field the reader's feed renders in an
    ``<img src>``.
    """
    if not generated or lead_in_doc is None:
        return None

    lead = generated.get("lead")
    cover = generated.get("cover")
    if not isinstance(lead, dict) or not isinstance(cover, dict):
        return None

    # Pairing: the document's first image must still be the frame this cover
    # was drawn beside. `lead_in_doc` is non-None here, so a missing or
    # malformed `lead.src` can never compare equal by accident.
    if lead.get("src") != lead_in_doc:
        return None

    src = cover.get("src")
    if not isinstance(src, str) or not MEDIA_SRC_RE.match(src):
        return None
    return src


def _lead_image(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    """The first image node's ``src``, or ``(None, None)``.

    Same rule as every other field here: the card shows what the article shows,
    so the tile is the article's own first picture rather than an image chosen
    for the feed. A reviewer who wants a different tile changes the article,
    which is the only edit that cannot make the two disagree.

    Only ``image`` nodes count — a ``youtube`` block would mean fetching a
    thumbnail from a third party on every feed render, which is exactly the
    tracking the article page's click-to-load facade exists to avoid.

    ``None`` is the normal case and not a fallback to fill in: the feed draws a
    typographic tile, which is the design's resting state.
    """
    for node in iter_nodes(doc):
        if node.get("type") != "image":
            continue
        attrs: Any = node.get("attrs")
        if not isinstance(attrs, dict):
            continue
        src = attrs.get("src")
        if not isinstance(src, str) or not src:
            continue
        alt = attrs.get("alt")
        return src, alt if isinstance(alt, str) and alt else None
    return None, None


def _first_sentence(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    parts = _SENTENCE_END_RE.split(cleaned, maxsplit=1)
    return parts[0].strip()


def _truncate(text: str, limit: int) -> str:
    """Truncate on a word boundary, with an ellipsis.

    Mid-word truncation in a card looks like a rendering bug, which undermines
    exactly the credibility this product trades on.
    """
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1]
    if " " in clipped:
        clipped = clipped[: clipped.rfind(" ")]
    return clipped.rstrip(",;: ") + "…"
