"""Re-check cited sources against the retraction record, and flag what breaks.

The ingest screen in ``RetrieveStage`` only catches papers that were already
retracted when we first saw them. Retractions land *after* publication, usually
by months or years, so a source cached clean in August and retracted in November
stays cited by a live article and nothing ever looks at it again. This is the
half of the feature that covers that, and it is the half that has to be run on a
schedule to be worth anything.

Dry by default::

    python -m scripts.check_retractions              # report only
    python -m scripts.check_retractions --apply      # write
    python -m scripts.check_retractions --scope all  # not just cited sources

**Scope is cited sources by default**, meaning those referenced by an article
that is published or awaiting review. A few dozen papers, one batched PubMed
call, and it covers every source a reader can currently reach. ``--scope all``
walks the whole cache, which catches a retraction before the paper is ever
cited — worth doing occasionally, and wasteful as the default because most
cached rows support no article.

**Nothing is deleted and nothing is unpublished.** A retracted source has its
row marked, and every article citing it gets ``retraction_flagged_at`` set so it
raises a banner and appears in the console. A human decides what happens next.
Deleting the source would break the provenance of a published article to tidy up
a citation, and withdrawing the article automatically would be the machine
deciding what the public sees — which is the thing invariant #1 exists to
prevent, pointed the other way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.retrieval.factory import USER_AGENT
from app.retrieval.retraction_sweep import build_checker, sweep
from app.retrieval.retractions import RetractionVerdict


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; otherwise report")
    parser.add_argument(
        "--scope",
        choices=("cited", "all"),
        default="cited",
        help="cited (default): sources cited by a published or pending article",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="check at most N sources (0 = no cap)"
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"Checking sources, scope={args.scope}…")
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as http:
        result = await sweep(
            get_session_factory(),
            build_checker(settings, http),
            scope=args.scope,
            limit=args.limit,
            apply=args.apply,
        )

    if not result.papers:
        print(f"No sources in scope={args.scope!r}. Nothing to check.")
        return 0

    counts = result.counts
    checked = counts["clean"] + counts["retracted"] + counts["concern"]
    print(
        f"  checked={checked} of {len(result.papers)}  clean={counts['clean']}  "
        f"retracted={counts['retracted']}  concern={counts['concern']}  "
        f"unchecked={counts['unchecked']}"
    )
    if counts["unchecked"]:
        # Called out rather than buried in the totals. An unchecked paper is
        # not a clean one, and a sweep that quietly reports "0 retracted" after
        # reaching nothing is exactly the false assurance this feature exists
        # to avoid.
        print(
            f"  ! {counts['unchecked']} source(s) could not be checked — these are "
            "NOT verified clean and will be retried next sweep"
        )

    for row, _ in result.papers:
        verdict = result.verdicts.get(row.id, RetractionVerdict())
        if verdict.retracted or verdict.concern:
            label = "RETRACTED" if verdict.retracted else "CONCERN  "
            print(f"  {label} pmid={row.pmid} doi={row.doi} — {row.title[:70]}")
            print(f"            {verdict.detail}")

    if result.flagged_articles:
        print(f"\n  {len(result.flagged_articles)} article(s) cite a retracted source:")
        for article_id, source_ids in result.flagged_articles.items():
            print(f"    {article_id}  ({len(source_ids)} source(s))")

    if not result.written:
        print("\nDry run. Re-run with --apply to write.")
        return 0
    print(
        f"\nWrote {checked} check result(s) and flagged "
        f"{len(result.flagged_articles)} article(s)."
    )
    return 0


async def main() -> int:
    """Wraps ``_run`` so the engine is disposed inside the same event loop.

    Disposing from a ``finally`` around ``asyncio.run`` starts a *second* loop
    and asyncpg's connections belong to the first, which surfaces as "attached
    to a different loop" after the work has already succeeded.
    """
    try:
        return await _run()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
