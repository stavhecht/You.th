"""Citation validation — the gate between generation and the review queue.

This module is where invariant #2 stops being a prompt instruction and becomes
a fact. It runs after every synthesis call, before persistence, and it decides
whether a draft is allowed to exist as ``pending_review``.

The design rule it embodies: **the thing that generates must not be the thing
that approves.** Nothing here consults the model, and nothing here trusts the
model's own account of what it cited. Checks run against (a) the exact handle
set that was rendered into the prompt and (b) the database. Neither is
influenceable by generation.

A draft that fails is stored as ``validation_failed`` with a structured report
and never enters the queue. It is not silently dropped — a persistent
validation failure is a prompt bug, and the console surfaces these on their own
tab so the bug is visible rather than absent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contracts import (
    PromptSource,
    SynthesisInput,
    SynthesisOutput,
    ValidationFailure,
    ValidationReport,
    extract_handles,
)
from app.domain.enums import Stance, StudyType, Verdict
from app.domain.models import Source
from app.evidence import numbers, readability
from app.evidence.grading import (
    AGAINST_STANCES,
    QUORUM_FOR_SUPPORTED,
    VERDICT_STRENGTH,
    best_grade,
    max_verdict_for_claims,
    max_verdict_for_sources,
    strong_by_direction,
)
from app.evidence.overlap import content_terms, lexical_support
from app.services.tiptap import MalformedBodyError, body_text_to_doc

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A cited handle successfully mapped to a real, identifiable paper."""

    handle: str
    source_id: str
    pmid: str | None
    doi: str | None
    study_type: StudyType


def check_handles_were_provided(
    output: SynthesisOutput, payload: SynthesisInput
) -> list[ValidationFailure]:
    """Check 1 — every emitted handle was in the prompt.

    The core anti-hallucination check. Compares against ``payload.handle_set``:
    the handles rendered into *this* prompt, not the set of sources in the
    database. A model that invents ``S9`` when only S1–S6 were provided fails
    here, even if some unrelated article once had a valid S9.
    """
    provided = payload.handle_set
    emitted = output.all_cited_handles()
    invented = sorted(emitted - provided, key=_handle_sort_key)

    return [
        ValidationFailure(
            code="hallucinated_handle",
            message=(
                f"cited {handle}, which was not among the sources provided "
                f"({_format_handles(provided)})"
            ),
            detail={"handle": handle, "provided": sorted(provided, key=_handle_sort_key)},
        )
        for handle in invented
    ]


async def check_sources_resolve(
    session: AsyncSession, payload: SynthesisInput, cited: set[str]
) -> tuple[list[ResolvedSource], list[ValidationFailure]]:
    """Check 2 — every cited source maps to a row with a real PMID or DOI.

    Handle → source_id comes from the prompt payload; the row is then loaded
    and its identifiers confirmed. A source row cannot exist without at least
    one identifier (there is a CHECK constraint), so this is belt-and-braces
    against a row deleted between retrieval and validation — but it is also
    what makes "4/4 citations resolve" a statement about reality rather than
    about the prompt.

    Handles not present in the payload are skipped here; check 1 already
    reported them, and reporting the same defect twice makes the report harder
    to read, not more convincing.
    """
    by_handle = {source.handle: source for source in payload.sources}
    wanted = {handle: by_handle[handle] for handle in cited if handle in by_handle}
    if not wanted:
        return [], []

    result = await session.execute(
        select(Source).where(Source.id.in_([s.source_id for s in wanted.values()]))
    )
    rows = {row.id: row for row in result.scalars()}

    resolved: list[ResolvedSource] = []
    failures: list[ValidationFailure] = []

    for handle, prompt_source in sorted(wanted.items(), key=lambda kv: _handle_sort_key(kv[0])):
        row = rows.get(prompt_source.source_id)
        if row is None:
            failures.append(
                ValidationFailure(
                    code="unresolvable_source",
                    message=f"{handle} refers to a source that no longer exists",
                    detail={"handle": handle, "source_id": prompt_source.source_id},
                )
            )
            continue
        if not row.pmid and not row.doi:
            failures.append(
                ValidationFailure(
                    code="unresolvable_source",
                    message=f"{handle} has neither a PMID nor a DOI",
                    detail={"handle": handle, "source_id": prompt_source.source_id},
                )
            )
            continue
        resolved.append(
            ResolvedSource(
                handle=handle,
                source_id=row.id,
                pmid=row.pmid,
                doi=row.doi,
                study_type=StudyType(row.study_type),
            )
        )

    return resolved, failures


