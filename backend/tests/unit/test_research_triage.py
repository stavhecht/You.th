"""Triage output is checked against what was sent, and degrades without a model."""

from __future__ import annotations

from app.domain.enums import ResearchCandidateStatus
from app.llm.base import LLMError
from app.research.contracts import Category, RawTrend
from app.research.normalize import QueryCluster, merge_exact
from app.research.triage import (
    TriageOutput,
    TriageTopic,
    apply_triage,
    build_triage_user_prompt,
    claimed_ids,
    triage_or_fallback,
)


def _clusters() -> list[QueryCluster]:
    return merge_exact(
        [
            RawTrend(
                query=query,
                seed=seed,
                category=category,
                source="google_trends",
                rising_percent=rising,
            )
            for query, seed, category, rising in [
                ("creatine before bed", "creatine", Category.SUPPLEMENTS, 900),
                ("creatine at night", "creatine", Category.SUPPLEMENTS, 700),
                ("taylor swift workout", "workout", Category.FITNESS, 600),
                ("mouth taping sleep", "sleep", Category.SLEEP_RECOVERY, 500),
            ]
        ]
    )


def _topic(ids: list[int], **overrides: object) -> TriageTopic:
    fields: dict[str, object] = {
        "ids": ids,
        "canonical_topic": "Creatine before bed",
        "category": Category.SUPPLEMENTS,
        "is_wellness_topic": True,
        "subject": "creatine",
        "outcome": "sleep",
        "reader_question": "Should I take creatine at night?",
    }
    fields.update(overrides)
    return TriageTopic.model_validate(fields)


def test_the_model_can_merge_queries_into_one_topic() -> None:
    clusters = _clusters()
    output = TriageOutput(
        topics=[
            _topic([0, 1]),
            _topic(
                [2],
                canonical_topic="Taylor Swift workout",
                is_wellness_topic=False,
                subject="taylor swift",
                outcome="",
            ),
            _topic(
                [3],
                canonical_topic="Mouth taping for sleep",
                category=Category.SLEEP_RECOVERY,
                subject="mouth taping",
            ),
        ]
    )

    candidates, skipped = apply_triage(clusters, output, allowed=list(Category))

    assert skipped == 0
    creatine = candidates[0]
    assert creatine.queries == ["creatine before bed", "creatine at night"]
    assert (creatine.subject, creatine.outcome) == ("creatine", "sleep")
    celebrity = candidates[1]
    assert celebrity.status is ResearchCandidateStatus.DISCARDED
    assert "not a wellness" in (celebrity.discard_reason or "")


def test_ids_the_model_invented_or_repeated_are_ignored() -> None:
    clusters = _clusters()
    output = TriageOutput(
        topics=[
            _topic([0, 99]),
            _topic([0, 1], canonical_topic="Creatine at night", outcome="night"),
        ]
    )

    candidates, skipped = apply_triage(clusters, output, allowed=list(Category))

    first = candidates[0]
    assert first.queries == ["creatine before bed"]
    # Id 1 survives from the second topic; 0 was already taken.
    assert candidates[1].queries == ["creatine at night"]
    # 2 and 3 were never covered: they fall back to their raw query.
    assert skipped == 2
    assert {c.canonical_topic for c in candidates[2:]} == {
        "taylor swift workout",
        "mouth taping sleep",
    }


def test_an_unrequested_category_is_discarded_with_a_reason() -> None:
    candidates, _ = apply_triage(_clusters(), None, allowed=[Category.SUPPLEMENTS])

    sleep = next(c for c in candidates if c.canonical_topic == "mouth taping sleep")
    assert sleep.status is ResearchCandidateStatus.DISCARDED
    assert "was not requested" in (sleep.discard_reason or "")


def test_the_prompt_lists_every_cluster_by_id() -> None:
    prompt = build_triage_user_prompt(_clusters())
    assert "0: creatine before bed (seed: creatine)" in prompt
    assert "3: mouth taping sleep" in prompt


class _FailingTriage:
    async def triage(
        self, clusters: list[QueryCluster], examples: object = None
    ) -> TriageOutput:
        raise LLMError("ollama is not running")


