"""Finding which stored images a document still points at."""

from __future__ import annotations

import json

from scripts.prune_media import referenced_digests

DIGEST = "ab" + "c" * 62


def test_paths_are_found_inside_json_documents() -> None:
    doc = json.dumps(
        {
            "type": "doc",
            "content": [{"type": "image", "attrs": {"src": f"/api/media/ab/{'c' * 62}.webp"}}],
        }
    )
    assert referenced_digests([doc]) == {DIGEST}


def test_a_path_elsewhere_is_not_a_reference() -> None:
    assert (
        referenced_digests(["/api/other/ab/" + "c" * 62 + ".webp", "no images here"]) == set()
    )
