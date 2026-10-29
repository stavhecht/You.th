"""Editorial console request/response models.

The console sees everything the public surface hides: draft content, the
validation report, retrieved-but-uncited sources, pipeline run history.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.api.public.schemas import FeedCardOut
from app.api.schemas_base import CamelModel
from app.discovery.manual import ManualScanStatus
from app.domain.enums import (
    ArticleStatus,
    ContactKind,
    ContactStatus,
    DiscoveryCandidateStatus,
    ImageFrame,
    ResearchCandidateStatus,
    ResearchRunMode,
    ResearchRunStatus,
    RunStatus,
    Stance,
    StudyType,
    Subject,
    UserRole,
    Verdict,
)
from app.research.contracts import (
    Category,
    ResearchParams,
    clean_queries,
    known_sources,
)

# --- auth ------------------------------------------------------------------


class LoginRequest(CamelModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    """Deliberately **not** camelCased.

    ``access_token`` / ``token_type`` / ``expires_in`` are the OAuth 2 bearer
    response field names. Renaming them to fit our house style would break
    every off-the-shelf client for no gain.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ReviewerOut(CamelModel):
    id: str
    email: str
    display_name: str
    role: UserRole


# --- review queue ----------------------------------------------------------


class QueueItemOut(CamelModel):
    id: str
    topic: str
    headline: str
    verdict: Verdict
    status: ArticleStatus
    evidence_grade: StudyType
    #: '4/4 citations resolve'. Computed at draft time, stored, not recomputed.
    validation_badge: str
    #: True when the verdict rests on in-vitro/animal/case-report evidence.
    #: Surfaced in the list so a reviewer can triage before opening anything.
    has_weak_evidence: bool
    #: Reviewer-set. Drives the left rule's colour in the queue, and the tile's
    #: on the public feed. Null means nobody has classified it yet.
    subject: Subject | None = None
    created_at: datetime


class QueuePageOut(CamelModel):
    items: list[QueueItemOut]
    next_cursor: str | None = None


class ExcerptOut(CamelModel):
    """A full-text passage the synthesis model was shown for a source."""

    section: str | None
    text: str


class ReviewSourceOut(CamelModel):
    """A source in the review panel — richer than the public equivalent."""

    source_id: str
    citation_handle: str
    claim: str
    title: str
    journal: str | None
    year: int | None
    study_type: StudyType
    citation_count: int | None
    url: str
    pmid: str | None
    doi: str | None
    #: The abstract the model was shown, so a reviewer can check a cited
    #: sentence against it without leaving the desk.
    abstract: str = ""
    #: False for sources retrieved but not cited. Shown anyway, greyed: a
    #: reviewer needs to see what the model chose to leave out, because
    #: omission is the failure mode human review exists to catch.
    was_cited: bool
    relevance_score: float | None
    #: Drives the inline warning marker in the sources panel.
    is_weak_evidence: bool
    #: The literature has withdrawn this paper. Distinct from weak evidence:
    #: weak means the study is a poor basis for confidence, retracted means it
    #: is not a basis at all.
    retracted: bool = False
    #: Under investigation, not withdrawn. Recorded rather than refused,
    #: because the paper may yet be exonerated.
    concern: bool = False
    #: Which provider said so, e.g. "pubmed: retracted publication".
    retraction_note: str | None = None
    #: Full-text passages the model was shown beside the abstract. An article
    #: may quote a number from one of these that the abstract does not state,
    #: so the reviewer needs them to check it. Empty for most sources.
    excerpts: list[ExcerptOut] = Field(default_factory=list)
    #: Which way APPRAISE found this source pointing for ``claim``. ``None``
    #: means not appraised, which the desk must say rather than leave blank.
    stance: Stance | None = None


class ValidationFailureOut(CamelModel):
    code: str
    message: str
    detail: dict = Field(default_factory=dict)


class ValidationReportOut(CamelModel):
    """API-facing mirror of ``domain.contracts.ValidationReport``.

    Separate rather than reusing the domain model because that one is also
    persisted verbatim into ``articles.validation_report``. Aliasing its fields
    to camelCase would silently change the stored JSON and break every reader
    of existing rows.
    """

    passed: bool
    citations_total: int
    citations_resolved: int
    best_evidence_grade: StudyType
    #: The strongest verdict the cited evidence would have allowed. ``None`` on
    #: articles drafted before it was recorded, which the desk must render as
    #: "not recorded" rather than as any particular verdict.
    verdict_ceiling: Verdict | None = None
    failures: list[ValidationFailureOut] = Field(default_factory=list)
    #: Non-blocking problems for the reviewer; empty on older reports.
    warnings: list[ValidationFailureOut] = Field(default_factory=list)


