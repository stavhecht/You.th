"""FULL_TEXT: which papers get read, which passages the model sees, and why
nothing here can fail a run."""

from __future__ import annotations

from app.domain.contracts import (
    CandidatePaper,
    Excerpt,
    ExtractionOutput,
    PromptSource,
    RankedSource,
    SynthesisInput,
)
from app.domain.enums import SourceApi, StudyType
from app.llm.prompts.synthesis import SYNTHESIS_SYSTEM_PROMPT, render_source_block
from app.pipeline.stages import PipelineContext, StageName
from app.pipeline.steps.full_text import (
    EXCERPTS_PER_PAPER,
    FullTextStage,
    best_excerpts,
    choose_papers,
    describes_literature_search,
)
from app.pipeline.steps.persist import excerpts_shown
from app.pipeline.steps.synthesize import SynthesizeStage
from app.retrieval.base import ProviderError
from app.retrieval.full_text import Section, parse_sections

# --- parsing the JATS body ----------------------------------------------------

_JATS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving DTD v1.2//EN"
  "JATS-archivearticle1.dtd">
<article>
  <front><article-meta><abstract><p>The abstract is not body text.</p></abstract>
  </article-meta></front>
  <body>
    <p>A paragraph before any section.</p>
    <sec><title>Methods</title>
      <p>Sixty adults took 600 mg daily for 60 days.</p>
      <sec><title>Outcomes</title><p>Cortisol was measured at <xref>12</xref> weeks.</p></sec>
      <table-wrap><table><tr><td>cell text</td></tr></table></table-wrap>
    </sec>
    <sec><title>Results</title>
      <p>Cortisol fell by 23%.</p>
      <fig><caption><p>A figure caption.</p></caption></fig>
    </sec>
    <sec><title>Acknowledgements</title><p>We thank everyone.</p></sec>
  </body>
  <back><ref-list><ref>A reference.</ref></ref-list></back>
</article>"""


def test_the_papers_own_reference_numbers_are_dropped() -> None:
    """They point into a reference list the model never sees, and a square
    bracket that is not a handle fails the draft if the model copies it."""
    xml = """<article><body><sec><title>Results</title>
      <p>Cortisol was measured in seven studies [34,35,36] and saliva in two [39\u201341].
      Chandrasekhar et al. [12] found a fall.</p></sec></body></article>"""

    assert parse_sections(xml)[0].text == (
        "Cortisol was measured in seven studies and saliva in two. "
        "Chandrasekhar et al. found a fall."
    )


def test_the_body_is_read_section_by_section_in_reading_order() -> None:
    assert parse_sections(_JATS) == [
        Section(title=None, text="A paragraph before any section."),
        Section(
            title="Methods",
            text="Sixty adults took 600 mg daily for 60 days.\n\n"
            "Cortisol was measured at 12 weeks.",
        ),
        Section(title="Results", text="Cortisol fell by 23%."),
    ]


def test_tables_figures_admin_sections_and_back_matter_are_left_out() -> None:
    text = " ".join(section.text for section in parse_sections(_JATS))

    for absent in ("cell text", "figure caption", "We thank", "A reference", "abstract"):
        assert absent not in text


def test_introduction_and_background_are_left_out() -> None:
    """They summarise other papers; an excerpt from one would let the model
    credit this paper with its reference list's findings."""
    xml = (
        "<article><body>"
        "<sec><title>Background</title><p>Earlier trials found benefits [3, 5].</p></sec>"
        "<sec><title>1. Introduction</title><p>Stress is common [1].</p></sec>"
        "<sec><title>Results</title><p>Cortisol fell by 23%.</p></sec>"
        "</body></article>"
    )
    assert parse_sections(xml) == [Section(title="Results", text="Cortisol fell by 23%.")]


def test_a_paper_with_no_body_has_no_sections() -> None:
    assert parse_sections("<article><front/></article>") == []


def test_a_bioavailability_section_is_not_mistaken_for_an_admin_one() -> None:
    xml = (
        "<article><body><sec><title>Bioavailability</title>"
        "<p>Absorbed well.</p></sec></body></article>"
    )
    assert parse_sections(xml) == [Section(title="Bioavailability", text="Absorbed well.")]


# --- choosing papers -----------------------------------------------------------


