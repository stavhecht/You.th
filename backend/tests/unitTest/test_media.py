"""Reviewer-uploaded media: what gets stored, and what a document may reference.

These are the tests for a directory that is written by an authenticated user
and read by every reader of a published article. The two properties worth
holding onto:

* the store decides what a file is from its bytes, never from what the upload
  claimed — so an HTML page named ``photo.png`` cannot be served back from our
  own origin;
* a document can only point at media we stored, so nothing a reviewer pastes
  becomes an ``<img src>`` or an iframe on the public site.

The rest — flattening, card derivation — is here because media blocks are new
siblings of the beat paragraphs, and the functions that walk those paragraphs
predate them.
"""

from __future__ import annotations

import hashlib

import pytest

from app.domain.contracts import ArticleBody
from app.domain.enums import Verdict
from app.services.card import derive_card
from app.services.media import (
    MEDIA_MAX_WIDTH,
    MEDIA_SRC_RE,
    UnsafeMediaError,
    UnsupportedMediaError,
    assert_media_is_ours,
    image_node,
    prepare_image,
    sniff_image,
)
from app.services.tiptap import (
    beat_text,
    body_text_to_doc,
    cited_handles_in_doc,
    doc_to_plain_text,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + b"\x2c\x00\x00\x00" + b"WEBP" + b"\x00" * 40


# ---------------------------------------------------------------------------
# Sniffing. The client's filename and Content-Type never reach this.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp")],
)
def test_each_accepted_format_is_recognised(data: bytes, expected: str) -> None:
    assert sniff_image(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<!doctype html><script>alert(1)</script>",
        b"PK\x03\x04",
        b"",
    ],
    ids=["svg", "html", "zip", "empty"],
)
def test_script_bearing_and_unknown_bytes_are_refused(data: bytes) -> None:
    """SVG is the one that matters.

    It is an image everywhere else in the world and a script host here: served
    from our own origin and embedded in an article, it is stored XSS against
    every reader. The four formats on the allowlist cannot carry script, which
    is why the list is those four.
    """
    assert sniff_image(data) is None


def test_a_file_named_like_an_image_is_still_refused() -> None:
    with pytest.raises(UnsupportedMediaError):
        prepare_image(b"<!doctype html><script>alert(1)</script>")


# ---------------------------------------------------------------------------
# Storage. Content-addressed, so nothing client-supplied reaches the key.
#
# These exercise `prepare_image`, which is every decision `store_image` makes
# except the INSERT: what the bytes are, what they are keyed by, and what path
# a document will hold. Deliberately DB-free — seven suites already skip
# silently when Postgres is down, and the shape of a media path is not a thing
# that should quietly stop being checked. The INSERT itself is covered in
# test_db_invariants.py, where a real database can answer for it.
# ---------------------------------------------------------------------------


def test_stored_path_matches_the_only_shape_the_server_accepts_back() -> None:
    """The store and the document check agree by construction.

    If these two drift, every upload succeeds and every save is refused.
    """
    stored = prepare_image(PNG).stored()
    assert MEDIA_SRC_RE.match(stored.src)
    assert stored.content_type == "image/png"
    assert stored.size == len(PNG)


def test_the_key_is_the_digest_of_the_bytes() -> None:
    """What makes the store content-addressed, and what the URL contains."""
    prepared = prepare_image(JPEG)
    assert prepared.digest == hashlib.sha256(JPEG).hexdigest()
    assert prepared.data == JPEG
    assert prepared.src.endswith(f"{prepared.digest[2:]}.jpg")


def test_the_same_image_twice_is_one_key() -> None:
    """Content addressing, and the reason two writers can never collide."""
    assert prepare_image(PNG).digest == prepare_image(PNG).digest
    assert prepare_image(PNG).src == prepare_image(PNG).src


def test_different_images_do_not_share_a_path() -> None:
    assert prepare_image(PNG).src != prepare_image(GIF).src


def test_the_extension_comes_from_the_bytes_not_the_caller() -> None:
    """A WebP is keyed and served as a WebP whatever anyone claims it is."""
    prepared = prepare_image(WEBP)
    assert prepared.extension == "webp"
    assert prepared.content_type == "image/webp"
    assert prepared.src.endswith(".webp")


# ---------------------------------------------------------------------------
# What a document may reference. Enforced on save and again on approve.
# ---------------------------------------------------------------------------