class ArticleDetailOut(CamelModel):
    """Everything the review screen needs, in one request."""

    id: str
    #: Where this article lives on the public feed once published. Sent for
    #: every draft — the slug is assigned at persist time — so the console gates
    #: the "view on the feed" link on `status`, not on this being set.
    slug: str
    status: ArticleStatus
    #: What kind of thing this assesses; see PATCH /articles/{id}/subject.
    subject: Subject | None = None
    topic: str
    product: str
    target_claims: list[str]
    ingredients: list[str]
    headline: str
    summary: str
    verdict: Verdict
    verdict_qualifier: str | None
    evidence_grade: StudyType
    #: The immutable AI draft. Rendered read-only beside the editor.
    original_content: dict
    #: Human edits, null until first autosave. The editor loads
    #: ``edited_content or original_content``.
    edited_content: dict | None
    validation_report: ValidationReportOut
    #: Grouped by claim in handle order, so the panel renders without
    #: client-side joining.
    sources: list[ReviewSourceOut]
    pipeline_run_id: str | None
    #: Set when a cited source has been retracted since this article was
    #: written. The article keeps its status — this raises it for a human,
    #: it does not withdraw it.
    retraction_flagged_at: datetime | None = None
    retraction_detail: dict | None = None
    created_at: datetime


class SaveContentRequest(CamelModel):
    """Autosave payload. Debounced ~800ms from the editor."""

    content: dict = Field(description="TipTap document")
    base: dict | None = Field(
        default=None,
        description="The edited content this edit started from (null if unedited). "
        "When sent, the save is refused with 412 if the draft has changed since.",
    )


class SetSubjectRequest(CamelModel):
    """Classify what kind of thing the article assesses.

    Explicitly nullable: clearing the subject is a real answer, not a missing
    one — an unclassified article renders in ink, which is the design's resting
    state rather than a broken cell.
    """

    subject: Subject | None = None


class MediaUploadOut(CamelModel):
    """Where a just-uploaded image now lives.

    Only a path comes back. There is no id and no record: the store is
    content-addressed, so the path *is* the identity, and an image is
    referenced only from the document that embeds it.
    """

    #: Origin-relative, e.g. ``/api/media/1f/2a….png``. Goes verbatim into the
    #: editor's image node, and is the only ``src`` shape the server will
    #: accept back.
    src: str
    #: Sniffed from the bytes, not taken from the upload's Content-Type.
    content_type: str
    bytes: int


class RejectRequest(CamelModel):
    #: Required. Rejection reasons are the best available signal for improving
    #: the synthesis prompt, so the API refuses to discard one.
    reason: str = Field(min_length=1, max_length=2000)


class CardPreviewOut(FeedCardOut):
    """The feed tile this draft would publish as.

    Extends the *public* contract rather than restating it, and the import
    direction is the safe one: the console sees everything the public surface
    does plus the draft behind it, so a console schema reading a public one
    carries no information the wrong way.

    Restating the nine fields was the alternative, and it is exactly what this
    endpoint exists to prevent. The preview is rendered by the public feed's
    own ``ArticleCard``, so a shape of its own would be a second definition of
    a tile, free to drift from the one that ships.
    """

    #: True when the picture is the pipeline's portrait cover rather than the
    #: document's own first image. The reviewer is then looking at a different
    #: crop from the picture in the editor beside them, and a tile that
    #: silently differs from the article reads as a bug rather than a framing.
    image_is_generated_cover: bool = False


class RegenerateIllustrationRequest(CamelModel):
    """Which of the article's two pictures to draw again.

    Both, unless a reviewer asked for one. A reviewer who likes the article's
    picture and not the tile should not have to pay for two renders to fix one,
    and — more to the point — should not have to accept a new picture in the
    prose to get a new one on the feed.

    The whole body is optional on the route, so a client that posts nothing
    still gets the pair it always got.
    """

    frames: list[ImageFrame] = Field(
        default_factory=lambda: [ImageFrame.LEAD, ImageFrame.COVER],
        min_length=1,
        max_length=2,
    )

    @field_validator("frames")
    @classmethod
    def _distinct(cls, value: list[ImageFrame]) -> list[ImageFrame]:
        """``["lead", "lead"]`` is one render, not two.

        Rejected rather than silently deduped: it is only ever a client bug,
        and this is the endpoint where a client bug is measured in money.
        """
        if len(set(value)) != len(value):
            raise ValueError("each frame may be named at most once")
        return value


class GeneratedFrameOut(CamelModel):
    """One frame of a regenerated illustration."""

    src: str
    alt: str


