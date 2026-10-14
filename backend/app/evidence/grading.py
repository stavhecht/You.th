"""Evidence-quality grading, and the verdict ceiling it implies.

Hierarchy (weakest → strongest):
    in-vitro / animal < observational < RCT < systematic review / meta-analysis

Two jobs:

1. **Classify** a paper into a ``StudyType`` from whatever metadata the
   provider gave us. Best-effort, and biased toward under-claiming.
2. **Cap** verdict confidence by the strongest evidence actually cited. This is
   invariant #3 and it is not advisory — a verdict above its cap is a
   validation failure that keeps the draft out of the review queue.

The small pure functions here are implemented rather than stubbed: they *are*
the specification of the invariant, and expressing them as prose in a docstring
would leave the most important rule in the system as the least precise thing in
it.
"""

from __future__ import annotations

import re

from app.domain.enums import EVIDENCE_RANK, Stance, StudyType, Verdict

#: Verdict ceiling by the strongest study type among cited sources.
#:
#: Reading: an article whose best source is an observational study may be
#: 'mixed' at most — never 'supported' — because observational data cannot
#: establish cause and effect. An article resting on cell-culture or animal
#: work may be 'weak' at most: the research has not been done in people.
#:
#: UNKNOWN maps to WEAK for the same reason UNKNOWN sorts lowest in the
#: hierarchy — uncertainty about study quality must reduce confidence, never
#: license it.
VERDICT_CEILING: dict[StudyType, Verdict] = {
    StudyType.META_ANALYSIS: Verdict.SUPPORTED,
    StudyType.SYSTEMATIC_REVIEW: Verdict.SUPPORTED,
    StudyType.RCT: Verdict.SUPPORTED,
    StudyType.OBSERVATIONAL: Verdict.MIXED,
    #: A narrative review restates other people's findings without a stated
    #: method. It can be a useful pointer and is never sufficient support.
    StudyType.NARRATIVE_REVIEW: Verdict.WEAK,
    StudyType.CASE_REPORT: Verdict.WEAK,
    StudyType.ANIMAL: Verdict.WEAK,
    StudyType.IN_VITRO: Verdict.WEAK,
    StudyType.UNKNOWN: Verdict.WEAK,
}

#: Confidence ordering of verdicts, weakest to strongest claim about evidence.
#: NO_EVIDENCE is the *least* confident assertion of support and is therefore
#: always permissible — an article may always report finding nothing.
VERDICT_STRENGTH: dict[Verdict, int] = {
    Verdict.NO_EVIDENCE: 0,
    Verdict.WEAK: 1,
    Verdict.MIXED: 2,
    Verdict.SUPPORTED: 3,
}

#: PubMed publication types → our hierarchy. Matched case-insensitively.
PUBLICATION_TYPE_MAP: dict[str, StudyType] = {
    "meta-analysis": StudyType.META_ANALYSIS,
    "systematic review": StudyType.SYSTEMATIC_REVIEW,
    "randomized controlled trial": StudyType.RCT,
    "controlled clinical trial": StudyType.RCT,
    "clinical trial, phase iii": StudyType.RCT,
    "clinical trial, phase ii": StudyType.RCT,
    # PubMed tags a randomised trial with BOTH "Randomized Controlled Trial"
    # and "Clinical Trial", and the map takes the strongest match — so a paper
    # carrying *only* this one is an interventional study that was not
    # randomised. Stronger than observational in design, far weaker than an
    # RCT in what it can establish; graded down per the module's stated bias
    # toward under-claiming. Measured: one such paper was scoring UNKNOWN.
    "clinical trial": StudyType.OBSERVATIONAL,
    "observational study": StudyType.OBSERVATIONAL,
    "cohort studies": StudyType.OBSERVATIONAL,
    "case-control studies": StudyType.OBSERVATIONAL,
    "cross-sectional studies": StudyType.OBSERVATIONAL,
    "case reports": StudyType.CASE_REPORT,
}