def check_beats_are_cited(output: SynthesisOutput) -> list[ValidationFailure]:
    """Check 3 — the evidence beat and every section carry a citation.

    An uncited factual sentence is exactly the failure this whole system exists
    to prevent, so beat 2 (what the research shows) must cite something, and so
    must each section.

    **Sections are covered for the same reason beat 2 is, at a scale that makes
    it matter more.** Before sections existed the widest gap this check left was
    three interpretive sentences. A section is up to eight, and there may be
    five of them — so exempting them would quietly turn a three-sentence
    allowance into most of the article. A section exists to say what the
    research shows; if it cannot name a source, it is the kind of prose the
    grounding invariant is for.

    Two deliberate exemptions, both unchanged:

    * verdict ``no_evidence`` — an article correctly reporting that nothing was
      found has nothing to cite, and demanding a citation would push the model
      toward citing something irrelevant to satisfy the check.
    * beats 1 and 3 — beat 1 restates the product's own marketing claim and
      beat 3 is an interpretive bottom line; requiring citations there
      encourages decorative citation, which is worse than none.
    """
    if output.verdict is Verdict.NO_EVIDENCE:
        return []

    from app.domain.contracts import extract_handles

    failures: list[ValidationFailure] = []

    if not extract_handles(output.body.beat_2_evidence):
        failures.append(
            ValidationFailure(
                code="uncited_beat",
                message="the evidence beat states findings without citing any source",
                detail={"beat": "beat_2_evidence"},
            )
        )

    for index, section in enumerate(output.body.sections, start=1):
        # The heading is excluded on purpose: it is a label, and a citation
        # marker in one would render as a chip in a title. The prose under it
        # is what makes a statement.
        if not extract_handles(section.body):
            failures.append(
                ValidationFailure(
                    code="uncited_section",
                    message=(
                        f"section {index} ({section.heading!r}) states findings "
                        "without citing any source"
                    ),
                    detail={"section": index, "heading": section.heading},
                )
            )

    return failures