def _ranked(source_id: str, claim: str, handle: str) -> RankedSource:
    # A source id starting "doi" stands for a paper known only by its DOI.
    doi_only = source_id.startswith("doi")
    return RankedSource(
        source_id=source_id,
        claim=claim,
        citation_handle=handle,
        paper=CandidatePaper(
            pmid=None if doi_only else f"pmid-{source_id}",
            doi=f"10.1/{source_id}" if doi_only else None,
            title=f"Paper {source_id}",
            abstract="An abstract long enough to stand in for a real one here.",
            study_type=StudyType.RCT,
            source_api=SourceApi.PUBMED,
        ),
        cosine_similarity=0.8,
        final_score=0.9,
    )


def _ranking(**claims: list[str]) -> dict[str, list[RankedSource]]:
    """Claim -> ranked sources, one handle per source as ``assign_handles`` gives."""
    handles: dict[str, str] = {}
    return {
        claim: [
            _ranked(source_id, claim, handles.setdefault(source_id, f"S{len(handles) + 1}"))
            for source_id in ids
        ]
        for claim, ids in claims.items()
    }


def test_every_claim_gets_a_paper_before_any_claim_gets_a_second() -> None:
    ranked = _ranking(sleep=["a1", "a2", "a3"], stress=["b1"])
    open_access = {source_id: f"PMC-{source_id}" for source_id in ("a1", "a2", "a3", "b1")}

    assert list(choose_papers(ranked, open_access, limit=3)) == ["a1", "b1", "a2"]


def test_only_open_access_papers_are_chosen_and_never_twice() -> None:
    ranked = _ranking(sleep=["a", "b", "c"], stress=["c", "d"])
    open_access = {"a": "PMC-a", "c": "PMC-c", "d": "PMC-d"}

    assert choose_papers(ranked, open_access, limit=6) == {
        "a": "PMC-a",
        "c": "PMC-c",
        "d": "PMC-d",
    }


def test_the_limit_is_respected() -> None:
    ranked = _ranking(sleep=[f"s{index}" for index in range(10)])
    open_access = {f"s{index}": f"PMC{index}" for index in range(10)}

    assert len(choose_papers(ranked, open_access, limit=6)) == 6


# --- choosing passages ---------------------------------------------------------


def test_the_closest_passages_are_kept_and_put_back_in_reading_order() -> None:
    passages = [("a", Excerpt(section=None, text=f"passage {index}")) for index in range(3)]
    vectors = [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]]

    best = best_excerpts(passages, vectors, {"a": [[1.0, 0.0]]}, per_paper=2)

    assert [excerpt.text for excerpt in best["a"]] == ["passage 0", "passage 2"]


def test_a_passage_counts_as_close_to_any_of_its_papers_claims() -> None:
    """A paper retrieved for two claims keeps a passage for each."""
    passages = [
        ("a", Excerpt(section=None, text=text)) for text in ("sleep", "stress", "neither")
    ]
    vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    claims = {"a": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]}

    best = best_excerpts(passages, vectors, claims, per_paper=2)

    assert [excerpt.text for excerpt in best["a"]] == ["sleep", "stress"]


def test_a_reviews_search_is_not_an_excerpt() -> None:
    """Both passages were measured: they score as close to the claim as a
    finding does, and say nothing a reader could use."""
    assert describes_literature_search(
        "The literature search identified 4788 articles from PubMed/MEDLINE, EMBASE, "
        "CENTRAL, Google Scholar, and Scopus. After duplicates were removed, 12 remained."
    )
    assert describes_literature_search(
        "Literature searches were conducted using the following databases: PubMed, "
        "Scopus and Web of Science. The search strategy combined the terms stress and WS."
    )


def test_a_trials_recruitment_is_kept() -> None:
    """Recruitment carries the sample size, and trials say "screened" too."""
    assert not describes_literature_search(
        "Initially, 74 participants were screened for participation and 60 met the "
        "eligibility criteria; 58 completed the 8-week study."
    )
    assert not describes_literature_search(
        "Results were compared with those in the ClinicalTrials.gov database."
    )


# --- the stage -------------------------------------------------------------------


