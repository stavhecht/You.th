"""What flows between the discovery stages.

Mirrors ``domain/contracts.py``'s role for the pipeline: every boundary in this
package takes and returns one of these, so a change to the shape fails at the
seam rather than three functions later.

The one deliberate absence is a paper's abstract. Discovery counts index terms,
never prose — an abstract-less MEDLINE record carries the full signal, which is
precisely why this package does not reuse ``CandidatePaper``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import StudyType


class DescriptorHit(BaseModel):
    """One MeSH term as it appears on one paper.

    ``in_chemical_list`` and ``intervention_qualifier`` are the two signals that
    separate an intervention from a biomarker, and they are kept per paper
    rather than aggregated because the rule between them is a ratio whose
    threshold is expected to be retuned against real data.
    """

    ui: str = Field(description="MeSH descriptor UI, e.g. 'D003401'")
    name: str
    #: ``MajorTopicYN="Y"`` — the indexer's judgement that the paper is *about*
    #: this, rather than merely mentioning it.
    major_topic: bool = False
    #: Appeared in this record's ``<ChemicalList>``.
    in_chemical_list: bool = False
    #: Tagged with /administration & dosage, /therapeutic use, /pharmacology or
    #: /adverse effects on this paper.
    intervention_qualifier: bool = False


class MeshRecord(BaseModel):
    """A PubMed record reduced to what discovery counts.

    ``entrez_date`` is the bucketing key: when PubMed *received* the record, not
    when the journal published it and not when we found it. A paper indexed late
    belongs to the window it entered in, or a scan that happens to catch up on a
    backlog reads as a surge.
    """

    pmid: str
    entrez_date: date
    study_type: StudyType = StudyType.UNKNOWN
    descriptors: list[DescriptorHit] = Field(default_factory=list)


class ScoredCandidate(BaseModel):
    """A substance that cleared the bar, with the arithmetic that got it there.

    Every field except ``score`` exists to be *shown* — to a reviewer in the
    console, or to whoever reads the dry run. A ranking nobody can audit is a
    ranking nobody can tune, and this one is built from guessed constants.
    """

    substance_ui: str
    substance_name: str
    outcome_ui: str | None = None
    outcome_name: str | None = None
    topic: str
    score: float
    #: Papers backing the *substance* this window — the trend signal, and the
    #: number the score and lift are computed from.
    paper_count: int
    #: Papers backing this specific angle. Equal to ``paper_count`` when the
    #: candidate names no outcome. Both are shown, because they answer different
    #: questions: 8 papers on omega-3 is why it is on the desk, 3 of them on
    #: skin is what the article would actually rest on.
    angle_paper_count: int = 0
    baseline_count: float
    lift: float
    #: Study types among this window's papers for the substance, by value.
    #: Drives the quality weight and tells a reviewer what kind of evidence the
    #: surge is made of — six case reports and six RCTs score differently and
    #: read very differently.
    study_mix: dict[str, int] = Field(default_factory=dict)
    #: A handful of PMIDs so a reviewer can check the claim in one click.
    top_pmids: list[str] = Field(default_factory=list)

    def rationale(self, window_start: date, window_end: date) -> dict[str, Any]:
        """The JSONB blob stored on the candidate row.

        ``paper_count`` and ``angle_paper_count`` are duplicated in here on
        purpose: the dismissal rule compares a later scan against what was known
        *at dismissal*, and the columns will have moved on by then. Suppression
        reads the **angle** count, because a dismissal is now a judgement about
        one question rather than about the substance.
        """
        return {
            "lift": round(self.lift, 3),
            "paper_count": self.paper_count,
            "angle_paper_count": self.angle_paper_count,
            "baseline_count": round(self.baseline_count, 3),
            "study_mix": self.study_mix,
            "top_pmids": self.top_pmids,
            "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        }