def check_verdict_within_grade(
    output: SynthesisOutput, resolved: list[ResolvedSource], payload: SynthesisInput
) -> tuple[StudyType, Verdict, list[ValidationFailure]]:
    """Check 4 — invariant #3: the verdict does not exceed its evidence.

    Computed over the **cited** sources only. Sources the model retrieved but
    ignored cannot raise its ceiling — otherwise a strong review sitting unused
    in the prompt would license a confident verdict the article never actually
    supported.

    Two questions, not one. *How good* is the evidence, which is the study-type
    ceiling; and *how much of it is there*, which is the quorum. A lone trial
    answers the first perfectly well and the second not at all, and the first
    version of this check only asked the first — so one cited study could carry
    ``supported``, the strongest thing this system can say. Both are evaluated
    **per claim**, and the article inherits its weakest claim's ceiling.

    **The ceiling is returned whether or not it was exceeded**, because the
    check is one-sided: it can only fail a verdict that is too strong. A verdict
    below its ceiling is either an honest cautious call or a model declining to
    commit, and nothing here can tell them apart — so the number is recorded and
    the judgement left to the reviewer, who can see both.

    The returned ``StudyType`` is still the best grade cited anywhere in the
    article: it is stored on the row as a description of the evidence and shown
    in the console, and it stays a fact about the sources rather than becoming
    a verdict-shaped judgement. The quorum shows up in the *ceiling*, which is
    why the failure message reports both.

    This is what makes "honest about evidence strength" structural. A
    ``supported`` verdict resting on two cell-culture studies fails here and the
    draft never reaches a human. It is not a style note and it is not
    overridable in the editor: a reviewer can rewrite the article, but cannot
    approve a draft that was never allowed into the queue.
    """
    grade = best_grade([source.study_type for source in resolved])

    cited = {source.source_id for source in resolved}
    types_by_claim: dict[str, list[StudyType]] = {
        claim: [] for claim in payload.target_claims
    }
    for prompt_source in payload.sources:
        if prompt_source.source_id not in cited:
            continue
        for claim in prompt_source.claims:
            # A claim absent from target_claims cannot happen via the pipeline
            # (both come from the same extraction), and if it ever did, adding
            # it here would let an unasked-for claim carry the article.
            if claim in types_by_claim:
                types_by_claim[claim].append(prompt_source.study_type)

    ceiling = max_verdict_for_claims(types_by_claim)

    if VERDICT_STRENGTH[output.verdict] > VERDICT_STRENGTH[ceiling]:
        claimed = VERDICT_STRENGTH[output.verdict]
        thin = sorted(
            claim
            for claim, types in types_by_claim.items()
            if VERDICT_STRENGTH[max_verdict_for_sources(types)] < claimed
        )
        return grade, ceiling, [
            ValidationFailure(
                code="verdict_exceeds_grade",
                message=(
                    f"verdict '{output.verdict}' exceeds what the cited evidence "
                    f"supports (ceiling: {ceiling}; best study type: {grade}; "
                    f"{QUORUM_FOR_SUPPORTED} sources at a supported-tier grade are "
                    f"required per claim). Underpowered: "
                    f"{', '.join(repr(claim) for claim in thin)}"
                ),
                detail={
                    "verdict": str(output.verdict),
                    "best_grade": str(grade),
                    "ceiling": str(ceiling),
                    "quorum": QUORUM_FOR_SUPPORTED,
                    "underpowered_claims": thin,
                    "cited_per_claim": {
                        claim: [str(t) for t in types]
                        for claim, types in types_by_claim.items()
                    },
                },
            )
        ]
    return grade, ceiling, []


def check_numbers_are_sourced(
    output: SynthesisOutput, payload: SynthesisInput
) -> list[ValidationFailure]:
    """Check 6 — every number beside a citation is in the sources it cites.

    See ``evidence/numbers.py``. The handle checks above prove a citation
    points at a real, provided source; this proves the sentence's figures came
    from it. A statistic cited to the wrong paper passes every other check.
    """
    failures = []
    for sentence, handles, missing, elsewhere in numbers.unsupported(output, payload.sources):
        cited = ", ".join(handles)
        figures = ", ".join(missing)
        many = len(missing) > 1
        them = "them" if many else "it"
        hint = (
            f"; {'they appear' if many else 'it appears'} in {', '.join(elsewhere)}, "
            f"so cite that source for {them}"
            if elsewhere
            else f"; no source provided contains {them}, so remove {them}"
        )
        failures.append(
            ValidationFailure(
                code="unsourced_number",
                message=(
                    f"the sentence “{sentence[:160]}” gives {figures} and cites {cited}, but "
                    f"{cited} {'do' if len(handles) > 1 else 'does'} not contain "
                    f"{'those numbers' if many else 'that number'}{hint}"
                ),
                detail={
                    "sentence": sentence,
                    "handles": handles,
                    "numbers": missing,
                    "found_in": elsewhere,
                },
            )
        )
    return failures


def check_no_evidence_cites_nothing(output: SynthesisOutput) -> list[ValidationFailure]:
    """Check 7 — a ``no_evidence`` verdict cites no source (once).

    ``no_evidence`` says no study met the criteria. A body citing studies
    contradicts it: either they bear on the claims, and the verdict is at least
    ``weak``, or they do not, and citing them is decoration. Measured
    2026-10-01, drafts did exactly this — a stretching article citing a
    Cochrane review under ``no_evidence`` — and nothing caught it, because
    ``no_evidence`` is exempt from the cited-beat rule and sits below every
    ceiling. The deterministic no-evidence template cites nothing and passes.

    **It blocks only the first draft.** The verdict scale measures support, so
    a claim the studies *refute* ("creatine damages the kidneys") honestly
    reads ``no_evidence`` while citing the trials that found no harm — and
    deterministic code cannot tell that from the stretching case. Measured
    2026-10-02, blocking a rewrite as well rejected eight such articles. So
    the first draft is sent back once with the question; a rewrite that keeps
    the pairing passes with a warning for the reviewer (``final_round``).
    """
    if output.verdict is not Verdict.NO_EVIDENCE:
        return []
    cited = sorted(output.body.cited_handles(), key=_handle_sort_key)
    if not cited:
        return []
    return [
        ValidationFailure(
            code="no_evidence_with_citations",
            message=(
                f"the verdict is no_evidence but the article cites {', '.join(cited)}: if "
                "those sources bear on the claims the verdict is at least weak, and if they "
                "do not, do not cite them"
            ),
            detail={"cited": cited},
        )
    ]


