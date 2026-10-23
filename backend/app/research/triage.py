"""The research agent's one model call: what each rising query is about.

Deciding that "creatine before bed" and "taking creatine at night" ask the same
thing, that "creatine" is the subject and "sleep" the outcome, and that
"taylor swift workout" is not a health question, is semantic work — the one
place in topic discovery where a model earns its cost (one call per run, on
the already-clustered queries).

**The output is checked, never trusted.** Every id must be one we sent, each
at most once; anything the model dropped falls back to the deterministic
reading of its cluster. And if the call fails outright, the whole run falls
back to that reading and says so in its notes. The model decides what a query
*means*; it never decides whether a claim is true or how strong the evidence
is — that is PubMed's job, in ``graph.py``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from ollama import AsyncClient
from pydantic import BaseModel, ConfigDict, Field

from app.llm.base import LLMError, LLMResult
from app.llm.llamacpp_client import LlamaCppChat
from app.llm.ollama_client import structured_chat
from app.research.contracts import CandidateTopic, Category
from app.research.normalize import QueryCluster, fallback_subject, query_key

logger = logging.getLogger(__name__)

TRIAGE_NUM_CTX = 8_192
TRIAGE_MAX_TOKENS = 3_000
#: Query groups per call. Measured 2026-09-30: handed 75 at once, qwen2.5:7b
#: skipped 25 and lumped 58 queries into one topic called "General Wellness and
#: Health". Small batches are what a 7B model can actually keep track of.
TRIAGE_BATCH = 25
#: A topic covering more groups than this is a model lumping unrelated queries
#: together, not a topic; its groups fall back to their raw queries.
MAX_GROUPS_PER_TOPIC = 6
#: Subjects that name no subject.
_VAGUE_SUBJECTS = frozenset({"various", "general", "multiple", "mixed", "misc", "other"})


class TriageTopic(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"description": "One article topic covering one or more queries."}
    )

    ids: list[int] = Field(min_length=1, description="Ids of the queries this topic covers.")
    canonical_topic: str = Field(
        min_length=3,
        max_length=80,
        description='Short plain-words topic, e.g. "Creatine before bed".',
    )
    category: Category
    is_wellness_topic: bool = Field(
        description="True only for a wellness, fitness, nutrition or health question a "
        "science-based article could address."
    )
    subject: str = Field(
        min_length=1,
        max_length=60,
        description='The substance, food, activity or practice, e.g. "creatine".',
    )
    outcome: str = Field(
        default="",
        max_length=60,
        description='What it is sought for, e.g. "sleep". Empty if none.',
    )
    reader_question: str = Field(
        default="", max_length=160, description="The reader's question, one sentence."
    )


class TriageOutput(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"description": "Topics for the given queries."}
    )

    topics: list[TriageTopic]


TRIAGE_SYSTEM_PROMPT = f"""\
You sort search queries rising on Google, and phrases from recent news \
headlines, into article topics for a wellness, fitness and health publication.

Group only queries that ask about the same specific thing into one topic; most \
topics cover one or two queries. Never make a catch-all topic. For each topic \
return:
- ids: the ids of the queries it covers. Every id belongs to exactly one topic.
- canonical_topic: a short topic in plain words, like "Creatine before bed".
- category: one of {", ".join(c.value for c in Category)}. Use other \
only for a wellness topic that fits none of the rest.
- is_wellness_topic: true only when a science-based article on wellness, \
fitness, nutrition, sleep, recovery or health could address it. False for \
celebrities, brands, products for sale, shopping queries ("best running \
shoes", "cheap protein powder"), shops, news events with no health question, \
sports results, games, and recipes with no health question.
- subject: the substance, food, activity or practice the topic is about, like \
"creatine" or "zone 2 training". Use the common name.
- outcome: what people want from it, like "sleep" or "muscle growth". Leave it \
empty when the query names no outcome.
- reader_question: the reader's question in one sentence.

