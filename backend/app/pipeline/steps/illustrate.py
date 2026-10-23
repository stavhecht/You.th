"""Stage 5 — draw the article's pictures, or don't.

**This stage cannot fail a run, and that is the whole of its error handling.**
The picture is decorative. An article without one renders as a typographic
tile, which is the design's resting state and which a reader cannot tell from
an editorial choice — so a provider outage that failed the run would trade a
complete, citable article for no article at all, in exchange for a decoration.

That is the deliberate *opposite* of ``retrieval/throttle.py``'s rule, and the
two are worth holding side by side, because the difference is not "how
important is this stage". Recall lost to a 429 produces a more cautious verdict,
which reads as a correct answer: it is invisible unless the stage fails loudly.
A missing picture is visible on the reviewer's very next screen and on every
tile. The rule is *fail on the degradations nobody can see*, and this one
everybody can.

So it is nearer to ``services/no_evidence.py``: record why, in the metrics, and
carry on. ``cause`` is what separates "no key is configured" from "the provider
503'd" — the same blank tile, and different problems.

**Nothing here enters the token ledger.** ``record_usage`` is never called. An
image is billed per image by a provider nobody has priced, while
``pipeline_stage_runs`` prices four token columns; writing a model id against
four zeros is exactly the "unpriced model reads as free" mistake
``llm/pricing.py`` exists to refuse. Count, bytes, latency and model go into
``metrics`` instead. Say "not measured", not "free" — the standing embeddings
already have.

**It writes rows, not files** — ``media_objects``, through the session it is
constructed with. It used to write files, and the difference is worth stating
because it removed a wart rather than adding one: image bytes now sit inside
the pipeline transaction, so they land with the orchestrator's stage commit and
are rolled back with a failed stage, instead of surviving on disk as orphans
from a run that never finished. The stage still does not commit — that rule is
the orchestrator's and is not relaxed here.

One orphan case remains and is deliberate. A lead that stores followed by a
cover that fails leaves the lead's row to be committed by a stage that then
reports no picture at all. That is the same orphan the store has always
tolerated: rows are content-addressed and immutable, so the cost is bytes, and
collection is deferred (``services/media.py``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.imagery.base import ImageClient, ImageError
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.services.illustration import generate_illustration, seed_for_run

logger = logging.getLogger(__name__)

#: Retries after a failed render before the article goes out text-only.
IMAGE_RETRIES = 1


@dataclass(frozen=True, slots=True)
class IllustrationConfig:
    """How big the pictures are and whether to draw them.

    A narrow config rather than the whole ``Settings`` object, the same shape
    as ``RerankConfig``: a stage handed Settings can reach anything, and a
    stage that can reach anything eventually does.

    No longer says *where* they go — there is one store and the stage reaches
    it through the session, the same way it reaches everything else it writes.
    """

    max_bytes: int
    lead_size: tuple[int, int]
    cover_size: tuple[int, int]
    #: Whether imagery was asked for at all. Read only when the client is
    #: ``None``, to tell "switched off" from "no key configured" — two states
    #: that produce an identical article and want opposite responses.
    enabled: bool
    #: ``IMAGE_COVER_MODE=crop``: one render, the cover cut from the lead.
    cover_from_lead: bool = False


class IllustrateStage:
    """Stage 5 of 7. Never raises for an image failure."""

    name = StageName.ILLUSTRATE

    def __init__(
        self,
        client: ImageClient | None,
        config: IllustrationConfig,
        session: AsyncSession,
    ) -> None:
        self._client = client
        self._config = config
        # The pipeline session, as ValidateStage and PersistStage take it —
        # image bytes are rows now, and they belong in the run's transaction.
        self._session = session

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Generate both frames and attach them to the context.

        The one exception to "never raises": a missing draft or extraction
        means SYNTHESIZE did not run, which is a broken pipeline rather than a
        failed image. Reporting that as ``cause: "unexpected"`` and continuing
        would hide an assembly bug behind a decorative feature.
        """
        if ctx.draft is None or ctx.extraction is None:
            raise StageError(self.name, "synthesis stage did not run")

        if ctx.illustration is not None:
            # A revision pass (VALIDATE -> SYNTHESIZE) comes back through here.
            # The pictures are claim-free and do not depend on the draft's
            # wording, so the ones already drawn — and billed — stand.
            ctx.record_metrics(self.name, {"generated": 0, "cause": "kept_from_first_draft"})
            return ctx

        if self._client is None:
            return self._skip(ctx, "disabled" if not self._config.enabled else "not_configured")

        started = time.monotonic()
        seed = seed_for_run(ctx.run_id)
        attempts = 0
        while True:
            attempts += 1
            try:
                illustration = await generate_illustration(
                    self._client,
                    product=ctx.extraction.product,
                    topic=ctx.topic,
                    # None, always, on this path: `subject` is reviewer-set and
                    # the article row does not exist yet. The prompt builder
                    # infers which objects to photograph from the text instead —
                    # which is why the actives are worth passing. See
                    # imagery/prompt.py.
                    subject=None,
                    ingredients=" ".join(ctx.extraction.ingredients),
                    outcome=" ".join(ctx.extraction.target_claims),
                    lead_size=self._config.lead_size,
                    cover_size=self._config.cover_size,
                    session=self._session,
                    max_bytes=self._config.max_bytes,
                    seed=seed,
                    cover_from_lead=self._config.cover_from_lead,
                )
                break
            except ImageError as exc:
                if attempts <= IMAGE_RETRIES:
                    # One retry: a cold or briefly overloaded provider is the
                    # common failure, and the second call usually lands. More
                    # would hold the run open for a decoration.
                    logger.info(
                        "illustration failed for run %s (%s); retrying once", ctx.run_id, exc
                    )
                    continue
                logger.warning("illustration failed for run %s: %s", ctx.run_id, exc)
                return self._skip(
                    ctx,
                    "provider_error",
                    detail=str(exc),
                    seed=seed,
                    started=started,
                    attempts=attempts,
                )
            except Exception as exc:
                # Broader than this codebase's usual instinct, and deliberate: a
                # bug in our own prompt builder or encoder must not fail every
                # run for a decoration. The traceback still reaches the log and
                # the type still reaches the metrics, so it is recorded rather
                # than swallowed. Not retried: a bug fails the same way twice.
                logger.exception("illustration raised unexpectedly for run %s", ctx.run_id)
                return self._skip(
                    ctx,
                    "unexpected",
                    detail=f"{type(exc).__name__}: {exc}",
                    seed=seed,
                    started=started,
                )

        ctx.record_metrics(
            self.name,
            {
                # 0 or 2, never 1 — this stage always asks for both frames, and
                # generate_illustration writes nothing until both are stored.
                # A reviewer can later redraw one alone; that is the console's
                # doing and does not touch this run's metrics.
                "generated": 2,
                # What was billed: one render in crop mode, two otherwise.
                "renders": 1 if self._config.cover_from_lead else 2,
                "attempts": attempts,
                "cause": None,
                # Read off the lead because provenance lives on the frames now.
                # On this path the two are identical by construction: one call,
                # one prompt, one seed.
                "model": illustration.lead.model,
                "seed": seed,
                "lead_src": illustration.lead.src,
                "cover_src": illustration.cover.src,
                "prompt": illustration.lead.prompt,
                "duration_ms": _elapsed_ms(started),
            },
        )
        return ctx.model_copy(update={"illustration": illustration})

    def _skip(
        self,
        ctx: PipelineContext,
        cause: str,
        *,
        detail: str | None = None,
        seed: int | None = None,
        started: float | None = None,
        attempts: int | None = None,
    ) -> PipelineContext:
        """Record why there is no picture and hand the context back untouched.

        Untouched, not copied: nothing changed. ``ctx.illustration`` stays
        ``None`` and PersistStage writes the article without an image node.
        """
        metrics: dict[str, Any] = {
            "generated": 0,
            "cause": cause,
            "detail": detail,
            "model": None,
            "seed": seed,
        }
        if started is not None:
            metrics["duration_ms"] = _elapsed_ms(started)
        if attempts is not None:
            metrics["attempts"] = attempts
        ctx.record_metrics(self.name, metrics)
        return ctx


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