#: The warnings ``check_verdict_against_stance`` raises. Named together so the
#: VALIDATE metrics can say whether a draft disagreed with its appraisal.
STANCE_WARNING_CODES = frozenset(
    {"verdict_against_stance", "refutation_understated", "verdict_on_off_topic_sources"}
)


def check_verdict_against_stance(
    output: SynthesisOutput, resolved: list[ResolvedSource], payload: SynthesisInput
) -> tuple[dict[str, dict[str, int]] | None, list[ValidationFailure]]:
    """Check 8 — does the verdict point the way its appraised sources do?

    **Warnings only, never a failure.** The labels come from a model call
    (APPRAISE), so unlike every other check in this module this one rests on
    something generated — and their accuracy is not measured yet. Until it is,
    they may tell a reviewer where to look and nothing more.

    Three disagreements are worth a reviewer's eye:

    * ``supported`` while, for some claim, the cited trials and reviews that
      found no effect are at least as many as those that found it.
    * ``no_evidence`` or ``weak`` while, for some claim, at least
      ``QUORUM_FOR_SUPPORTED`` cited trials or reviews tested it and found no
      effect, and outnumber those that found one. That is a refuted claim
      written up as an untested one — the readers' gloss for ``no_evidence``
      says no study tested it — and there is no verdict for it yet.
    * Any verdict but ``no_evidence`` while every source cited for some claim
      was appraised ``off_topic``. ``off_topic`` counts in neither direction
      of the tally, so without this the tally reads 0 for, 0 against: a claim
      resting entirely on papers about something else looks like a claim with
      no conflict. Measured 2026-10-02: "Three systematic reviews and
      meta-analyses reported a significant increase in antioxidant intake from
      dietary fiber", ``supported``, cited three cardiovascular meta-analyses,
      all three appraised off-topic, and the report was clean. Every cited
      source must be labelled, so a claim APPRAISE skipped is never flagged.
      (``no_evidence`` citing anything is ``no_evidence_with_citations``'s case.)

    Returns the per-claim tally (see ``grading.strong_by_direction``) for the
    report, or None when nothing in the payload was appraised, which must read
    as "not recorded" rather than as no conflict.
    """
    if not any(source.stances for source in payload.sources):
        return None, []

    cited = {source.source_id for source in resolved}
    pairs: dict[str, list[tuple[StudyType, Stance | None]]] = {
        claim: [] for claim in payload.target_claims
    }
    against: dict[str, list[str]] = {claim: [] for claim in payload.target_claims}
    cited_stances: dict[str, dict[str, Stance | None]] = {
        claim: {} for claim in payload.target_claims
    }
    for source in payload.sources:
        if source.source_id not in cited:
            continue
        for claim in source.claims:
            if claim not in pairs:
                continue
            stance = source.stances.get(claim)
            pairs[claim].append((source.study_type, stance))
            cited_stances[claim][source.handle] = stance
            if stance in AGAINST_STANCES:
                against[claim].append(source.handle)

    tally = {claim: strong_by_direction(cited_pairs) for claim, cited_pairs in pairs.items()}

    warnings: list[ValidationFailure] = []
    if output.verdict is not Verdict.NO_EVIDENCE:
        off_topic = sorted(
            claim
            for claim, stances in cited_stances.items()
            if stances and all(stance is Stance.OFF_TOPIC for stance in stances.values())
        )
        if off_topic:
            warnings.append(
                ValidationFailure(
                    code="verdict_on_off_topic_sources",
                    message=(
                        f"the verdict is {output.verdict}, but "
                        + "; ".join(
                            f"every source cited for “{claim}” "
                            f"({_format_handles(set(cited_stances[claim]))}) was appraised "
                            "as not reporting the claim's outcome"
                            for claim in off_topic
                        )
                        + ". Check whether what the article says those sources found is "
                        "what they studied."
                    ),
                    detail={
                        "claims": off_topic,
                        "cited": {
                            claim: sorted(cited_stances[claim], key=_handle_sort_key)
                            for claim in off_topic
                        },
                    },
                )
            )
    if output.verdict is Verdict.SUPPORTED:
        conflicted = sorted(
            claim
            for claim, counts in tally.items()
            if counts["against"] and counts["against"] >= counts["for"]
        )
        if conflicted:
            warnings.append(
                ValidationFailure(
                    code="verdict_against_stance",
                    message=(
                        "the verdict is supported, but for "
                        + "; ".join(
                            f"“{claim}” {tally[claim]['for']} cited trial(s) or review(s) "
                            f"were appraised as finding the effect and "
                            f"{tally[claim]['against']} as finding none or the opposite "
                            f"({_format_handles(set(against[claim]))})"
                            for claim in conflicted
                        )
                        + ". Check those sources against the verdict."
                    ),
                    detail={"claims": conflicted, "tally": tally},
                )
            )
    elif output.verdict in (Verdict.NO_EVIDENCE, Verdict.WEAK):
        refuted = sorted(
            claim
            for claim, counts in tally.items()
            if counts["against"] >= QUORUM_FOR_SUPPORTED and counts["against"] > counts["for"]
        )
        if refuted:
            warnings.append(
                ValidationFailure(
                    code="refutation_understated",
                    message=(
                        f"the verdict is {output.verdict}, but "
                        + "; ".join(
                            f"for “{claim}” {tally[claim]['against']} cited trial(s) or "
                            f"review(s) were appraised as testing it and finding no effect "
                            f"or the opposite ({_format_handles(set(against[claim]))})"
                            for claim in refuted
                        )
                        + ". The claim may have been tested and not borne out, which "
                        "this verdict does not say."
                    ),
                    detail={"claims": refuted, "tally": tally},
                )
            )
    return tally, warnings


