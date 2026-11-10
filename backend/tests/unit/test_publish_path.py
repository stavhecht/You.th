"""Structural guard on invariant #1.

Behavioural tests prove that today's code publishes only via review. This one
proves that *tomorrow's* code still does — it fails when someone adds a second
place that can set an article to `published`, which is the regression a
behavioural test cannot see coming.

It is a grep, and deliberately so: it needs no database, runs in milliseconds,
and is the cheapest possible tripwire on the guarantee the whole product rests
on. If it fires, the question to answer is not "how do I make the test pass"
but "should this second path exist at all".
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: The only module allowed to move an article into `published`.
AUTHORISED_MODULE = "services/review.py"

#: Reading the status to *filter* on it is unrestricted — the public feed must
#: query it. Only assignment is controlled.
PUBLISHED_NAMES = {"PUBLISHED", "published"}


def _assignments_to_published(path: Path) -> list[int]:
    """Line numbers where this module assigns a status of `published`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[int] = []

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None

        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue

        # Only care about `<something>.status = ...`
        if not any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in targets
        ):
            continue

        # Either `ArticleStatus.PUBLISHED` or the bare string "published".
        via_enum = isinstance(value, ast.Attribute) and value.attr in PUBLISHED_NAMES
        via_literal = isinstance(value, ast.Constant) and value.value == "published"
        if via_enum or via_literal:
            hits.append(node.lineno)

    return hits


def test_only_the_review_service_can_publish() -> None:
    offenders: dict[str, list[int]] = {}

    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT).as_posix()
        lines = _assignments_to_published(path)
        if lines and relative != AUTHORISED_MODULE:
            offenders[relative] = lines

    assert not offenders, (
        "invariant #1 violated: article status is set to 'published' outside "
        f"{AUTHORISED_MODULE}: {offenders}. Publication must go through "
        "ReviewService.approve(), which records an authenticated reviewer."
    )


def test_the_review_service_actually_publishes() -> None:
    """Guards the guard.

    If ``approve()`` were refactored so this file no longer matched, the test
    above would keep passing while checking nothing.
    """
    lines = _assignments_to_published(APP_ROOT / AUTHORISED_MODULE)
    assert lines, (
        f"{AUTHORISED_MODULE} no longer assigns status='published'. Either the "
        "publish path moved (update AUTHORISED_MODULE) or this guard is now blind."
    )


def test_pipeline_never_references_published() -> None:
    """The generation pipeline should not mention the published state at all.

    Stronger than the assignment check for this package specifically: the
    pipeline's terminal state is `pending_review` or `validation_failed`, so it
    has no legitimate reason to name `published` even in a comparison.
    """
    offenders: list[str] = []
    for path in (APP_ROOT / "pipeline").rglob("*.py"):
        text = path.read_text()
        if "ArticleStatus.PUBLISHED" in text or '"published"' in text:
            offenders.append(path.relative_to(APP_ROOT).as_posix())

    assert not offenders, (
        f"pipeline modules reference the published state: {offenders}. "
        "The pipeline must terminate at pending_review or validation_failed."
    )