#: "Review" is a supertype, not a verdict — it says a paper reviews a
#: literature without saying by what method. Treating it as a definite match
#: for ``NARRATIVE_REVIEW`` was tried and reverted: PubMed's indexing lags, and
#: several genuine systematic reviews in the cache carry only this tag while
#: their *titles* say "A Systematic Review". Mapping it suppressed the text
#: fallback and demoted them — the same failure as the protocol bug, mirrored.
#:
#: So it is a **floor**: narrative review unless the text says otherwise, and
#: the text may only raise it within the review family below. Never to RCT — a
#: narrative review's abstract discusses randomised trials at length, which is
#: precisely how this went wrong the first time.
PROVISIONAL_REVIEW_TYPES: frozenset[str] = frozenset({"review"})

#: The only upgrades a provisional review may receive from title/abstract text.
REVIEW_FAMILY: frozenset[StudyType] = frozenset(
    {StudyType.SYSTEMATIC_REVIEW, StudyType.META_ANALYSIS}
)

#: Publication types that say something real about a paper without naming a
#: study design — and that must therefore **suppress the text fallback** rather
#: than fall through to it.
#:
#: This is the fix for the worst classification bug the first live runs
#: exposed. ``classify_study_type`` used to treat "informative but unmapped"
#: exactly like "no metadata at all", so a paper tagged ``Clinical Trial
#: Protocol`` reached the abstract heuristics — and a protocol *describes the
#: randomised trial it intends to run*, in those words. Three of them scored
#: RCT, whose ceiling is ``supported``, on the strength of a study that has not
#: reported a single result.
#:
#: The same mechanism promoted a plainly-tagged ``Review`` to
#: ``systematic_review``, because a review discusses the randomised trials it
#: surveys. One such paper was cited in a real draft and set its evidence
#: grade. The docstring on ``classify_study_type`` promises to prefer the
#: weaker classification when torn; without this set it did the reverse.
NEGATIVE_PUBLICATION_TYPES: frozenset[str] = frozenset(
    {
        "clinical trial protocol",
        "editorial",
        "comment",
        "letter",
        "news",
        "published erratum",
        "retraction of publication",
        "retracted publication",
    }
)

#: A review that names itself narrative stays narrative, whatever it surveys.
#: "A Narrative Review of meta-analyses" is still a narrative review.
_NARRATIVE_TITLE_RE = re.compile(
    r"\bnarrative\b[^.:]{0,30}\breview\b|(?<!systematic )\bliterature review\b", re.I
)

#: Abstract prose in which a review describes *its own* method, as opposed to
#: the literature it surveys. Measured 2026-10-13: three narrative reviews in
#: one article graded ``meta_analysis`` because their abstracts said "evidence
#: from randomized trials and meta-analyses indicates…" — 25 papers in the cache
#: whose titles say narrative or literature review were graded above it. A
#: review's abstract mentions meta-analyses for the same reason it mentions
#: trials: they are what it reviews.
_SELF_DESCRIBED_REVIEW_RE = re.compile(
    r"\b(this|the present|the current|our)\s+(systematic review|meta-?analys[ie]s)\b"
    r"|\bwe\s+(conducted|performed|undertook|carried out)\s+an?\s+[^.]{0,40}?"
    r"(systematic review|meta-?analys[ie]s)\b"
    r"|\bsystematic(ally)?\s+(literature\s+)?search(ed)?\b"
    r"|\bPRISMA\b",
    re.I,
)

#: Title/abstract fallbacks, checked only when publication type is unusable.
#: Ordered strongest-first; first match wins.
TEXT_PATTERNS: list[tuple[re.Pattern[str], StudyType]] = [
    (re.compile(r"\bmeta-?analys(is|es)\b", re.I), StudyType.META_ANALYSIS),
    (re.compile(r"\bsystematic review\b", re.I), StudyType.SYSTEMATIC_REVIEW),
    (
        re.compile(r"\b(randomi[sz]ed|double-?blind|placebo-?controlled)\b", re.I),
        StudyType.RCT,
    ),
    (re.compile(r"\b(cohort|case-?control|cross-?sectional)\b", re.I), StudyType.OBSERVATIONAL),
    (re.compile(r"\b(in vitro|cell culture|cell line)\b", re.I), StudyType.IN_VITRO),
    (re.compile(r"\b(mice|mouse|rats?|murine|rodent|zebrafish)\b", re.I), StudyType.ANIMAL),
]