class IllustrationOut(CamelModel):
    """The article's frames after a regenerate — always both, redrawn or not.

    Both, because the client's job is to make the document agree with the row,
    and it cannot do that from a partial answer. A response carrying only what
    changed would put the decision "is this lead still the one on the article"
    in the browser, where the pairing rule is not enforced.

    The route deliberately does not rewrite the document itself — see
    ``ReviewService.set_illustration``. The client puts ``lead`` into the image
    node and lets autosave persist it, so the new ``src`` passes through
    ``assert_media_is_ours`` like any other edit.
    """

    lead: GeneratedFrameOut
    cover: GeneratedFrameOut
    #: Which frames this call actually drew. The client already knows what it
    #: asked for; this is what the *server* did, and it is what a reviewer sees
    #: quoted back to them when only half the picture changed.
    redrawn: list[ImageFrame]


# --- pipeline --------------------------------------------------------------


class CreateRunRequest(CamelModel):
    topic: str = Field(min_length=1, description="e.g. 'ashwagandha for stress'")
    blurb: str | None = Field(default=None, description="Optional marketing copy")


class StageRunOut(CamelModel):
    stage: str
    ordinal: int
    status: RunStatus
    error: dict | None
    metrics: dict | None
    #: Provider-namespaced model this stage called; null for the four that call
    #: none. Recorded from the call itself, so it is what actually ran rather
    #: than what the settings say now.
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    #: Computed at read time from app/llm/pricing.py, never stored. Null means
    #: the model is not in the price table — which is *not* zero, and reads in
    #: the console as "unknown" so a newly-pointed-at model cannot look free.
    estimated_cost_usd: Decimal | None
    started_at: datetime | None
    finished_at: datetime | None


class RunOut(CamelModel):
    id: str
    topic: str
    status: RunStatus
    article_id: str | None
    error: dict | None
    #: Lifetime totals across every attempt, so a run that retried reports what
    #: it really spent. Not priceable as a unit — the stages may have run on
    #: different models — which is why the cost below is summed per stage.
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    #: Sum of the per-stage costs, or null if *any* stage that consumed tokens
    #: ran on an unpriced model. All-or-nothing: a partial sum is a plausible
    #: number that is wrong by an order of magnitude, and the per-stage figures
    #: stay visible either way.
    estimated_cost_usd: Decimal | None
    #: Attempts started. >1 means a retryable failure requeued this run; the
    #: stages below are the latest attempt.
    attempts: int
    #: When the run is queued and waiting out a retry backoff. Null otherwise —
    #: distinguishes "waiting deliberately" from "the worker is not running".
    next_attempt_at: datetime | None
    #: Last sign of life while `running`. A timestamp minutes old means the
    #: worker died and the sweep has not reached the run yet — without it a
    #: dead run and a slow one look identical in the console.
    heartbeat_at: datetime | None
    stages: list[StageRunOut]
    created_at: datetime
    finished_at: datetime | None


class RunPageOut(CamelModel):
    items: list[RunOut]
    next_cursor: str | None = None


# --- contact inbox ---------------------------------------------------------


class ContactRequestOut(CamelModel):
    """A "Let us know" submission, as the console sees it.

    Free text written by a member of the public. Rendered as text and never as
    markup — the note and the link are both attacker-controlled.
    """

    id: str
    kind: ContactKind
    name: str | None
    email: str
    link: str | None
    note: str
    status: ContactStatus
    handled_at: datetime | None = None
    created_at: datetime


class SetContactStatusRequest(CamelModel):
    status: ContactStatus


# --- trend discovery -------------------------------------------------------


class DiscoveryCandidateOut(CamelModel):
    """A proposed topic, with the arithmetic that proposed it.

    Everything past ``topic`` exists to be *shown*. The ranking is built from
    constants that were guessed before any real data existed, so a reviewer has
    to be able to see why something is on the list — a score with nothing behind
    it is either believed too readily or ignored entirely.
    """

    id: str
    topic: str
    substance_ui: str
    substance_name: str
    outcome_name: str | None
    score: float
    #: Distinct papers in the scan's window.
    #: Papers behind the substance's surge — what the score and lift measure.
    paper_count: int
    #: Papers behind this specific angle. Equal to ``paper_count`` for a
    #: candidate that names no outcome. Both are shown because they answer
    #: different questions: the first is why the substance is on the desk, the
    #: second is what this particular article would rest on.
    angle_paper_count: int
    #: Mean papers per window over the preceding windows.
    baseline_count: float
    #: ``paper_count`` against ``baseline_count``, Laplace-smoothed.
    lift: float
    #: Study types among those papers. Six case reports and six RCTs are the
    #: same surge and a very different proposition.
    study_mix: dict[str, int]
    top_pmids: list[str]
    status: DiscoveryCandidateStatus
    pipeline_run_id: str | None
    scanned_at: datetime


class DismissCandidateRequest(CamelModel):
    """Why this topic is not worth writing about.

    Required, mirroring ``RejectRequest`` — and the
    ``discovery_candidates_dismissed_has_reason`` CHECK refuses the row without
    one anyway, so making it optional would only turn a 422 into a 500.
    """

    reason: str = Field(min_length=1, max_length=500)


