"""Domain enumerations.

These mirror the Postgres enum types in migrations/0001_initial.sql. Keep the
two in sync — a value added here without a matching ``ALTER TYPE`` will fail at
write time, not at import time.
"""

from __future__ import annotations

from enum import StrEnum


class ArticleStatus(StrEnum):
    """Lifecycle of an article.

    ``validation_failed`` and ``draft_failed`` are terminal *pre-queue* states.
    They are never reachable from ``pending_review`` and never appear in the
    review queue — they exist so a failed draft is visible rather than silent.
    """

    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"
    VALIDATION_FAILED = "validation_failed"
    DRAFT_FAILED = "draft_failed"


class Verdict(StrEnum):
    """Plain-language verdict on whether the evidence supports the claims."""

    SUPPORTED = "supported"
    MIXED = "mixed"
    WEAK = "weak"
    NO_EVIDENCE = "no_evidence"


# Which way one source's reported finding points, for one claim.
#
# Assigned by the APPRAISE stage (``pipeline/steps/appraise.py``) from the
# abstract, and recorded per (source, claim) on ``article_sources.stance``. It
# is a reading of what the paper *found*, which is a different fact from the
# claim it was *retrieved for* — the verdict scale above measures only support,
# and without this nothing in the system could tell a trial that found the
# effect from one that tested for it and found none.
#
# **Relative to the claim as written.** For "creatine damages the kidneys", a
# trial that found no kidney harm is ``NO_EFFECT``: the claim was tested and not
# borne out. It is not ``SUPPORTS`` because the result was reassuring.
#
# ``UNCLEAR`` is a real answer (the abstract states no result for this outcome);
# a source the model never labelled has **no** stance at all — ``NULL`` in the
# column — and must never be read as any of these.
#
# **No docstring, on purpose.** This enum is part of ``AppraisalOutput``, the
# appraisal model's generation grammar, and Pydantic would put a docstring there
# as the enum's description — the same trap as ``research.contracts.Category``.
class Stance(StrEnum):
    SUPPORTS = "supports"
    NO_EFFECT = "no_effect"
    CONTRADICTS = "contradicts"
    UNCLEAR = "unclear"
    OFF_TOPIC = "off_topic"


class StudyType(StrEnum):
    """Evidence-quality hierarchy, weakest to strongest.

    Declaration order is the ranking and is load-bearing: ``EVIDENCE_RANK``
    below, the verdict cap, and the Postgres enum ordering all depend on it.

    ``UNKNOWN`` deliberately sits at the bottom. When a provider gives us no
    usable publication type, uncertainty must *cap* confidence rather than
    inflate it — an unclassifiable source is treated as the weakest evidence,
    not given the benefit of the doubt.
    """

    UNKNOWN = "unknown"
    IN_VITRO = "in_vitro"
    ANIMAL = "animal"
    CASE_REPORT = "case_report"
    #: A literature review with no stated search or inclusion method — what
    #: PubMed tags plainly as "Review". Distinct from UNKNOWN on purpose: we
    #: *did* identify it, and it is weak secondary evidence rather than an
    #: unreadable record. Sits below OBSERVATIONAL because it contributes no
    #: primary data and has no protection against selection bias, and above
    #: CASE_REPORT because it surveys a literature rather than one patient.
    #: Measured need: 16 of 92 cached sources landed here (see README).
    NARRATIVE_REVIEW = "narrative_review"
    OBSERVATIONAL = "observational"
    RCT = "rct"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"


#: Numeric rank for comparisons. Higher is stronger evidence.
EVIDENCE_RANK: dict[StudyType, int] = {
    study_type: rank for rank, study_type in enumerate(StudyType)
}


class Subject(StrEnum):
    """The editorial category an article is filed under.

    The one chromatic axis in the product: colour says *what area* an article
    belongs to, never how it scored (see the frontend's ``subject.ts``). It is
    also the feed's browse axis — the drawer narrows the feed to one category,
    and a reader's interests lift categories to the top. Set by a reviewer and
    deliberately not inferred — ``product`` is free text, and a guessed
    category would put a confident colour on an unchecked classification.

    ``OTHER`` is a reviewer's answer ("none of these fit") and is distinct from
    ``NULL``, which means nobody has classified the article yet. Nullable
    everywhere it appears. An unclassified article renders in ink, which is the
    design's resting state rather than a broken one.

    These are areas, not kinds of object, so they do not choose the generated
    picture's props — ``imagery/prompt.py`` has its own ``Motif`` for that and
    maps only the categories that settle it.
    """

    FITNESS = "fitness"
    NUTRITION = "nutrition"
    SUPPLEMENTS = "supplements"
    SLEEP_RECOVERY = "sleep_recovery"
    LIFESTYLE = "lifestyle"
    PREVENTIVE_HEALTH = "preventive_health"
    GENERAL_HEALTH = "general_health"
    WELLNESS = "wellness"
    OTHER = "other"