#: A title that announces the paper is a plan rather than a result. Providers
#: other than PubMed supply no publication types at all, so the tag check alone
#: would let protocols in through OpenAlex — and "study protocol for a
#: randomised controlled trial" is close to a house style in trial reporting.
_PROTOCOL_TITLE_RE = re.compile(r"\b(study|trial)\s+protocol\b|\bprotocol\s+for\s+a\b", re.I)


def is_protocol(raw_study_type: str | None, title: str = "") -> bool:
    """True for a trial protocol — a design document with no findings.

    Excluded from retrieval entirely rather than graded down. A protocol
    reports nothing, so it can never legitimately support or refute a claim;
    admitting it can only occupy a top-k slot a real study would have taken,
    and put a "randomised, double-blind, placebo-controlled" abstract in front
    of a model that is being asked what the evidence says.

    Three were cached and scored RCT before this existed.
    """
    if _PROTOCOL_TITLE_RE.search(title):
        return True
    if not raw_study_type:
        return False
    return any(
        part.strip().lower() == "clinical trial protocol"
        for part in raw_study_type.split(";")
    )


#: Publication types that mark a paper as withdrawn by the literature itself.
#:
#: ``retracted publication`` is the paper; ``retraction of publication`` is the
#: notice announcing it. Both are refused, for different reasons — the first is
#: a finding that no longer stands, the second is an announcement with no
#: findings at all, and a model handed either would cite it as evidence.
#:
#: Note these are *already* in ``NEGATIVE_PUBLICATION_TYPES`` above, which caps
#: such a paper at ``UNKNOWN``. That was never enough: a capped grade limits how
#: confident the article may sound, and does nothing about the citation itself.
#: The article still says "one trial found X [S3]" with S3 pointing at a study
#: the journal has withdrawn. Grade caps govern confidence; this governs
#: admission, and they are different questions.
RETRACTION_PUBLICATION_TYPES: frozenset[str] = frozenset(
    {"retracted publication", "retraction of publication"}
)

#: An unresolved question, not a withdrawal. Deliberately **not** refused: an
#: expression of concern says the journal is investigating, and the paper may
#: well be exonerated. It stays admissible and is recorded, so a reviewer can
#: see it — which is more than the grade cap does for a retraction today.
CONCERN_PUBLICATION_TYPES: frozenset[str] = frozenset({"expression of concern"})


def is_retracted(raw_study_type: str | None) -> bool:
    """True for a paper the literature has withdrawn.

    Refused in ``RetrieveStage`` before dedup and before the cache, the same
    way ``is_protocol`` is, and by the stronger version of the same argument. A
    protocol is merely uninformative — it reports nothing, so it cannot support
    a claim. A retracted paper *does* report something, and the report is one
    the record has repudiated; leaving it admissible means the strongest
    possible wrong answer stays reachable.

    Takes no title, unlike ``is_protocol``. There is no reliable prose tell for
    a retraction — journals mark them in metadata, not in the abstract, and
    guessing from a title containing the word "retracted" would catch the
    notices while missing every retracted paper, which is backwards. Providers
    with no publication types therefore contribute nothing here; that gap is
    what ``scripts/check_retractions.py`` exists to close.
    """
    if not raw_study_type:
        return False
    return any(
        part.strip().lower() in RETRACTION_PUBLICATION_TYPES
        for part in raw_study_type.split(";")
    )


#: Distinct sources required at a `supported`-ceiling grade before a claim may
#: actually reach `supported`.
#:
#: Two, not one, and the reason is specific to this system rather than to
#: evidence convention. Classification here is heuristic — publication types
#: where they exist, abstract prose where they do not — and it has already been
#: measurably wrong in both directions on real data (DESIGN.md §4). Requiring a
#: second independent source means one misclassified paper cannot license a
#: confident verdict by itself. The clinical convention that a single good
#: meta-analysis suffices assumes a grade you can trust; ours is a guess.
#:
#: Sources are counted by ``source_id`` after cross-provider dedup, so one
#: paper arriving from three APIs is one source. What this cannot see: a
#: meta-analysis and a trial it already includes are not independent, and
#: detecting that needs reference lists the abstract-only MVP does not fetch.
QUORUM_FOR_SUPPORTED = 2