#: A path of the shape ``store_image`` produces, without touching a filesystem.
_STORED_SRC = "/api/media/ab/" + "c" * 62 + ".png"


def _doc(*nodes: dict) -> dict:
    return {"type": "doc", "content": list(nodes)}


def test_the_pipelines_own_image_node_passes_the_document_check() -> None:
    """The writer and the checker have to agree, and this is what keeps them in step.

    ``image_node`` builds the node ``PersistStage`` puts a generated picture in;
    ``assert_media_is_ours`` is what refuses a node on every autosave. If they
    ever disagree, the pipeline writes an article whose *first* autosave is
    rejected — with an error message aimed at a paste the reviewer never made.
    """
    node = image_node(prepare_image(PNG).src, "A generated still-life illustration.")

    assert_media_is_ours(_doc(node))


def test_the_generated_node_carries_the_layout_the_editor_would_write() -> None:
    """Full width, no wrap — matching MediaNodes.ts's own defaults.

    So loading the document into the editor and saving it straight back is a
    no-op rather than a spurious first edit.
    """
    node = image_node(_STORED_SRC, "alt")

    assert node["attrs"]["width"] == MEDIA_MAX_WIDTH
    assert node["attrs"]["align"] == "none"


def test_an_uploaded_image_and_a_youtube_id_are_accepted() -> None:
    assert_media_is_ours(
        _doc(
            {"type": "image", "attrs": {"src": prepare_image(PNG).src, "alt": "A chart"}},
            {"type": "youtube", "attrs": {"videoId": "dQw4w9WgXcQ"}},
        )
    )


def test_a_section_heading_is_not_mistaken_for_media() -> None:
    """The check ignores every node that is not an image or a video.

    Worth pinning: the walk is recursive and deliberately looks everywhere, so
    a new block type reaching it must pass through rather than be rejected as
    an unrecognised node.
    """
    assert_media_is_ours(
        _doc(
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "What the trials measured"}],
            },
            {"type": "image", "attrs": {"src": prepare_image(PNG).src, "alt": "A chart"}},
        )
    )


@pytest.mark.parametrize(
    "src",
    [
        "https://example.com/tracker.png",
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "/api/media/../../../etc/passwd",
        "/api/media/ab/cd.png",
        "/etc/passwd",
        "",
        None,
    ],
    ids=[
        "remote",
        "javascript",
        "data-uri",
        "traversal",
        "wrong-shape",
        "absolute-path",
        "empty",
        "missing",
    ],
)
def test_an_image_we_did_not_store_is_refused(src: object) -> None:
    with pytest.raises(UnsafeMediaError, match="uploaded through the console"):
        assert_media_is_ours(_doc({"type": "image", "attrs": {"src": src}}))


@pytest.mark.parametrize(
    "video_id",
    [
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXc",
        "dQw4w9WgXcQ!",
        '" onload="alert(1)',
        "",
        None,
    ],
    ids=["a-url", "too-short", "bad-character", "attribute-escape", "empty", "missing"],
)
def test_anything_that_is_not_a_video_id_is_refused(video_id: object) -> None:
    """The document stores an id, never a URL.

    The renderer builds the embed src from it, so there is no string a reviewer
    can supply that reaches an iframe unexamined.
    """
    with pytest.raises(UnsafeMediaError, match="YouTube video id"):
        assert_media_is_ours(_doc({"type": "youtube", "attrs": {"videoId": video_id}}))


def test_media_nested_inside_a_paragraph_is_still_checked() -> None:
    """The walk does not trust the document's shape.

    Media blocks sit at the top level when the editor puts them there. A check
    that only looks at the top level is a check an attacker reads once.
    """
    with pytest.raises(UnsafeMediaError):
        assert_media_is_ours(
            _doc(
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Before"},
                        {"type": "image", "attrs": {"src": "https://example.com/x.png"}},
                    ],
                }
            )
        )


# ---------------------------------------------------------------------------
# Layout. A width and a wrap, not a stylesheet.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("align", ["none", "left", "right"])
@pytest.mark.parametrize("width", [20, 45, 100, 33.5])
def test_a_width_and_a_wrap_in_range_are_accepted(align: str, width: float) -> None:
    assert_media_is_ours(
        _doc(
            {
                "type": "youtube",
                "attrs": {"videoId": "dQw4w9WgXcQ", "align": align, "width": width},
            }
        )
    )


