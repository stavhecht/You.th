"""The one model call social listening and product discovery share: read short texts.

A video title, a headline, a feed summary: what wellness topic it is about, and
which specific products it names. One pass serves both modules, so a text is
read once however many of them want it.

The local model is the bottleneck (minutes per batch on llama-server), so:

* **Cached by content.** A reading is stored under sha256(``READER_VERSION`` +
  text). An unchanged post or feed item is never read twice; a prompt change
  bumps the version and re-reads only what is asked for again.
* **Batched and capped.** ``BATCH`` texts per call, at most ``max_batches``
  calls per run. What is left over waits for the next run and is reported.
* **Checked, never trusted.** An id must be one we sent. A topic's subject must
  share a word with the text, and a product's quote must be copied from it,
  brand included — the model reads the text, it never adds to it. A text the
  model skipped stays unread and is asked again next run.

Like ``research/triage.py``, the model decides what a text *means*. It never
decides whether something is trending, popular or true.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.models import TextReading
from app.llm.base import LLMError
from app.products.contracts import CATEGORY_INFO, MentionKind, ProductCategory
from app.research.categories import Category
from app.research.normalize import normalize_query, singular
from app.research.trends.metrics import is_wellness_text
from app.social.contracts import CollectionStatus, SourceLog

logger = logging.getLogger(__name__)

READER_VERSION = "reader-v1"
BATCH = 20
MAX_TOKENS = 3_000
NUM_CTX = 8_192
#: Characters of each text the model sees. Titles and the top of a description
#: carry the topic; the rest is links and sponsor copy.
TEXT_CHARS = 500


class ProductMentionOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"description": "One specific commercial product the text names."}
    )

    brand: str = Field(min_length=1, max_length=40, description='The maker, e.g. "Oura".')
    product_name: str = Field(
        min_length=1, max_length=60, description='The product, e.g. "Oura Ring 4".'
    )
    category: ProductCategory
    mention_kind: MentionKind
    quote: str = Field(
        min_length=2, max_length=200, description="The exact words from the text naming it."
    )


class TextReadingOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "What one text is about."})

    id: int
    is_wellness_topic: bool = Field(
        description="True only for a wellness, fitness, nutrition, sleep or health question "
        "a science-based article could address."
    )
    subject: str = Field(
        default="",
        max_length=60,
        description='The substance, food, activity, practice or device, e.g. "creatine".',
    )
    outcome: str = Field(
        default="", max_length=60, description='What it is sought for, e.g. "sleep".'
    )
    category: Category
    products: list[ProductMentionOut] = Field(default_factory=list, max_length=5)


class ReaderOutput(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "One reading per text."})

    items: list[TextReadingOut]


READER_SYSTEM_PROMPT = """\
You read short texts for a science-based wellness publication: video titles and \
descriptions, news headlines and feed summaries. Each text has an id. For every id:

- is_wellness_topic: true only if the text is about a wellness, fitness, nutrition, \
sleep or health question a science-based article could address. Entertainment, \
celebrity news, politics, shopping deals and giveaways are false.
- subject: what the text is about, in one to four plain words taken from the text: a \
substance, food, activity, practice or device ("creatine", "cold plunge", "smart ring").
- outcome: what it is sought for ("sleep", "muscle recovery"), or empty.
- category: the closest category.
- products: each specific commercial product the text names by brand and product name \
("Oura" / "Oura Ring 4"). A generic device ("a smart ring"), a brand on its own, or a \
generic supplement is not a product. quote: copy the exact words of the text that name \
it. mention_kind: launch if the text says it was just launched or released; \
announcement if announced or coming soon; review if the text reviews or tests it; \
comparison if it is compared with others; mention otherwise.

Answer for every id. Never name a product the text does not name.\
"""

type StructuredCall[T] = Callable[[str, str], Awaitable[T]]
ReadCall = StructuredCall[ReaderOutput]


@dataclass
class ReadResult:
    #: Text id -> validated reading: {"topic": {...} | None, "products": [...]}.
    readings: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: int = 0
    failed_batches: int = 0
    #: Texts that wanted reading and did not get it this run.
    backlog: int = 0

    def log(self, *, configured: bool) -> SourceLog:
        """The reader as a collection source, for the run's status."""
        if not configured:
            return SourceLog(CollectionStatus.NOT_CONFIGURED, "no local model (LLM_PROVIDER)")
        log = SourceLog(CollectionStatus.SUCCESS, "no new texts to read; cached readings used")
        log.calls, log.items = self.batches, len(self.readings)
        log.failures = [
            {"call": "reader batch", "status": "ERROR", "detail": "batch failed"}
        ] * self.failed_batches
        return log


def text_key(text: str) -> str:
    return hashlib.sha256(f"{READER_VERSION}\n{text}".encode()).hexdigest()