class ImageFrame(StrEnum):
    """Which of an article's two generated pictures.

    They are two frames of one still life at two aspect ratios, and they live
    in two different places: ``LEAD`` is an ordinary image node inside the
    document, ``COVER`` exists only on ``articles.generated_imagery`` and
    reaches the reader through the feed tile.

    Named as a pair because they are drawn as one by default and can be redrawn
    one at a time — a reviewer who likes the article's picture and not the tile
    should not have to pay for both to fix one.
    """

    LEAD = "lead"
    COVER = "cover"


class ContactKind(StrEnum):
    """What a "Let us know" submission is asking for."""

    FACT_CHECK = "fact_check"
    TOPIC = "topic"
    OTHER = "other"


class UserRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunOrigin(StrEnum):
    """Who asked for a pipeline run.

    ``CONSOLE`` is a reviewer typing a topic; ``DISCOVERY`` is a reviewer
    promoting a trend the scan proposed. Both are human decisions — the
    distinction is what the topic was derived from, not whether anyone chose it.
    It exists so "what has trend discovery cost us" is one ``WHERE`` against the
    token ledger.
    """

    CONSOLE = "console"
    DISCOVERY = "discovery"
    #: A reviewer promoting a topic the research agent proposed (research_runs).
    RESEARCH = "research"
    #: A reviewer promoting a social-listening topic (social_candidates).
    SOCIAL = "social"


class ResearchRunMode(StrEnum):
    """What triggered a research run. Metadata only — the graph never branches on it."""

    WEEKLY = "weekly"
    MANUAL = "manual"


class ResearchRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchCandidateStatus(StrEnum):
    """Where one topic got to in a research run, and what a reviewer did with it.

    ``SELECTED`` is the agent's proposal; ``SHORTLISTED`` got deep research but
    lost on score; ``DISCARDED`` carries a reason. Only a reviewer moves a row
    to ``PROMOTED`` or ``DISMISSED``.
    """

    CANDIDATE = "candidate"
    DISCARDED = "discarded"
    SHORTLISTED = "shortlisted"
    SELECTED = "selected"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"


class SourceApi(StrEnum):
    """Which scholarly API a source came from.

    Google Scholar is absent by design — no scraping. SerpApi is reserved as a
    later fallback and is not part of the MVP.
    """

    PUBMED = "pubmed"
    EUROPE_PMC = "europe_pmc"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"


class DiscoveryScanStatus(StrEnum):
    """Where one run of ``scripts/scan_trends.py`` got to."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DiscoveryScanMode(StrEnum):
    """Whether a scan proposes candidates or only builds the baseline.

    ``BOOTSTRAP`` walks months of history to fill ``discovery_observations`` and
    emits nothing: a scan with no baseline can only rank by raw volume, which
    proposes vitamin D and creatine forever.
    """

    SCAN = "scan"
    BOOTSTRAP = "bootstrap"


class DiscoveryDescriptorKind(StrEnum):
    """What a MeSH descriptor is to us.

    ``STOPLISTED`` is a decision, not an absence — it records that we saw the
    descriptor and judged it to carry no signal (check tags, method terms, and
    the seed anchors every query matches by construction).
    """

    SUBSTANCE = "substance"
    OUTCOME = "outcome"
    STOPLISTED = "stoplisted"


class DiscoveryCandidateStatus(StrEnum):
    """Lifecycle of a proposed topic.

    ``EXPIRED`` exists so ``PROPOSED`` means "live now": a candidate a later scan
    drops below the cut stops being offered without being deleted, because the
    trail of what was proposed is what makes the scorer tunable.
    """

    PROPOSED = "proposed"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"
    EXPIRED = "expired"