def test_media_with_no_layout_attributes_is_still_valid() -> None:
    """The whole reason absent is not the same as wrong.

    Every article written before media could be laid out has neither attribute.
    A check that invalidates those rows is a migration wearing a validator's
    clothes — and it would surface as a reviewer's autosave failing on a draft
    they had not touched.
    """
    assert_media_is_ours(_doc({"type": "youtube", "attrs": {"videoId": "dQw4w9WgXcQ"}}))


@pytest.mark.parametrize(
    "width",
    [0, 19, 101, 1_000_000_000, -50, "45", "45%", True, None.__class__],
    ids=[
        "zero",
        "under-floor",
        "over-ceiling",
        "absurd",
        "negative",
        "numeric-string",
        "percent-string",
        "bool",
        "a-type",
    ],
)
def test_a_width_outside_what_a_column_can_show_is_refused(width: object) -> None:
    """`width: 1e9` is the one this exists for.

    Nothing here can execute, but a number the renderer turns into a percentage
    is still a number that reaches a reader's page, and 20–100 is the range a
    column can actually express.
    """
    # Two branches, two messages: a non-number is named as such, a number out
    # of range is told what the range is.
    with pytest.raises(UnsafeMediaError, match=r"% wide|not a number"):
        assert_media_is_ours(
            _doc({"type": "youtube", "attrs": {"videoId": "dQw4w9WgXcQ", "width": width}})
        )


@pytest.mark.parametrize(
    "align",
    ["centre", "float: left", "'; drop", "LEFT", "", 1],
    ids=["near-miss", "css", "injection-ish", "wrong-case", "empty", "a-number"],
)
def test_an_alignment_the_renderer_cannot_express_is_refused(align: object) -> None:
    """Alignment is one of three words, never CSS.

    The renderer maps the word to a class it already ships, which is what keeps
    a reviewer from being able to put style on a published page at all.
    """
    with pytest.raises(UnsafeMediaError, match="aligned"):
        assert_media_is_ours(
            _doc({"type": "image", "attrs": {"src": _STORED_SRC, "align": align}})
        )


def test_layout_is_checked_on_images_too() -> None:
    with pytest.raises(UnsafeMediaError, match="% wide"):
        assert_media_is_ours(
            _doc({"type": "image", "attrs": {"src": _STORED_SRC, "width": 5000}})
        )


def test_a_document_with_no_media_passes() -> None:
    assert_media_is_ours(
        body_text_to_doc(
            ArticleBody(
                beat_1_claim="It claims to reduce stress.",
                beat_2_evidence="Cortisol fell [S1].",
                beat_3_bottom_line="Early evidence only.",
            )
        )
    )


# ---------------------------------------------------------------------------
# Media blocks are new siblings of the beats. Nothing that walks the beats may
# change its answer because one is present.
# ---------------------------------------------------------------------------


def _doc_with_media(image_src: str = "/api/media/ab/" + "c" * 62 + ".png") -> dict:
    doc = body_text_to_doc(
        ArticleBody(
            beat_1_claim="It claims to reduce stress. It also claims better sleep.",
            beat_2_evidence="Cortisol fell [S1].",
            beat_3_bottom_line="Early evidence only.",
        )
    )
    doc["content"].insert(0, {"type": "image", "attrs": {"src": image_src, "alt": "Chart"}})
    doc["content"].append({"type": "youtube", "attrs": {"videoId": "dQw4w9WgXcQ"}})
    return doc


def test_beats_are_still_addressable_around_media() -> None:
    doc = _doc_with_media()
    assert beat_text(doc, beat=1).startswith("It claims")
    assert beat_text(doc, beat=3) == "Early evidence only."


def test_citations_are_still_found_around_media() -> None:
    assert cited_handles_in_doc(_doc_with_media()) == {"S1"}


def test_flattening_ignores_media_rather_than_emitting_blanks() -> None:
    """Search text and card excerpts are prose; an image contributes none."""
    text = doc_to_plain_text(_doc_with_media(), keep_citations=False)
    assert text.startswith("It claims")
    assert "\n\n\n" not in text


def test_the_card_excerpt_is_unchanged_by_a_leading_image() -> None:
    """The card is derived, not generated (DESIGN.md §3.3).

    An image inserted above beat 1 is the obvious way that derivation could
    start returning an empty excerpt, because position is the thing a reviewer
    can change and ``attrs.beat`` is the thing they cannot.
    """
    card = derive_card("Ashwagandha and stress", _doc_with_media(), Verdict.MIXED)
    assert card.excerpt == "It claims to reduce stress."
