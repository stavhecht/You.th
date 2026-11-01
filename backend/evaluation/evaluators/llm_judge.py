"""An LLM judge for the questions only language understanding can answer.

Two kinds of call, both narrow:

* **Citation check** — one statement against the source text it cites:
  supported / partially_supported / not_supported / contradicted. This is the
  semantic half of citation correctness; ``evaluators/text.py`` supplies the
  mechanical half (numbers, vocabulary), and the report keeps the two apart.
* **Answer assessment** — the question, the article, the list of sources it was
  allowed to use: relevance, completeness, correctness (do the conclusions
  follow from those sources at their strength), certainty, and whether each
  expected claim is present and each forbidden claim asserted.

**Independence is enforced, not assumed.** The judge must be a different model
from the one that wrote the article — a model grading its own prose is the
circular evaluation this framework exists to avoid — and the run disables the
judge (and says so) when they match. The judge never sees the pipeline's own
validation report or verdict ceiling, only the article and the sources: it
forms its view from the evidence, not from the system's opinion of itself.

**It is still a small local model.** Its labels are noisy, so statement-level
hallucination needs a judge verdict *and* weak lexical support, or an
unsupported number, or a deterministic failure. The exception is a forbidden
claim the judge says the article asserts — nothing deterministic can read
meaning — and the report names each one so a person can confirm it. Every
judgement is cached by prompt, so a re-score is free and stable.

Runs through ``llm/ollama_client.py::structured_chat`` — the existing helper for
structured calls outside the pipeline — so the JSON grammar and the
validation-repair retry are the production ones.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.llm.base import LLMError
from app.llm.ollama_client import build_ollama_client, structured_chat
from evaluation.config import JudgeConfig
from evaluation.harness.cassette import Cassette, stable_key

logger = logging.getLogger(__name__)

#: Ollama silently drops the front of a prompt past num_ctx: measured 2026-10-25, a
#: 12-source citation check (9,703 tokens) kept 4,098 at 8K, statement included, and
#: the judge answered "contradicted" blind. So the window grows with the prompt.
_CTX_STEP = 8192
_CTX_CEILING = 32768
_CHARS_PER_TOKEN = 3  # conservative: biomedical English runs ~4
_MAX_OUTPUT_TOKENS = 600


def _context_for(system: str, user: str, floor: int, output_tokens: int) -> int | None:
    """The window that holds the whole prompt, or None when none here does."""
    needed = (len(system) + len(user)) // _CHARS_PER_TOKEN + output_tokens
    if needed <= floor:
        return floor
    ctx = -(-needed // _CTX_STEP) * _CTX_STEP
    return ctx if ctx <= _CTX_CEILING else None

CITATION_SYSTEM = """\
You check one statement from a health article against the source text it cites. \
Use only the source text below; ignore anything you know from elsewhere.

Labels:
- supported: the source text states the statement's finding, including its \
direction, the population, and any numbers.
- partially_supported: the main finding is in the source text, but some detail \
of the statement (a number, the population, the duration, or how large or how \
certain the effect is) is missing from the source text or overstated.
- not_supported: the source text does not contain the statement's finding.
- contradicted: the source text reports the opposite or a materially different \
result.

A statement that only describes what was studied, rather than a result, is \
supported if the source text describes that study.

explanation: one sentence. Return only the JSON object."""

ANSWER_SYSTEM = """\
You are an exacting reviewer of evidence-based health articles. You are given a \
reader's QUESTION, an ARTICLE written to answer it, and the SOURCES the article \
was allowed to use (title, study design, year). Judge only what is on the page. \
Do not reward length or a confident tone.

The article's parts are labelled. CLAIM UNDER REVIEW restates the claim being \
checked; it is not the article's view, so a claim that appears only there is \
not "asserted" and does not count as "present". Judge the article by what the \
other parts say about that claim.

Scores are integers from 1 to 5:
- relevance: 5 = directly answers the question asked; 3 = answers a related but \
different question; 1 = off-topic.
- completeness: 5 = addresses every part of the question; 3 = misses one part; \
1 = misses most of it.
- correctness: 5 = every conclusion follows from the listed sources and matches \
their strength; 3 = some conclusions go beyond the sources; 1 = conclusions \
contradict or ignore the sources.

certainty: "appropriate", "overconfident" or "underconfident", relative to the \
evidence. A few small or weak studies call for hedged language; several \
systematic reviews and randomised trials allow a firmer statement.

expected_claims: for each numbered EXPECTED CLAIM, give its index and status — \
"present" (the article states it or an equivalent), "absent", or \
"contradicted" (the article states the opposite).
forbidden_claims: for each numbered FORBIDDEN CLAIM, give its index and \
asserted = true only if the article states or clearly implies it is true. An \
article that names the claim in order to reject, refute or question it ("X does \
not double memory") does not assert it: asserted = false.
reasoning: at most three sentences naming the main problem, if there is one.