def check_reader_shorthand(output: SynthesisOutput) -> list[ValidationFailure]:
    """Warning — statistical notation a general reader cannot read.

    See ``evidence/readability.py``: a style problem in a correct article, so
    it is shown to the reviewer and never fails or revises a draft.
    """
    terms = readability.statistical_shorthand(output)
    if not terms:
        return []
    return [
        ValidationFailure(
            code="statistical_shorthand",
            message=(
                f"the article uses statistical shorthand readers cannot follow "
                f"({'; '.join(terms[:4])}{'; …' if len(terms) > 4 else ''}): say how big the "
                "effect was in words, write an effect size as \"effect size\", and leave out "
                "confidence intervals and P values"
            ),
            detail={"terms": terms},
        )
    ]


#: Below this share of its content words found in its cited sources, a cited
#: sentence is flagged. Measured 2026-10-24 over 90 judged sentences: the one
#: the judge and a reader agreed was cited to the wrong paper (creatine and
#: muscle soreness, mp_001) scored 0.31; the lowest "supported" one 0.44.
LOW_SOURCE_OVERLAP = 0.35
#: A bracketed aside with no digit: the reader edit's explanation of a term,
#: which no source states. Effect sizes in brackets are kept.
_EXPLANATION = re.compile(r"\s*\([^()\d]{20,}\)")