class _Client:
    def __init__(
        self,
        open_access: dict[str, str],
        papers: dict[str, list[Section] | Exception],
        lookup_error: Exception | None = None,
    ) -> None:
        self._open_access = open_access
        self._papers = papers
        self._lookup_error = lookup_error
        self.lookups = 0

    async def open_access_pmcids(self, pmids: list[str], dois: list[str]) -> dict[str, str]:
        self.lookups += 1
        self.asked = (pmids, dois)
        if self._lookup_error:
            raise self._lookup_error
        return {
            **{f"pmid:{p}": self._open_access[p] for p in pmids if p in self._open_access},
            **{f"doi:{d}": self._open_access[d] for d in dois if d in self._open_access},
        }

    async def sections(self, pmcid: str) -> list[Section]:
        paper = self._papers[pmcid]
        if isinstance(paper, Exception):
            raise paper
        return paper


class _Embedder:
    """Every claim points one way; a passage mentioning cortisol points the same way."""

    model_id = "test-embedder"
    dimension = 2

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "cortisol" in text.lower() else [0.0, 1.0] for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


_PAPER = [
    Section(title="Introduction", text="Ashwagandha is an Ayurvedic herb."),
    Section(title="Methods", text="Cortisol was measured every two weeks."),
    Section(title="Results", text="Serum cortisol fell by 23% against placebo."),
    Section(title="Discussion", text="Larger trials are needed."),
]


def _ctx() -> PipelineContext:
    ranked = _ranking(stress=["a", "b"])
    return PipelineContext(run_id="run-1", topic="ashwagandha", ranked=ranked)


def _stage(client: _Client, max_papers: int = 6) -> FullTextStage:
    return FullTextStage(client, _Embedder(), max_papers)  # type: ignore[arg-type]


async def test_the_passages_closest_to_the_claim_reach_the_context() -> None:
    client = _Client({"pmid-a": "PMC-a"}, {"PMC-a": _PAPER})

    result = await _stage(client).run(_ctx())

    assert result.excerpts == {
        "a": [
            Excerpt(section="Methods", text="Cortisol was measured every two weeks."),
            Excerpt(section="Results", text="Serum cortisol fell by 23% against placebo."),
        ]
    }
    metrics = result.metrics[StageName.FULL_TEXT]
    assert (metrics["cause"], metrics["open_access"], metrics["chosen"]) == (None, 1, 1)
    assert metrics["excerpts"] == EXCERPTS_PER_PAPER


async def test_a_paper_with_no_pmid_is_looked_up_by_its_doi() -> None:
    """A DOI-only paper used to be skipped before Europe PMC was even asked."""
    client = _Client({"10.1/doi-a": "PMC-a"}, {"PMC-a": _PAPER})
    ranked = _ranking(stress=["doi-a"])
    ctx = PipelineContext(run_id="run-1", topic="ashwagandha", ranked=ranked)

    result = await _stage(client).run(ctx)

    assert client.asked == ([], ["10.1/doi-a"])
    assert list(result.excerpts) == ["doi-a"]
    assert result.metrics[StageName.FULL_TEXT]["looked_up_by_doi"] == 1


async def test_an_unreachable_europe_pmc_leaves_the_article_on_abstracts() -> None:
    client = _Client({}, {}, lookup_error=ProviderError("europe_pmc returned 503"))

    result = await _stage(client).run(_ctx())

    assert result.excerpts == {}
    assert result.metrics[StageName.FULL_TEXT]["cause"] == "lookup_failed"


async def test_one_failed_download_costs_only_that_paper() -> None:
    client = _Client(
        {"pmid-a": "PMC-a", "pmid-b": "PMC-b"},
        {"PMC-a": ProviderError("europe_pmc returned 500"), "PMC-b": _PAPER},
    )

    result = await _stage(client).run(_ctx())

    assert list(result.excerpts) == ["b"]
    assert result.metrics[StageName.FULL_TEXT]["failed"] == 1


async def test_no_open_access_paper_means_no_excerpts() -> None:
    result = await _stage(_Client({}, {})).run(_ctx())

    assert result.excerpts == {}
    assert result.metrics[StageName.FULL_TEXT]["cause"] == "no_open_access"