class SuppressedTopicOut(CamelModel):
    """A trend the scan found again and declined to re-propose."""

    topic: str
    #: Verbatim from ``discovery/service.py::suppression_reason`` — "promoted
    #: 2026-09-07", or a dismissal with the date it comes back. Not re-worded
    #: here: one explanation, shown identically by the desk and the CLI.
    reason: str


class TrendScanOut(CamelModel):
    """The state of a manually triggered scan.

    Deliberately not a ``discovery_scans`` row. That table is the window ledger
    — ``next_window`` reads only its succeeded rows — and this is a UI
    affordance about *this process's* last press, which a restart forgets. The
    one field that does come from the ledger is ``last_scan_at``, so the desk
    can still say when a scan last completed after a deploy.
    """

    status: ManualScanStatus
    started_at: datetime | None
    finished_at: datetime | None
    records_seen: int
    observations_written: int
    candidates_proposed: int
    #: The scan's own remarks — a shallow baseline, a truncated seed. Shown
    #: verbatim, because "nothing is trending" and "this could not have proposed
    #: anything" are different answers and the counts cannot tell them apart.
    notes: list[str]
    #: Topics found again and not re-proposed, each with why. The desk renders
    #: these, because "0 proposals" and "0 proposals, and here are the four
    #: trends I am deliberately holding back" are different screens, and only the
    #: second one is trustworthy when it is empty.
    suppressed: list[SuppressedTopicOut]
    error: str | None
    #: When a scan — cron or console — last succeeded. From the ledger.
    last_scan_at: datetime | None


# --- research agent --------------------------------------------------------


class ResearchRunRequest(CamelModel):
    """Optional overrides for one research run; omitted fields use settings.

    The desk button and n8n's automation call send the same shape, and both
    become the same ``ResearchParams``.
    """

    target_article_count: int | None = Field(default=None, ge=1, le=10)
    categories: list[Category] | None = None
    trend_window_days: int | None = Field(default=None, ge=1, le=30)
    geo: str | None = Field(default=None, min_length=2, max_length=2)
    language: str | None = Field(default=None, min_length=2, max_length=2)
    #: A subset of the sources settings enable (``RESEARCH_ENABLED_SOURCES``).
    enabled_sources: list[str] | None = None
    #: Lowers the per-source request budget; cannot raise it.
    max_requests_per_source: int | None = Field(default=None, ge=1, le=200)
    #: Raise the selection bars for this run; settings are the floor.
    min_trend_score: float | None = Field(default=None, ge=0, le=100)
    min_data_coverage: float | None = Field(default=None, ge=0, le=1)
    #: Topics to look into on top of what the sweep finds (at most 10).
    queries: list[str] | None = Field(default=None, max_length=10)

    # The same checks ``ResearchParams`` makes, here so a bad value is a 422
    # at the edge rather than a validation error inside the route.
    @field_validator("enabled_sources")
    @classmethod
    def _known_sources(cls, value: list[str] | None) -> list[str] | None:
        return known_sources(value)

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str] | None) -> list[str] | None:
        return clean_queries(value)

    def to_params(self) -> ResearchParams:
        # ``by_alias=False`` is load-bearing: ``CamelModel`` serialises by
        # alias, ``ResearchParams`` is snake_case and ignores unknown keys, so
        # a plain ``model_dump()`` silently dropped every two-word field
        # (``targetArticleCount``, ``trendWindowDays``) while one-word ones
        # (``geo``, ``categories``) survived and made it look like it worked.
        return ResearchParams.model_validate(self.model_dump(by_alias=False))


class AutomationResearchRequest(ResearchRunRequest):
    #: ``weekly`` from the scheduled workflow, ``manual`` from the webhook one.
    mode: ResearchRunMode = ResearchRunMode.WEEKLY


class ResearchCandidateOut(CamelModel):
    """One topic a research run considered.

    ``signals`` and ``scores`` are the stored JSON verbatim — provider data and
    component scores — so their inner keys are snake_case.
    """

    id: str
    canonical_topic: str
    category: str | None
    queries: list[str]
    status: ResearchCandidateStatus
    discard_reason: str | None
    evidence_status: str | None
    overall: float | None
    rank: int | None
    scores: dict
    signals: dict
    pipeline_run_id: str | None
    decided_at: datetime | None
    dismiss_reason: str | None
    #: The persistent topic (``research_topics``) this candidate observes.
    topic_ref_id: str | None = None


class ResearchRunOut(CamelModel):
    id: str
    label: str
    mode: ResearchRunMode
    status: ResearchRunStatus
    stage: str | None
    params: dict
    provider_status: dict
    stage_log: list
    notes: list
    error: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: Filled on the single-run read; empty on the list.
    candidates: list[ResearchCandidateOut] = Field(default_factory=list)