def check_sentences_share_source_words(
    output: SynthesisOutput, payload: SynthesisInput
) -> list[ValidationFailure]:
    """Warning — a cited sentence that shares few words with what it cites.

    The number check cannot see a finding without a number cited to the wrong
    paper. Word overlap is a weak signal, so it only warns the reviewer.
    """
    by_handle = {source.handle: numbers.source_text(source) for source in payload.sources}
    body = output.body
    blocks = [body.beat_2_evidence, *(section.body for section in body.sections)]
    flagged = []
    for block in blocks:
        for sentence in numbers.sentences(block):
            cited = [by_handle[h] for h in extract_handles(sentence) if h in by_handle]
            if not cited:
                continue
            claim = _EXPLANATION.sub("", numbers.plain_sentence(sentence))
            if lexical_support(claim, " ".join(cited)) < LOW_SOURCE_OVERLAP:
                flagged.append(numbers.plain_sentence(sentence))
    if not flagged:
        return []
    return [
        ValidationFailure(
            code="low_source_overlap",
            message=(
                f"{len(flagged)} cited sentence(s) share few words with the sources they cite; "
                f"check they say what the source found: “{flagged[0][:160]}”"
            ),
            detail={"sentences": flagged},
        )
    ]


#: A cited sentence is called misattributed when its own sources share under
#: ``LOW_SOURCE_OVERLAP`` of its words and another provided source shares at least
#: this much, and this much more. Measured 2026-10-26 over 2,592 recorded
#: sentences: it named the right source for both confirmed misses (wt_007's S13
#: for S12, mp_001's S2 for S14).
MISATTRIBUTION_BETTER_SOURCE = 0.5
MISATTRIBUTION_MARGIN = 0.2
#: Shorter sentences ("These findings need more research") match anything.
MISATTRIBUTION_MIN_TERMS = 6


def check_citations_point_at_best_source(
    output: SynthesisOutput, payload: SynthesisInput
) -> list[ValidationFailure]:
    """A finding cited to one source whose wording is plainly another's.

    The number check is fooled when the wrong source happens to hold the
    number: wt_007 cited a memory study (S13, "21 highlighted words") for the
    opioid trial's 10 + 11 patients (S12). Revisable, so the in-place repair can
    move the marker; ``validate_draft`` makes it a warning in the final round,
    because word overlap is evidence, not proof.
    """
    body = output.body
    return misattributed_sentences(
        [body.beat_2_evidence, *(section.body for section in body.sections)], payload.sources
    )


def misattributed_sentences(
    blocks: list[str], sources: list[PromptSource]
) -> list[ValidationFailure]:
    """``check_citations_point_at_best_source`` over any text; the repair uses it too."""
    by_handle = {source.handle: numbers.source_text(source) for source in sources}
    failures = []
    for block in blocks:
        for sentence in numbers.sentences(block):
            handles = sorted(
                (h for h in extract_handles(sentence) if h in by_handle), key=_handle_sort_key
            )
            claim = _EXPLANATION.sub("", numbers.plain_sentence(sentence))
            if not handles or len(content_terms(claim)) < MISATTRIBUTION_MIN_TERMS:
                continue
            own = lexical_support(claim, " ".join(by_handle[h] for h in handles))
            others = {
                h: lexical_support(claim, text)
                for h, text in by_handle.items()
                if h not in handles
            }
            if own >= LOW_SOURCE_OVERLAP or not others:
                continue
            better = max(others, key=lambda h: others[h])
            if others[better] < max(MISATTRIBUTION_BETTER_SOURCE, own + MISATTRIBUTION_MARGIN):
                continue
            plain = numbers.plain_sentence(sentence)
            cited = ", ".join(handles)
            failures.append(
                ValidationFailure(
                    code="misattributed_citation",
                    message=(
                        f"the sentence “{plain[:160]}” cites {cited}, but its wording matches "
                        f"{better} ({others[better]:.0%}) far better than {cited} ({own:.0%}); "
                        f"cite the source that reports this finding"
                    ),
                    detail={"sentence": plain, "handles": handles, "found_in": [better]},
                )
            )
    return failures