async def read_texts(
    session: AsyncSession,
    call: ReadCall | None,
    texts: dict[str, str],
    *,
    max_batches: int,
) -> ReadResult:
    """Readings for ``texts`` (id -> text): cached ones, then up to
    ``max_batches`` new calls. Commits after each batch: a batch is minutes of
    model time, and a restart must not lose the ones already paid for."""
    result = ReadResult()
    keys = {text_id: text_key(text) for text_id, text in texts.items()}
    cached: dict[str, dict[str, Any]] = {}
    if keys:
        rows = await session.execute(
            select(TextReading.text_key, TextReading.reading).where(
                TextReading.text_key.in_(set(keys.values()))
            )
        )
        cached = {key: reading for key, reading in rows.all()}
    todo = []
    for text_id, key in keys.items():
        if key in cached:
            result.readings[text_id] = cached[key]
        else:
            todo.append(text_id)

    if call is None:
        result.backlog = len(todo)
        return result
    for start in range(0, len(todo), BATCH):
        if result.batches >= max_batches:
            result.backlog += len(todo) - start
            break
        batch = todo[start : start + BATCH]
        numbered = {index: text_id for index, text_id in enumerate(batch, start=1)}
        user = batch_prompt({index: texts[text_id] for index, text_id in numbered.items()})
        result.batches += 1
        try:
            output = await call(READER_SYSTEM_PROMPT, user)
        except LLMError as exc:
            result.failed_batches += 1
            result.backlog += len(batch)
            logger.warning("reader batch failed: %s", exc)
            continue
        answered = validate(output, {i: texts[t] for i, t in numbered.items()})
        result.backlog += len(batch) - len(answered)
        for index, reading in answered.items():
            text_id = numbered[index]
            result.readings[text_id] = reading
            await session.execute(
                insert(TextReading)
                .values(text_key=keys[text_id], reading=reading)
                .on_conflict_do_nothing(index_elements=["text_key"])
            )
        await session.commit()
    return result


def batch_prompt(texts: dict[int, str]) -> str:
    """The user turn for one batch: ``[id] text`` per line."""
    return "\n".join(f"[{index}] {_clip(text)}" for index, text in texts.items())


def validate(output: ReaderOutput, sent: dict[int, str]) -> dict[int, dict[str, Any]]:
    """The checked readings, by the id we sent. Pure."""
    readings: dict[int, dict[str, Any]] = {}
    for item in output.items:
        if item.id not in sent or item.id in readings:
            continue
        text = _clip(sent[item.id])
        words = _words(text)
        topic = None
        subject = item.subject.strip()
        if item.is_wellness_topic and subject and _words(subject) & words:
            topic = {
                "subject": subject.lower(),
                "outcome": item.outcome.strip().lower(),
                "category": item.category.value,
            }
        products = [
            {
                "brand": p.brand.strip(),
                "product_name": p.product_name.strip(),
                "category": p.category.value,
                "mention_kind": p.mention_kind.value,
                "quote": p.quote.strip(),
            }
            for p in item.products
            if _quoted(p.quote, text) and _words(p.brand) & _words(p.quote)
        ]
        readings[item.id] = {"topic": topic, "products": products}
    return readings


def local_call[T: BaseModel](
    settings: Settings, call: str, output_format: type[T], *, max_tokens: int
) -> StructuredCall[T] | None:
    """A structured call on whichever local server the pipeline uses; None on the
    hosted path (the work is then left undone and the run says so)."""
    provider = settings.llm_provider.lower()
    if provider == "ollama":
        from app.llm.ollama_client import build_ollama_client, structured_chat

        client = build_ollama_client(settings.ollama_base_url, settings.ollama_timeout_seconds)

        async def ollama_call(system: str, user: str) -> T:
            result = await structured_chat(
                client,
                call=call,
                model=settings.ollama_synthesis_model,
                system=system,
                user=user,
                output_format=output_format,
                num_ctx=NUM_CTX,
                max_tokens=max_tokens,
            )
            output: T = result.output
            return output

        return ollama_call
    if provider == "llamacpp":
        from app.llm.factory import llamacpp_chat

        chat = llamacpp_chat(settings)

        async def llamacpp_call(system: str, user: str) -> T:
            result = await chat.chat(
                call=call,
                system=system,
                user=user,
                output_format=output_format,
                max_tokens=max_tokens,
                temperature=0.0,
            )
            output: T = result.output
            return output

        return llamacpp_call
    return None


def build_reader_call(settings: Settings) -> ReadCall | None:
    return local_call(settings, "text reading", ReaderOutput, max_tokens=MAX_TOKENS)


_PRODUCT_HINTS = frozenset(
    {
        singular(w)
        for phrase, _ in CATEGORY_INFO.values()
        for w in normalize_query(phrase).split()
    }
    | {"wearable", "gadget", "tracker", "monitor", "launch", "unveil", "release"}
) - {"device", "light", "smart"}  # too common alone; "led" and "therapy" still match


def worth_reading(text: str) -> bool:
    """A cheap word filter before the model: wellness words or product words.
    What it drops is never sent; it is a cost cut, not a judgement."""
    return is_wellness_text(text) or bool(_words(text) & _PRODUCT_HINTS)


def _clip(text: str) -> str:
    return " ".join(text.split())[:TEXT_CHARS]


def _words(text: str) -> set[str]:
    return {singular(w) for w in normalize_query(text).split() if len(w) > 1}


def _quoted(quote: str, text: str) -> bool:
    return normalize_query(quote) in normalize_query(text)
