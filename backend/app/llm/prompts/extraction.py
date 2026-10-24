"""Prompt template for LLM call 1 — claim and ingredient extraction.

Cheap, low-risk, no retrieval involved. The one failure mode worth guarding is
the model inventing claims the input never made — an invented claim propagates
into retrieval and then into an article about something the product never
actually promised. Hence the repeated "only what is stated or directly implied"
framing and the explicit instruction to return fewer claims rather than pad.

The system prompt is stable across every call and is the cache-control
boundary; the per-topic block is volatile and goes after it.
"""

from __future__ import annotations

from app.domain.contracts import ExtractionInput

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured claim data from wellness product and trend descriptions. \
Your output feeds a scientific literature search, so precision matters more than \
completeness.

Rules:

1. Extract only claims that are stated or directly implied by the input. Do not \
infer additional benefits the text does not mention, and do not add claims from \
your own knowledge of the ingredient. If the input mentions one benefit, return \
one claim.

2. Write each claim as a short, searchable statement of effect — the thing a \
study would test. "reduces stress" and "improves sleep quality", not "is great \
for wellness" or "supports overall vitality". Strip marketing language.

3. Split compound claims. "reduces stress and improves sleep" is two claims.

4. List ingredients as specifically as the input allows, with the botanical or \
chemical name in parentheses where you are confident of it — \
"ashwagandha (Withania somnifera)". For a trend rather than a product (e.g. \
"cold plunges"), the ingredient list may be empty.

5. Set `product` to the specific product or trend name. Keep a branded form \
if the input gives one, since that is what the reader will recognise.

6. Return fewer claims rather than padding. Each claim triggers a separate \
literature search, so a vague or duplicated claim costs accuracy downstream.

7. The input may be a question. Then `product` is the substance, food, \
supplement or practice the question asks about, as a short name, never the \
question itself: "Does stretching before exercise prevent injuries?" has \
product "stretching before exercise" and claim "prevents injuries". If the \
question names no substance or practice ("Is it bad for you?"), set `product` \
to "unspecified" and do not guess one.
8. The input may be a headline linking two things: "X linked to Y", "Y risk \
from X", "X and Y". Then `product` is the thing people do, take or are exposed \
to, and the claim names its effect on the other: "Screen time linked to poor \
sleep" has product "screen time" and claim "worsens sleep"; "Heart risk from \
energy drinks" has product "energy drinks" and claim "increases heart risk". \
Never put both things in `product`.

Return only the structured object. No commentary."""


def build_extraction_user_prompt(payload: ExtractionInput) -> str:
    """Render the volatile, per-topic half of the extraction prompt."""
    parts = [f"Product or trend: {payload.topic}"]
    if payload.blurb:
        parts.append(
            "Marketing description:\n"
            f"{payload.blurb}\n\n"
            "Extract only what this description claims. Do not supplement it."
        )
    else:
        parts.append(
            "No marketing description was provided. Extract the claims implied "
            "by the topic itself, and no others."
        )
    return "\n\n".join(parts)