def check_body_parses(output: SynthesisOutput) -> list[ValidationFailure]:
    """Check 5 — the body can be turned into a document.

    PERSIST parses the body again to build ``original_content``, and it once
    was the only place that did. The failure was then added to the report after
    this stage had recorded its metrics, so a run's ``validate`` metrics read
    ``passed: true`` for an article stored as ``validation_failed``.

    The raw text of the offending block goes into ``detail``: a draft that does
    not parse is stored with an empty document, so this is the only record of
    what the model wrote.
    """
    try:
        body_text_to_doc(output.body)
    except MalformedBodyError as exc:
        return [
            ValidationFailure(
                code="malformed_body",
                message=str(exc),
                detail={"where": exc.where, "text": exc.text},
            )
        ]
    return []


async def validate_draft(
    session: AsyncSession,
    output: SynthesisOutput,
    payload: SynthesisInput,
    *,
    final_round: bool = False,
) -> ValidationReport:
    """Run every check and produce the report stored on the article.

    Runs all checks rather than short-circuiting on the first failure: when a
    draft is bad you want the whole picture in one report, not one symptom per
    re-run.

    Returns:
        A report whose ``passed`` decides the article's status:
        True → ``pending_review``, False → ``validation_failed``.
    """
    failures: list[ValidationFailure] = []

    failures.extend(check_handles_were_provided(output, payload))

    cited = output.all_cited_handles()
    resolved, resolve_failures = await check_sources_resolve(session, payload, cited)
    failures.extend(resolve_failures)

    failures.extend(check_beats_are_cited(output))

    failures.extend(check_body_parses(output))

    failures.extend(check_numbers_are_sourced(output, payload))

    warnings: list[ValidationFailure] = []
    no_evidence_cited = check_no_evidence_cites_nothing(output)
    (warnings if final_round else failures).extend(no_evidence_cited)
    (warnings if final_round else failures).extend(
        check_citations_point_at_best_source(output, payload)
    )

    grade, ceiling, verdict_failures = check_verdict_within_grade(
        output, resolved, payload
    )
    failures.extend(verdict_failures)

    # Warnings in every round: nothing is enforced on the labels yet.
    stance_tally, stance_warnings = check_verdict_against_stance(output, resolved, payload)
    warnings.extend(stance_warnings)
    warnings.extend(check_reader_shorthand(output))
    warnings.extend(check_sentences_share_source_words(output, payload))

    report = ValidationReport(
        passed=not failures,
        citations_total=len(cited),
        citations_resolved=len(resolved),
        best_evidence_grade=grade,
        verdict_ceiling=ceiling,
        stance_tally=stance_tally,
        failures=failures,
        warnings=warnings,
    )

    if report.passed:
        # The verdict and its ceiling are logged together so the gap between
        # them is greppable: a run of "verdict=weak ceiling=supported" is the
        # signature of a model declining to commit on good evidence, and it
        # produces a clean report otherwise.
        logger.info(
            "draft validated: %s, grade=%s, verdict=%s, ceiling=%s",
            report.badge,
            grade,
            output.verdict,
            ceiling,
        )
    else:
        logger.warning(
            "draft REJECTED (%d failures): %s", len(failures), summarise_failures(report)
        )
    return report


def summarise_failures(report: ValidationReport) -> str:
    """One-line human summary for the console list view."""
    if report.passed:
        return "passed"

    counts: dict[str, int] = {}
    for failure in report.failures:
        counts[failure.code] = counts.get(failure.code, 0) + 1

    labels = {
        "hallucinated_handle": "hallucinated citation",
        "unresolvable_source": "unresolvable source",
        "uncited_beat": "uncited evidence beat",
        "uncited_section": "uncited section",
        "verdict_exceeds_grade": "verdict exceeds evidence grade",
        "malformed_body": "unparseable body",
        "unsourced_number": "number not in its cited source",
        "no_evidence_with_citations": "no-evidence verdict citing sources",
        "misattributed_citation": "finding cited to the wrong source",
    }
    parts = [
        f"{count} {labels.get(code, code)}{'s' if count > 1 else ''}"
        for code, count in sorted(counts.items())
    ]
    return ", ".join(parts)


def _handle_sort_key(handle: str) -> int:
    """Sort S2 before S10. Lexicographic order would not."""
    digits = handle[1:]
    return int(digits) if digits.isdigit() else 0


def _format_handles(handles: set[str]) -> str:
    return ", ".join(sorted(handles, key=_handle_sort_key)) or "none"
