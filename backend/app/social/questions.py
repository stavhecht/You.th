"""The question a proposed topic's article would answer, written from its posts.

"Smart ring for sleep" names two things and asks nothing. The posts behind it
already ask: "Is the Oura Ring 5 worth it?", "This smart ring caught my sleep
apnea". It also searches better: on 2026-11-05 extraction kept that label whole
as the product, a quoted phrase PubMed will not match, and gave "smart rings"
for the question.

One call per weekly run, for the proposed topics that have no question yet, so a
topic is asked about once. A failure costs the question, never the run: the desk
and a promotion fall back to the label.

The output models have **no docstrings**: Pydantic would send them to the model
as part of the grammar.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.models import SocialPost, SocialPostTopic, SocialTopic
from app.intel.reader import StructuredCall, local_call
from app.research.normalize import normalize_query, singular
from app.research.trends.documents import NEAR_DUPLICATE_JACCARD, jaccard
from app.social.contracts import CollectionError, CollectionStatus, SourceLog

logger = logging.getLogger(__name__)

TITLES_PER_TOPIC = 8
MAX_WORDS = 14
MAX_TOKENS = 1_000
#: The first word of a question that can be answered yes or no.
_YES_NO_OPENERS = frozenset({"do", "does", "is", "are", "can", "will", "should"})

#: What a question says around its subject and claim. Singular like the words it is
#: taken from: "does" reads "doe" there, and stayed in as if it were content.
_FILLER_WORDS = {"a", "an", "the", "really", "actually", "truly", "accurately"}
_WORDING = frozenset(singular(word) for word in _YES_NO_OPENERS | _FILLER_WORDS)

QUESTION_SYSTEM_PROMPT = """\
You turn what people online are saying into the question an evidence-checking \
article would answer.
For each topic you get its name and the titles of real posts about it. Write one \
question for each.
Rules:
- A yes/no question an everyday reader would ask, at most 12 words, ending in "?".
- It names the thing and what it is said to do, as the posts claim it. The generic \
thing, not one brand.
- Published studies could help answer it.
- Write only what the titles support. If the titles are about unrelated things, \
write about the most common one.
Examples of the shape: "Does foam rolling really speed up recovery?" "Do standing \
desks actually help back pain?" "Is collagen powder worth taking for joints?"
Answer with one item per topic, using its id."""


class TopicQuestion(BaseModel):
    id: int
    question: str


class QuestionsOutput(BaseModel):
    items: list[TopicQuestion] = Field(min_length=1)


QuestionCall = StructuredCall[QuestionsOutput]


def build_question_call(settings: Settings) -> QuestionCall | None:
    return local_call(settings, "topic questions", QuestionsOutput, max_tokens=MAX_TOKENS)


def _words(text: str) -> list[str]:
    return [singular(word) for word in normalize_query(text).split()]


def same_question(left: str, right: str) -> bool:
    """Two topics asking one thing, by their words without the wording around them.
    "Do smart rings accurately detect sleep apnea?" and "Can smart rings detect
    sleep apnea?" are one article; "... help sleep?" about creatine and about
    magnesium stay two, at the bar the trend search uses for near-duplicate headlines."""
    return jaccard(_content_words(left), _content_words(right)) >= NEAR_DUPLICATE_JACCARD


def _content_words(question: str) -> frozenset[str]:
    return frozenset(_words(question)) - _WORDING


def usable_question(question: str, subject: str) -> bool:
    """A short yes/no question about this topic's subject. Pure."""
    words = _words(question)
    return (
        question.strip().endswith("?")
        and 3 <= len(words) <= MAX_WORDS
        and question.split()[0].lower() in _YES_NO_OPENERS
        and bool(set(words) & set(_words(subject)))
    )


async def write_questions(
    session: AsyncSession, call: QuestionCall | None, topics: list[SocialTopic]
) -> SourceLog:
    """Set ``question`` on the topics missing one. The caller commits."""
    if call is None:
        return SourceLog(CollectionStatus.NOT_CONFIGURED, "no local model (LLM_PROVIDER)")
    wanted = {index: t for index, t in enumerate(topics, start=1) if t.question is None}
    log = SourceLog(CollectionStatus.EMPTY, "not asked: every topic has a question")
    if not wanted:
        return log
    blocks = []
    for index, topic in wanted.items():
        titles = (
            await session.execute(
                select(SocialPost.title)
                .join(SocialPostTopic, SocialPostTopic.post_id == SocialPost.id)
                .where(SocialPostTopic.topic_id == topic.id)
                .order_by(SocialPost.published_at.desc())
                .limit(TITLES_PER_TOPIC)
            )
        ).scalars()
        listed = "\n".join(f"- {title[:110]}" for title in titles)
        blocks.append(f"[{index}] {topic.label}\n{listed}")
    try:
        output = await call(QUESTION_SYSTEM_PROMPT, "\n\n".join(blocks))
    except Exception as exc:  # the label still works; a model outage costs only this
        logger.warning("topic questions failed: %s", exc)
        log.failed(CollectionError(CollectionStatus.ERROR, str(exc)[:300]), "topic questions")
        return log
    written = 0
    for item in output.items:
        asked = wanted.get(item.id)
        if asked is not None and usable_question(item.question, asked.subject):
            asked.question = item.question.strip()
            written += 1
    log.ok(written)
    return log