async def test_a_failed_model_call_degrades_to_the_raw_queries() -> None:
    candidates, note = await triage_or_fallback(
        _FailingTriage(), _clusters(), allowed=list(Category)
    )

    assert len(candidates) == 4
    assert note is not None and "triage failed" in note
    # Without a model the subject is the query minus filler words.
    assert candidates[0].subject == "creatine before bed"


async def test_no_model_configured_also_degrades_and_says_so() -> None:
    candidates, note = await triage_or_fallback(None, _clusters(), allowed=list(Category))
    assert len(candidates) == 4
    assert note is not None and "skipped" in note


def test_a_catch_all_topic_is_refused_and_its_groups_fall_back() -> None:
    """Measured: qwen2.5:7b lumped 58 queries into "General Wellness and Health"."""
    clusters = _clusters()
    output = TriageOutput(
        topics=[
            _topic(
                [0, 1, 2, 3],
                canonical_topic="General Wellness and Health",
                subject="various",
                outcome="various",
            )
        ]
    )

    candidates, skipped = apply_triage(clusters, output, allowed=list(Category))

    assert skipped == 4
    assert all(c.canonical_topic != "General Wellness and Health" for c in candidates)


async def test_the_ollama_client_triages_in_batches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each call sees at most TRIAGE_BATCH groups; the results are concatenated."""
    from app.llm.base import LLMResult, TokenUsage
    from app.research import triage as triage_module

    sizes: list[int] = []

    async def fake_chat(client: object, **kwargs: object) -> LLMResult[TriageOutput]:
        user = str(kwargs["user"])
        ids = [int(line.split(":")[0]) for line in user.splitlines()[1:]]
        sizes.append(len(ids))
        return LLMResult(
            output=TriageOutput(topics=[_topic([i]) for i in ids]),
            usage=TokenUsage(),
            model="ollama/fake",
        )

    monkeypatch.setattr(triage_module, "structured_chat", fake_chat)
    clusters = merge_exact(
        [
            RawTrend(
                query=f"creatine dose {n}",
                seed="creatine",
                category=Category.SUPPLEMENTS,
                source="google_trends",
                rising_percent=100 + n,
            )
            for n in range(60)
        ]
    )

    output = await triage_module.OllamaTriageClient(object(), "qwen").triage(clusters)  # type: ignore[arg-type]

    assert sizes == [25, 25, 10]
    assert len(output.topics) == 60


def test_the_same_topic_from_two_batches_is_one_candidate() -> None:
    """Measured: "gut health" beside "Gut Health", "Intermittent Fasting" beside
    "Fasting intermittently"."""
    clusters = merge_exact(
        [
            RawTrend(
                query=q,
                seed="fasting",
                category=Category.LIFESTYLE,
                source="google_trends",
                rising_percent=r,
            )
            for q, r in [
                ("intermittent fasting 16 8", 900),
                ("fasting intermittently", 500),
                ("gut health foods", 300),
            ]
        ]
    )
    output = TriageOutput(
        topics=[
            _topic(
                [0],
                canonical_topic="Intermittent fasting",
                category=Category.LIFESTYLE,
                subject="intermittent fasting",
                outcome="",
            ),
            _topic(
                [1],
                canonical_topic="Fasting intermittently",
                category=Category.LIFESTYLE,
                subject="intermittent fasting",
                outcome="",
            ),
            _topic(
                [2],
                canonical_topic="Gut health foods",
                category=Category.NUTRITION,
                subject="fermented foods",
                outcome="gut health",
            ),
        ]
    )

    candidates, _ = apply_triage(clusters, output, allowed=list(Category))

    assert [c.canonical_topic for c in candidates] == [
        "Intermittent fasting",
        "Gut health foods",
    ]
    assert candidates[0].queries == ["intermittent fasting 16 8", "fasting intermittently"]


def test_the_categories_are_the_article_categories() -> None:
    """The desk's trending filter and the feed drawer must offer one list."""
    from app.domain.enums import Subject

    assert [c.value for c in Category] == [s.value for s in Subject]


def test_the_triage_grammar_carries_no_maintainer_rationale() -> None:
    """An enum's docstring becomes its schema ``description``, which the model reads.

    ``Category`` mirrors ``Subject`` as its own class for exactly this reason;
    aliasing it would hand the triage model ``Subject``'s notes on colours and
    pictures as part of its generation grammar.
    """
    from app.research.triage import TriageOutput

    schema = TriageOutput.model_json_schema()["$defs"]["Category"]
    assert "description" not in schema