A line may end with headlines the phrase was seen in; use them only to \
understand what the phrase refers to. Only use the queries given. Do not judge \
whether any claim is true."""


#: Example headlines per query group, by cluster id. Context, not topics.
Examples = Mapping[int, list[str]]


class TriageClient(Protocol):
    async def triage(
        self, clusters: list[QueryCluster], examples: Examples | None = None
    ) -> TriageOutput: ...


class OllamaTriageClient:
    def __init__(self, client: AsyncClient, model: str) -> None:
        self._client = client
        self._model = model
        #: Batches that failed in the last call; their groups fall back to
        #: the deterministic reading instead of failing every other batch too.
        self.failed_batches = 0
        #: Cluster ids in those failed batches: never asked, so not "left out".
        self.failed_ids: set[int] = set()

    async def triage(
        self, clusters: list[QueryCluster], examples: Examples | None = None
    ) -> TriageOutput:
        topics: list[TriageTopic] = []
        self.failed_batches = 0
        self.failed_ids = set()
        batches = range(0, len(clusters), TRIAGE_BATCH)
        last_error: LLMError | None = None
        for start in batches:
            try:
                result = await self._ask(
                    build_triage_user_prompt(clusters[start : start + TRIAGE_BATCH], examples)
                )
            except LLMError as exc:
                self.failed_batches += 1
                batch_clusters = clusters[start : start + TRIAGE_BATCH]
                self.failed_ids.update(c.cluster_id for c in batch_clusters)
                last_error = exc
                logger.warning("research triage batch at %d failed: %s", start, exc)
                continue
            batch: TriageOutput = result.output
            topics.extend(batch.topics)
        if last_error is not None and self.failed_batches == len(batches):
            raise last_error
        return TriageOutput(topics=topics)

    async def _ask(self, user: str) -> LLMResult[Any]:
        return await structured_chat(
            self._client,
            call="research triage",
            model=self._model,
            system=TRIAGE_SYSTEM_PROMPT,
            user=user,
            output_format=TriageOutput,
            num_ctx=TRIAGE_NUM_CTX,
            max_tokens=TRIAGE_MAX_TOKENS,
        )


class LlamaCppTriageClient(OllamaTriageClient):
    """Triage on llama-server, for ``LLM_PROVIDER=llamacpp``.

    Ollama's language model cannot share the GPU with llama-server, so this
    path used to skip triage, and the raw rising queries became topics: a run
    on 2026-10-23 selected "care" and "cognitive" and ranked "benefit" first.
    Same batches, prompt and checks; only the transport differs.
    """

    def __init__(self, chat: LlamaCppChat) -> None:
        super().__init__(None, chat.model)  # type: ignore[arg-type]
        self._chat = chat

    async def _ask(self, user: str) -> LLMResult[Any]:
        return await self._chat.chat(
            call="research triage",
            system=TRIAGE_SYSTEM_PROMPT,
            user=user,
            output_format=TriageOutput,
            max_tokens=TRIAGE_MAX_TOKENS,
            temperature=0.0,
        )


def build_triage_user_prompt(
    clusters: list[QueryCluster], examples: Examples | None = None
) -> str:
    lines = []
    for cluster in clusters:
        others = [query for query in cluster.queries if query != cluster.representative.query]
        also = f"; also: {', '.join(others[:4])}" if others else ""
        seen = (examples or {}).get(cluster.cluster_id) or []
        headlines = (
            "; seen in: " + "; ".join(f'"{title[:120]}"' for title in seen[:2]) if seen else ""
        )
        lines.append(
            f"{cluster.cluster_id}: {cluster.representative.query} "
            f"(seed: {cluster.representative.seed}{also}{headlines})"
        )
    return "Queries:\n" + "\n".join(lines)


def apply_triage(
    clusters: list[QueryCluster],
    output: TriageOutput | None,
    *,
    allowed: list[Category],
    unclassified: set[int] | None = None,
) -> tuple[list[CandidateTopic], int]:
    """Candidates from the model's grouping, checked against what was sent.

    Returns the candidates and how many clusters fell back to the deterministic
    reading because the model skipped them (all of them when ``output`` is
    None). Non-wellness and out-of-category topics come back *discarded with a
    reason*, not dropped, so the desk can show what was considered.

    ``unclassified`` names groups a working model was asked about and left out
    — twice (``triage_or_fallback`` asks again). They come back discarded:
    nobody judged them to be wellness topics, and measured 2026-10-12 one of
    them ("soulja boy running for president", under the seed "running") was
    ranked fourth on the raw query. A group the model was never asked about
    (its batch failed) keeps the deterministic reading as before.
    """
    by_id = {cluster.cluster_id: cluster for cluster in clusters}
    claimed: set[int] = set()
    candidates: list[CandidateTopic] = []

    for topic, ids in _accepted(output, by_id):
        claimed.update(ids)
        members = [by_id[i] for i in ids]
        candidate = _candidate(
            members,
            canonical=topic.canonical_topic.strip(),
            category=topic.category,
            subject=topic.subject.strip(),
            outcome=topic.outcome.strip(),
            reader_question=topic.reader_question.strip() or None,
        )
        if not topic.is_wellness_topic:
            candidate.discard("not a wellness, fitness or health question (triage)")
        candidates.append(candidate)

    skipped = [cluster for cluster in clusters if cluster.cluster_id not in claimed]
    for cluster in skipped:
        candidate = _candidate(
            [cluster],
            canonical=cluster.representative.query,
            category=cluster.category,
            subject=fallback_subject(cluster.representative.query),
            outcome="",
            reader_question=None,
        )
        if unclassified and cluster.cluster_id in unclassified:
            candidate.discard("triage did not classify this query (asked twice)")
        candidates.append(candidate)

    candidates = merge_duplicates(candidates)
    for candidate in candidates:
        if not candidate.live or candidate.category in allowed:
            continue
        # Triage filed it elsewhere, but it was found under a requested
        # category's seed: keep it there. Measured 2026-10-12 — narrowed to
        # supplements, triage filed "Creatine and Muscle Growth" under
        # nutrition, and the run discarded the very topic its seed asked about.
        seeded = next((c for c in candidate.seed_categories if c in allowed), None)
        if seeded is not None:
            candidate.category = seeded
        else:
            candidate.discard(f"category {candidate.category.value} was not requested")
    return candidates, len(skipped)


def _accepted(
    output: TriageOutput | None, by_id: Mapping[int, QueryCluster]
) -> list[tuple[TriageTopic, list[int]]]:
    """The model's topics that pass the checks, each with the ids it claims.

    Ids must be ones we sent, each claimed once (first topic wins); a topic
    spanning more than ``MAX_GROUPS_PER_TOPIC`` groups, or with a subject that
    names nothing, is refused and its groups stay unclaimed.
    """
    claimed: set[int] = set()
    accepted: list[tuple[TriageTopic, list[int]]] = []
    for topic in output.topics if output is not None else []:
        ids = [i for i in dict.fromkeys(topic.ids) if i in by_id and i not in claimed]
        if (
            not ids
            or len(ids) > MAX_GROUPS_PER_TOPIC
            or topic.subject.strip().lower() in _VAGUE_SUBJECTS
        ):
            continue
        claimed.update(ids)
        accepted.append((topic, ids))
    return accepted


def claimed_ids(output: TriageOutput | None, clusters: list[QueryCluster]) -> set[int]:
    by_id = {cluster.cluster_id: cluster for cluster in clusters}
    return {i for _, ids in _accepted(output, by_id) for i in ids}


def merge_duplicates(candidates: list[CandidateTopic]) -> list[CandidateTopic]:
    """Fold candidates that name the same topic into the first of them.

    Triage runs in batches and cannot merge across them, and a group it skipped
    keeps its raw query — so one run produced "gut health" and "Gut Health",
    and "Intermittent Fasting" beside "Fasting intermittently". Same key (word
    order, case and plurals ignored), or the same subject and outcome, is one
    topic. The first keeps its name, since candidates arrive strongest first.
    """
    kept: list[CandidateTopic] = []
    index: dict[str, CandidateTopic] = {}
    for candidate in candidates:
        keys = [
            f"topic:{query_key(candidate.canonical_topic)}",
            f"pair:{query_key(candidate.subject)}|{query_key(candidate.outcome)}",
        ]
        existing = next((index[key] for key in keys if key in index), None)
        if existing is None or existing.live != candidate.live:
            kept.append(candidate)
            for key in keys:
                index.setdefault(key, candidate)
            continue
        existing.queries = list(dict.fromkeys(existing.queries + candidate.queries))
        existing.seeds = list(dict.fromkeys(existing.seeds + candidate.seeds))
        existing.seed_categories = list(
            dict.fromkeys(existing.seed_categories + candidate.seed_categories)
        )
    return kept


def _candidate(
    members: list[QueryCluster],
    *,
    canonical: str,
    category: Category,
    subject: str,
    outcome: str,
    reader_question: str | None,
) -> CandidateTopic:
    trends = [trend for member in members for trend in member.trends]
    trends.sort(key=lambda t: (not t.is_breakout, -(t.rising_percent or 0), t.depth))
    queries = list(dict.fromkeys(trend.query for trend in trends))
    seed_categories = list(dict.fromkeys(trend.category for trend in trends))
    return CandidateTopic(
        topic_id=str(uuid.uuid4()),
        canonical_topic=canonical,
        category=category,
        subject=subject or fallback_subject(queries[0]),
        outcome=outcome,
        reader_question=reader_question,
        queries=queries,
        seeds=list(dict.fromkeys(trend.seed for trend in trends)),
        seed_categories=seed_categories,
    )


async def triage_or_fallback(
    client: TriageClient | None,
    clusters: list[QueryCluster],
    *,
    allowed: list[Category],
    examples: Examples | None = None,
) -> tuple[list[CandidateTopic], str | None]:
    """Run triage, degrading to the deterministic reading on any failure.

    Returns the candidates and a note for the run when it degraded.
    """
    if client is None:
        candidates, _ = apply_triage(clusters, None, allowed=allowed)
        return (
            candidates,
            "triage skipped (no local model configured); topics are the raw queries",
        )
    try:
        output = await client.triage(clusters, examples)
    except LLMError as exc:
        logger.warning("research triage failed, using raw queries: %s", exc)
        candidates, _ = apply_triage(clusters, None, allowed=allowed)
        return candidates, f"triage failed ({exc}); topics are the raw queries"
    failed = int(getattr(client, "failed_batches", 0) or 0)
    never_asked: set[int] = set(getattr(client, "failed_ids", set()) or set())

    # Groups the model was shown and gave no usable answer for — left out, or
    # lumped into a refused catch-all — get one more, smaller ask: a 7B model
    # drops ids from a batch of 25 (16 of 75 on 2026-10-12), and alone they
    # are usually answered. Whatever is still unclaimed is set aside.
    answered = claimed_ids(output, clusters) | never_asked
    omitted = [c for c in clusters if c.cluster_id not in answered]
    still_omitted: set[int] = set()
    if omitted:
        try:
            retry = await client.triage(omitted, examples)
        except LLMError as exc:
            logger.warning("research triage retry failed: %s", exc)
        else:
            output = TriageOutput(topics=[*output.topics, *retry.topics])
            still_omitted = {c.cluster_id for c in omitted} - claimed_ids(output, clusters)
    candidates, skipped = apply_triage(
        clusters, output, allowed=allowed, unclassified=still_omitted
    )
    notes: list[str] = []
    if omitted:
        notes.append(
            f"triage gave no usable answer for {len(omitted)} query groups and was asked "
            f"again; {len(still_omitted)} stayed unclassified and were set aside"
        )
    fallback = skipped - len(still_omitted)
    if fallback:
        notes.append(f"triage skipped {fallback} query groups; they use the raw query")
    if failed:
        notes.append(f"{failed} triage batch{'es' if failed != 1 else ''} failed")
    return candidates, "; ".join(notes) or None
