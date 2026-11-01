"""``chunk_text``: fixed-size word windows that overlap their neighbour."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.retrieval.chunking import CHUNK_SETTINGS, CHUNK_WORDS, OVERLAP_WORDS, chunk_text


def _words(count: int) -> str:
    return " ".join(f"w{index}" for index in range(count))


def test_a_typical_abstract_is_one_chunk() -> None:
    """The median cached abstract is ~250 words. One chunk means it is embedded
    whole, exactly as it was before chunking existed."""
    assert chunk_text(_words(250)) == [_words(250)]


def test_the_chunk_settings_name_the_numbers_they_came_from() -> None:
    """Stored per source; changing either number must change this string, or
    papers chunked the old way would still look current."""
    assert str(CHUNK_WORDS) in CHUNK_SETTINGS
    assert str(OVERLAP_WORDS) in CHUNK_SETTINGS


def test_an_abstract_of_exactly_one_chunk_is_not_split() -> None:
    assert len(chunk_text(_words(CHUNK_WORDS))) == 1


def test_empty_text_has_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n ") == []


def test_neighbouring_chunks_share_exactly_the_overlap() -> None:
    chunks = [chunk.split() for chunk in chunk_text(_words(400))]

    assert len(chunks) > 1
    for before, after in pairwise(chunks):
        assert before[-OVERLAP_WORDS:] == after[:OVERLAP_WORDS]


def test_every_chunk_fits_the_size_and_every_word_is_kept() -> None:
    text = _words(333)
    chunks = [chunk.split() for chunk in chunk_text(text)]

    assert all(len(chunk) <= CHUNK_WORDS for chunk in chunks)
    # Dropping each chunk's overlap with the previous one rebuilds the text.
    rebuilt = chunks[0] + [word for chunk in chunks[1:] for word in chunk[OVERLAP_WORDS:]]
    assert rebuilt == text.split()


def test_the_last_chunk_ends_at_the_last_word() -> None:
    """No trailing chunk made only of words the previous chunk already had."""
    chunks = chunk_text(_words(CHUNK_WORDS + 1))

    assert len(chunks) == 2
    assert chunks[-1].split()[-1] == f"w{CHUNK_WORDS}"


def test_a_sentence_cut_by_a_boundary_is_whole_in_the_next_chunk() -> None:
    """The reason for the overlap: a short sentence across a boundary survives."""
    before = _words(CHUNK_WORDS - 5).split()  # the sentence's 7 words straddle word 120
    sentence = "creatine improved sprint performance in trained adults"
    after = _words(100).split()
    chunks = chunk_text(" ".join([*before, sentence, *after]))

    assert sentence not in chunks[0]
    assert sentence in chunks[1]


def test_size_and_overlap_can_be_set() -> None:
    assert chunk_text("a b c d e f g", size=3, overlap=1) == ["a b c", "c d e", "e f g"]


@pytest.mark.parametrize(("size", "overlap"), [(10, 10), (10, 12), (10, -1)])
def test_an_overlap_that_would_never_advance_is_refused(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("a b c", size=size, overlap=overlap)