def max_verdict_for_sources(study_types: list[StudyType]) -> Verdict:
    """The strongest verdict one claim's cited evidence permits.

    ``max_verdict_for_grade`` asks only how good the *best* source is, which
    lets a lone study carry the strongest verdict the system can express. This
    additionally asks how *many* there are, and only at the top: below
    ``supported`` the ceilings already say "provisional", so a quorum there
    would demote thin evidence that is being reported as thin.

    Falling short drops the ceiling exactly one step, to ``mixed`` — "there is
    real evidence and it is not settled", which is the honest description of a
    single trial. Dropping to ``weak`` would understate a good RCT as badly as
    ``supported`` overstates it.
    """
    ceiling = VERDICT_CEILING[best_grade(study_types)]
    if ceiling is not Verdict.SUPPORTED:
        return ceiling

    strong = sum(
        1 for study_type in study_types if VERDICT_CEILING[study_type] is Verdict.SUPPORTED
    )
    return ceiling if strong >= QUORUM_FOR_SUPPORTED else Verdict.MIXED


def max_verdict_for_claims(by_claim: dict[str, list[StudyType]]) -> Verdict:
    """The article's ceiling: the weakest of its claims'.

    A verdict is one field covering every claim the product makes, so it can
    only be as strong as its thinnest support. Counted article-wide instead,
    one well-evidenced claim lends its sources to the others — two claims each
    backed by a single trial would satisfy a quorum of two between them, while
    neither is corroborated, and the finished article shows no sign of it.

    A claim with **no cited source at all** is the weakest case there is, and
    caps the article at ``weak``. This is the aggressive reading and it is
    deliberate: asserting `supported` about a product while one of its claims
    turned up nothing is the overstatement this system exists to prevent.
    ``no_evidence`` stays permissible everywhere — an article may always report
    finding nothing.
    """
    if not by_claim:
        return VERDICT_CEILING[StudyType.UNKNOWN]
    return min(
        (max_verdict_for_sources(types) for types in by_claim.values()),
        key=lambda verdict: VERDICT_STRENGTH[verdict],
    )


#: Stances that count against a claim: it was tested and not borne out.
AGAINST_STANCES: frozenset[Stance] = frozenset({Stance.NO_EFFECT, Stance.CONTRADICTS})


def strong_by_direction(cited: list[tuple[StudyType, Stance | None]]) -> dict[str, int]:
    """Which way the supported-tier sources cited for one claim point.

    Counts only the study types whose ceiling is ``supported`` — the set the
    quorum counts — because a confident verdict in either direction could only
    rest on those. ``unclear`` and ``off_topic`` count in neither direction;
    ``unlabelled`` is sources APPRAISE never labelled, kept apart so "no
    conflict" cannot be read off a claim nobody looked at.

    Recorded on every report and enforced nowhere yet: the verdict cap above is
    still stance-blind, and this only drives a reviewer warning while the
    labels' accuracy is measured (CLAUDE.md, APPRAISE).
    """
    tally = {"for": 0, "against": 0, "unlabelled": 0}
    for study_type, stance in cited:
        if VERDICT_CEILING[study_type] is not Verdict.SUPPORTED:
            continue
        if stance is None:
            tally["unlabelled"] += 1
        elif stance is Stance.SUPPORTS:
            tally["for"] += 1
        elif stance in AGAINST_STANCES:
            tally["against"] += 1
    return tally


def max_verdict_for_grade(best_grade: StudyType) -> Verdict:
    """The strongest verdict this evidence permits."""
    return VERDICT_CEILING[best_grade]


def verdict_exceeds_grade(verdict: Verdict, best_grade: StudyType) -> bool:
    """True when a verdict claims more confidence than the evidence supports.

    This is the check behind invariant #3. A True here fails the draft.
    """
    return VERDICT_STRENGTH[verdict] > VERDICT_STRENGTH[max_verdict_for_grade(best_grade)]


def best_grade(study_types: list[StudyType]) -> StudyType:
    """The strongest study type in a set of cited sources.

    Empty input yields UNKNOWN, which caps the verdict at 'weak' — the correct
    posture for an article that cited nothing.
    """
    if not study_types:
        return StudyType.UNKNOWN
    return max(study_types, key=lambda s: EVIDENCE_RANK[s])