def test_other_alone_searches_every_seed() -> None:
    """Other has no seeds of its own; asked for alone it must not search nothing."""
    from app.research.seeds import SEEDS, seeds_for

    assert Category.OTHER not in SEEDS
    everything = {seed for seeds in SEEDS.values() for seed in seeds}
    assert {seed for seed, _ in seeds_for([Category.OTHER])} == everything
    assert seeds_for([Category.OTHER, Category.SUPPLEMENTS]) == seeds_for(
        [Category.SUPPLEMENTS]
    )


async def test_one_failed_batch_falls_back_alone(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A batch the model fails costs those groups their triage, not every batch."""
    from app.llm.base import LLMResult, TokenUsage
    from app.research import triage as triage_module

    calls = 0

    async def flaky_chat(client: object, **kwargs: object) -> LLMResult[TriageOutput]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LLMError("the second batch timed out")
        user = str(kwargs["user"])
        ids = [int(line.split(":")[0]) for line in user.splitlines()[1:]]
        return LLMResult(
            output=TriageOutput(topics=[_topic([i]) for i in ids]),
            usage=TokenUsage(),
            model="ollama/fake",
        )

    monkeypatch.setattr(triage_module, "structured_chat", flaky_chat)
    clusters = merge_exact(
        [
            RawTrend(
                query=f"creatine dose {n}",
                seed="creatine",
                category=Category.SUPPLEMENTS,
                source="google_trends",
                rising_percent=100 + n,
            )
            for n in range(60)
        ]
    )
    client = triage_module.OllamaTriageClient(object(), "qwen")  # type: ignore[arg-type]
    candidates, note = await triage_or_fallback(client, clusters, allowed=list(Category))

    assert client.failed_batches == 1
    assert note is not None and "1 triage batch failed" in note
    # The failed batch's 25 groups fell back to their raw queries; the others
    # were triaged (into one topic, since the fake names them all alike).
    raw = [c for c in candidates if c.canonical_topic.startswith("creatine dose")]
    assert len(raw) == 25
    assert any(c.canonical_topic == "Creatine before bed" for c in candidates)


async def test_every_batch_failing_is_still_a_failed_triage(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.research import triage as triage_module

    async def down(client: object, **kwargs: object) -> object:
        raise LLMError("ollama is not running")

    monkeypatch.setattr(triage_module, "structured_chat", down)
    client = triage_module.OllamaTriageClient(object(), "qwen")  # type: ignore[arg-type]
    candidates, note = await triage_or_fallback(client, _clusters(), allowed=list(Category))
    assert note is not None and note.startswith("triage failed")
    assert len(candidates) == 4


def test_headline_context_reaches_the_prompt_and_is_labelled_as_context() -> None:
    clusters = _clusters()
    prompt = build_triage_user_prompt(
        clusters, {clusters[0].cluster_id: ["Creatine at night: what a new trial found"]}
    )
    assert 'seen in: "Creatine at night: what a new trial found"' in prompt
    from app.research.triage import TRIAGE_SYSTEM_PROMPT

    assert "news headlines" in TRIAGE_SYSTEM_PROMPT
    assert "news events with no health question" in TRIAGE_SYSTEM_PROMPT


def test_a_topic_filed_elsewhere_stays_under_the_requested_seed_category() -> None:
    """Measured live: narrowed to supplements, triage filed creatine under nutrition."""
    clusters = _clusters()
    output = TriageOutput(
        topics=[_topic([0], category=Category.NUTRITION, canonical_topic="Creatine and muscle")]
    )
    candidates, _ = apply_triage(clusters, output, allowed=[Category.SUPPLEMENTS])
    creatine = next(c for c in candidates if c.canonical_topic == "Creatine and muscle")
    assert creatine.live
    assert creatine.category is Category.SUPPLEMENTS


class _Forgetful:
    """Answers every group except those it is told to forget, on every ask."""

    def __init__(self, forget_first: set[str], forget_always: set[str]) -> None:
        self.asks: list[list[str]] = []
        self._first, self._always = forget_first, forget_always
        self.failed_batches = 0
        self.failed_ids: set[int] = set()

    async def triage(
        self, clusters: list[QueryCluster], examples: object = None
    ) -> TriageOutput:
        first = not self.asks
        self.asks.append([c.representative.query for c in clusters])
        forget = self._always | (self._first if first else set())
        return TriageOutput(
            topics=[
                _topic(
                    [c.cluster_id],
                    canonical_topic=c.representative.query.title(),
                    subject=c.representative.query.split()[0],
                )
                for c in clusters
                if c.representative.query not in forget
            ]
        )


async def test_groups_the_model_left_out_are_asked_again_once() -> None:
    client = _Forgetful(forget_first={"mouth taping sleep"}, forget_always=set())
    candidates, note = await triage_or_fallback(client, _clusters(), allowed=list(Category))
    assert client.asks[1] == ["mouth taping sleep"]
    taping = next(c for c in candidates if "Mouth" in c.canonical_topic)
    assert taping.live
    assert note is not None and "asked again; 0 stayed unclassified" in note


async def test_a_group_left_out_twice_is_set_aside_not_ranked_on_its_raw_query() -> None:
    """Measured: "soulja boy running for president" ranked fourth on its raw query."""
    client = _Forgetful(forget_first=set(), forget_always={"taylor swift workout"})
    candidates, note = await triage_or_fallback(client, _clusters(), allowed=list(Category))
    swift = next(c for c in candidates if c.canonical_topic == "taylor swift workout")
    assert swift.status is ResearchCandidateStatus.DISCARDED
    assert "did not classify" in (swift.discard_reason or "")
    assert len(client.asks) == 2
    assert note is not None and "1 stayed unclassified" in note


async def test_triage_on_llama_server_goes_through_its_chat_in_the_same_batches() -> None:
    from app.llm.base import LLMResult, TokenUsage

    asked: list[str] = []

    class Chat:
        model = "bonsai"

        async def chat(self, **kwargs: object) -> LLMResult[TriageOutput]:
            asked.append(str(kwargs["user"]))
            return LLMResult(output=TriageOutput(topics=[]), usage=TokenUsage())

    clusters = merge_exact(
        [
            RawTrend(
                query=f"creatine dose {n}",
                seed="creatine",
                category=Category.SUPPLEMENTS,
                source="google_trends",
                rising_percent=100 + n,
            )
            for n in range(30)
        ]
    )

    from app.research.triage import LlamaCppTriageClient

    await LlamaCppTriageClient(Chat()).triage(clusters)  # type: ignore[arg-type]

    assert len(asked) == 2


def _bundled_clusters() -> list[QueryCluster]:
    queries = ["fish oil benefits", "zone 2 running", "fish oil brain", "ice bath benefits"]
    return [
        QueryCluster(
            cluster_id=index,
            trends=[
                RawTrend(
                    query=query,
                    seed="seed",
                    category=Category.SUPPLEMENTS,
                    source="google_trends",
                    rising_percent=100,
                )
            ],
        )
        for index, query in enumerate(queries)
    ]


def test_a_topic_bundling_an_unrelated_subject_keeps_only_its_own_queries() -> None:
    """2026-10-23: "Benefits of fish oil and zone 2 running" was one topic."""
    clusters = _bundled_clusters()
    output = TriageOutput(
        topics=[
            _topic(
                [0, 1, 2],
                canonical_topic="Benefits of fish oil and zone 2 running",
                subject="fish oil",
                outcome="brain health",
            )
        ]
    )

    assert claimed_ids(output, clusters) == {0, 2}, "zone 2 running is asked about again"
    candidates, _ = apply_triage(clusters, output, allowed=list(Category))
    assert candidates[0].canonical_topic == "fish oil for brain health"


def test_several_claims_about_one_subject_and_a_synonym_stay_one_topic() -> None:
    clusters = _bundled_clusters()
    output = TriageOutput(
        topics=[
            _topic(
                [0, 2],
                canonical_topic="Fish oil for the brain and heart",
                subject="fish oil",
                outcome="brain and heart health",
            ),
            _topic(
                [3],
                canonical_topic="Cold water immersion",
                subject="cold water immersion",
                outcome="recovery",
            ),
        ]
    )

    assert claimed_ids(output, clusters) == {0, 2, 3}
    candidates, _ = apply_triage(clusters, output, allowed=list(Category))
    assert candidates[0].canonical_topic == "Fish oil for the brain and heart"