async def test_zero_papers_turns_the_stage_off_without_calling_europe_pmc() -> None:
    client = _Client({"pmid-a": "PMC-a"}, {"PMC-a": _PAPER})

    result = await _stage(client, max_papers=0).run(_ctx())

    assert result.excerpts == {}
    assert result.metrics[StageName.FULL_TEXT]["cause"] == "disabled"
    assert client.lookups == 0


async def test_an_unexpected_error_is_recorded_not_raised() -> None:
    class _Broken(_Embedder):
        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding server went away")

    client = _Client({"pmid-a": "PMC-a"}, {"PMC-a": _PAPER})
    stage = FullTextStage(client, _Broken(), 6)  # type: ignore[arg-type]

    result = await stage.run(_ctx())

    assert result.excerpts == {}
    assert result.metrics[StageName.FULL_TEXT]["cause"] == "unexpected"


def test_full_text_runs_after_rank_and_before_synthesis() -> None:
    """It reads only papers that made the cut, and synthesis needs its output."""
    order = list(StageName)
    assert order.index(StageName.RANK) < order.index(StageName.FULL_TEXT)
    assert order.index(StageName.FULL_TEXT) < order.index(StageName.SYNTHESIZE)


# --- into the prompt, and into the record ---------------------------------------------


def _prompt_source(handle: str, source_id: str, excerpts: list[Excerpt]) -> PromptSource:
    return PromptSource(
        handle=handle,
        title=f"Paper {source_id}",
        abstract="The abstract.",
        journal=None,
        year=2024,
        study_type=StudyType.RCT,
        source_id=source_id,
        claims=["reduces stress"],
        excerpts=excerpts,
    )


def test_square_brackets_in_source_text_reach_the_model_round() -> None:
    """Measured: a draft copied "95 % CI = [-3.83 to -0.55]" out of an
    abstract into beat 2 and was discarded as a malformed citation."""
    source = _prompt_source(
        "S1", "a", [Excerpt(section="Results [main]", text="SMD 0.4 [0.1 to 0.7].")]
    ).model_copy(update={"abstract": "HAS (MD = -2.19, 95 % CI = [-3.83 to -0.55])."})
    payload = SynthesisInput(
        product="Ashwagandha", target_claims=["reduces stress"], sources=[source]
    )

    block = render_source_block(payload)

    assert block.startswith("[S1] ")  # the handle is still a handle
    body = block.removeprefix("[S1] ")
    assert "[" not in body and "]" not in body
    assert "(-3.83 to -0.55)" in body and "(0.1 to 0.7)" in body


def test_excerpts_are_rendered_under_the_abstract_with_their_section() -> None:
    excerpt = Excerpt(section="Results", text="Cortisol fell by 23%.")
    payload = SynthesisInput(
        product="Ashwagandha",
        target_claims=["reduces stress"],
        sources=[_prompt_source("S1", "a", [excerpt]), _prompt_source("S2", "b", [])],
    )

    first, second = render_source_block(payload).split("\n\n---\n\n")

    assert first.endswith(
        "Abstract: The abstract.\nFull-text excerpts:\n(Results) Cortisol fell by 23%."
    )
    assert "Full-text excerpts" not in second


def test_the_system_prompt_explains_excerpts() -> None:
    assert "full-text excerpts" in SYNTHESIS_SYSTEM_PROMPT


def test_synthesis_hands_each_source_its_own_excerpts() -> None:
    excerpt = Excerpt(section="Results", text="Cortisol fell by 23%.")
    ctx = _ctx().model_copy(
        update={
            "extraction": ExtractionOutput(product="Ashwagandha", target_claims=["stress"]),
            "excerpts": {"a": [excerpt]},
        }
    )

    sources = {source.source_id: source for source in SynthesizeStage._build_input(ctx).sources}

    assert sources["a"].excerpts == [excerpt]
    assert sources["b"].excerpts == []


def test_only_sources_shown_with_excerpts_are_recorded() -> None:
    payload = SynthesisInput(
        product="Ashwagandha",
        target_claims=["reduces stress"],
        sources=[
            _prompt_source("S1", "a", [Excerpt(section="Results", text="Cortisol fell.")]),
            _prompt_source("S2", "b", []),
        ],
    )

    assert excerpts_shown(payload) == {"a": [{"section": "Results", "text": "Cortisol fell."}]}
    assert excerpts_shown(None) == {}