def classify_study_type(
    publication_types: list[str],
    title: str = "",
    abstract: str = "",
) -> StudyType:
    """Best-effort classification from provider metadata.

    Publication types are authoritative when present and recognised; title and
    abstract text are a fallback for providers that don't supply them (OpenAlex
    and Semantic Scholar, mostly).

    Deliberately conservative. Returning UNKNOWN caps the verdict at 'weak',
    which is a safe failure; guessing RCT from an ambiguous abstract unlocks
    'supported' on evidence that may not warrant it. When torn, prefer the
    weaker classification.

    One asymmetry worth noting: within *publication types* the strongest match
    wins (PubMed tags a paper "Meta-Analysis" and "Review" both, and the former
    is the real signal), but the *text* fallback takes the first match in a
    strongest-first list and stops. Abstract prose is far less reliable than a
    curated tag, so it gets one shot rather than a survey.

    **The text fallback runs only when the tags are genuinely uninformative.**
    A type in ``NEGATIVE_PUBLICATION_TYPES`` stops it dead, because prose
    heuristics are actively misleading for exactly those papers: a trial
    protocol and a narrative review both discuss randomised trials at length
    without being one. Treating "informative but unmapped" as "no metadata"
    was how three protocols scored RCT against real PubMed data.
    """
    normalised_types = {
        normalised
        for publication_type in publication_types
        if (normalised := publication_type.strip().lower())
    }

    # Checked BEFORE the map, unlike every other negative type below, and the
    # exception is the point. The rule down there — a positive identification
    # outranks a negative one — holds because "Comment" alongside "Randomized
    # Controlled Trial" describes a *different aspect* of a paper that really is
    # a trial. A retraction is not another aspect. It says the paper does not
    # stand, and it says so about the trial tag itself.
    #
    # Leaving this below the map meant `Randomized Controlled Trial; Retracted
    # Publication` graded as `rct`, ceiling `supported` — and a retracted trial
    # nearly always carries its own design tag, so that was the common case, not
    # the edge. The grade cap this set was supposed to provide did not exist for
    # any paper it actually mattered for. Caught by a test asserting the cap,
    # which found the opposite.
    if normalised_types & RETRACTION_PUBLICATION_TYPES:
        return StudyType.UNKNOWN

    matches = [
        PUBLICATION_TYPE_MAP[normalised]
        for normalised in normalised_types
        if normalised in PUBLICATION_TYPE_MAP
    ]
    if matches:
        return max(matches, key=lambda study: EVIDENCE_RANK[study])

    # Checked *after* the map: a real RCT can carry "Comment" alongside its own
    # type. A positive identification outranks a negative one — except for the
    # retraction case handled above.
    if normalised_types & NEGATIVE_PUBLICATION_TYPES:
        return StudyType.UNKNOWN

    haystack = f"{title} {abstract}"
    guess = next(
        (study for pattern, study in TEXT_PATTERNS if pattern.search(haystack)), None
    )

    if normalised_types & PROVISIONAL_REVIEW_TYPES:
        # Tagged a review and nothing more specific. The text may sharpen that
        # into a systematic review or meta-analysis; anything else it thinks it
        # sees is the surveyed literature, not this paper. And even a review-
        # family word counts only where it describes this paper — in its title,
        # or in an abstract sentence about its own method.
        if _NARRATIVE_TITLE_RE.search(title):
            return StudyType.NARRATIVE_REVIEW
        if (
            guess is not None
            and guess in REVIEW_FAMILY
            and _SELF_DESCRIBED_REVIEW_RE.search(abstract)
        ):
            return guess
        title_guess = next(
            (study for pattern, study in TEXT_PATTERNS if pattern.search(title)), None
        )
        if title_guess in REVIEW_FAMILY:
            return title_guess
        return StudyType.NARRATIVE_REVIEW

    if guess is not None:
        return guess

    return StudyType.UNKNOWN


def is_weak_evidence(study_type: StudyType) -> bool:
    """Whether the review UI should flag this source inline.

    Drives the 'this verdict rests on weak study types' warning in the sources
    panel — the reviewer's cue to look harder.
    """
    return EVIDENCE_RANK[study_type] < EVIDENCE_RANK[StudyType.OBSERVATIONAL]