Return only the JSON object."""


class CitationJudgement(BaseModel):
    verdict: Literal["supported", "partially_supported", "not_supported", "contradicted"]
    explanation: str = ""


class ClaimStatus(BaseModel):
    index: int
    status: Literal["present", "absent", "contradicted"]


class ForbiddenStatus(BaseModel):
    index: int
    asserted: bool


class AnswerJudgement(BaseModel):
    relevance: int
    completeness: int
    correctness: int
    certainty: Literal["appropriate", "overconfident", "underconfident"]
    expected_claims: list[ClaimStatus] = Field(default_factory=list)
    forbidden_claims: list[ForbiddenStatus] = Field(default_factory=list)
    reasoning: str = ""

    @field_validator("relevance", "completeness", "correctness")
    @classmethod
    def _clamp(cls, value: int) -> int:
        # Clamped rather than rejected: a 6 is a 5 with enthusiasm, and a
        # validation failure would spend a repair round on it.
        return min(5, max(1, value))


class Judge:
    def __init__(
        self,
        config: JudgeConfig,
        cassette: Cassette,
        *,
        base_url: str,
        timeout: float,
        mode: str,
    ) -> None:
        self._config = config
        self._cassette = cassette
        self._mode = mode
        self._client = build_ollama_client(base_url, timeout)
        self.calls = 0
        self.cached_calls = 0
        self.failures = 0
        self.latency_ms: list[float] = []

    @property
    def model(self) -> str:
        return f"ollama/{self._config.model}"

    async def _ask(self, call: str, system: str, user: str, schema: type[BaseModel]) -> Any:
        num_ctx = _context_for(system, user, self._config.num_ctx, _MAX_OUTPUT_TOKENS)
        if num_ctx is None:
            self.failures += 1
            logger.warning("judge call %s skipped: prompt over %d tokens", call, _CTX_CEILING)
            return None
        # A larger window joins the key, so answers given on a truncated prompt are re-asked.
        widened = () if num_ctx == self._config.num_ctx else (num_ctx,)
        key = stable_key("judge", self._config.model, call, system, user, *widened)
        if self._mode != "live":
            hit = self._cassette.get("judge", key)
            if hit is not None:
                self.cached_calls += 1
                return schema.model_validate(hit[0])
        if self._mode == "replay":
            return None
        started = time.monotonic()
        self.calls += 1
        try:
            result = await structured_chat(
                self._client,
                call=call,
                model=self._config.model,
                system=system,
                user=user,
                output_format=schema,
                num_ctx=num_ctx,
                max_tokens=_MAX_OUTPUT_TOKENS,
                temperature=0.0,
            )
        except LLMError as exc:
            self.failures += 1
            logger.warning("judge call %s failed: %s", call, exc)
            return None
        latency = (time.monotonic() - started) * 1000
        self.latency_ms.append(latency)
        self._cassette.put("judge", key, result.output.model_dump(mode="json"), latency)
        return result.output

    async def check_citation(
        self, statement: str, sources: list[dict[str, Any]]
    ) -> CitationJudgement | None:
        """``sources``: dicts with handle, title, study_type, year, text."""
        blocks = "\n\n".join(
            f"[{source['handle']}] {source['title']} ({source['study_type']}, "
            f"{source.get('year') or 'year unknown'})\n{source['text']}"
            for source in sources
        )
        user = f"STATEMENT:\n{statement}\n\nSOURCE TEXT:\n{blocks}"
        result: CitationJudgement | None = await self._ask(
            "judge_citation", CITATION_SYSTEM, user, CitationJudgement
        )
        return result

    async def assess_answer(
        self,
        *,
        question: str,
        article: str,
        sources: list[dict[str, Any]],
        expected_claims: list[str],
        forbidden_claims: list[str],
    ) -> AnswerJudgement | None:
        source_lines = (
            "\n".join(
                f"- [{source['handle']}] {source['title']} ({source['study_type']}, "
                f"{source.get('year') or 'year unknown'})"
                for source in sources
            )
            or "- (none: the article was written without sources)"
        )
        expected = "\n".join(f"{i}. {claim}" for i, claim in enumerate(expected_claims, 1))
        forbidden = "\n".join(f"{i}. {claim}" for i, claim in enumerate(forbidden_claims, 1))
        user = (
            f"QUESTION:\n{question}\n\nARTICLE:\n{article}\n\nSOURCES:\n{source_lines}\n\n"
            f"EXPECTED CLAIMS:\n{expected or '(none)'}\n\n"
            f"FORBIDDEN CLAIMS:\n{forbidden or '(none)'}"
        )
        result: AnswerJudgement | None = await self._ask(
            "judge_answer", ANSWER_SYSTEM, user, AnswerJudgement
        )
        return result


def judge_unavailable_reason(
    config: JudgeConfig, *, generator_models: list[str], installed: set[str] | None
) -> str | None:
    """Why the judge cannot run, or None. Checked once per evaluation run."""
    if not config.enabled or config.provider == "none":
        return "judge disabled in config.yaml"
    if config.model in generator_models and not config.allow_same_model_as_generator:
        return (
            f"judge model {config.model!r} is the model that wrote the articles; refusing "
            "to let a model grade its own output (allow_same_model_as_generator overrides this)"
        )
    if installed is not None and not (
        config.model in installed or f"{config.model}:latest" in installed
    ):
        return f"judge model {config.model!r} is not installed in ollama"
    return None
