"""The reader model (``intel/reader.py``) on a small hand-labelled set.

    python -m evaluation.reader_check

One batch of 14 texts (``reader_cases.jsonl``), so a few minutes on the
local model, not hours. It makes **real model calls** on whichever server
``LLM_PROVIDER`` names. Reports, after the reader's own checks:

* wellness accuracy: is_wellness_topic against the label;
* subject hits: the kept topic's subject shares a word with the label's list;
* product precision and recall: a labelled product counts as found when all its
  words appear in a kept mention's brand + name;
* guard drops: products the model named that the quote check refused.

Labels are the texts' own words, chosen before the first run. Not part of
``evaluation.run``: the reader is not on the article pipeline's path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.intel.reader import READER_SYSTEM_PROMPT, batch_prompt, build_reader_call, validate
from app.research.normalize import normalize_query

HERE = Path(__file__).parent
DATASET = HERE / "reader_cases.jsonl"  # not datasets/: those are article cases


def _words(text: str) -> set[str]:
    return set(normalize_query(text).split())


def score(
    cases: list[dict[str, Any]], raw: Any, kept: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    raw_products = {item.id: len(item.products) for item in raw.items}
    wellness_right = subject_hits = subject_total = 0
    found = expected = predicted = correct = guard_drops = 0
    rows = []
    for index, case in enumerate(cases, start=1):
        reading = kept.get(index, {"topic": None, "products": []})
        topic = reading["topic"]
        wellness_right += (topic is not None) == case["wellness"]
        if case["wellness"]:
            subject_total += 1
            subject_hits += bool(topic and _words(topic["subject"]) & set(case["subject_any"]))
        mentions = [_words(f"{p['brand']} {p['product_name']}") for p in reading["products"]]
        hits = [
            label for label in case["products"] if any(_words(label) <= m for m in mentions)
        ]
        expected += len(case["products"])
        found += len(hits)
        predicted += len(mentions)
        correct += sum(
            1 for m in mentions if any(_words(label) <= m for label in case["products"])
        )
        guard_drops += max(0, raw_products.get(index, 0) - len(reading["products"]))
        rows.append(
            {
                "id": case["id"],
                "answered": index in kept,
                "topic": topic,
                "products": [
                    f"{p['brand']} / {p['product_name']}" for p in reading["products"]
                ],
                "missed": [p for p in case["products"] if p not in hits],
            }
        )
    return {
        "cases": len(cases),
        "answered": len(kept),
        "wellness_accuracy": round(wellness_right / len(cases), 3),
        "subject_hit_rate": round(subject_hits / subject_total, 3) if subject_total else None,
        "product_recall": round(found / expected, 3) if expected else None,
        "product_precision": round(correct / predicted, 3) if predicted else None,
        "guard_drops": guard_drops,
        "rows": rows,
    }


async def main() -> int:
    settings = get_settings()
    call = build_reader_call(settings)
    if call is None:
        print(
            "no local reader model (LLM_PROVIDER is hosted); nothing to check", file=sys.stderr
        )
        return 1
    cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    texts = {index: case["text"] for index, case in enumerate(cases, start=1)}
    started = time.monotonic()
    raw = await call(READER_SYSTEM_PROMPT, batch_prompt(texts))
    report = score(cases, raw, validate(raw, texts))
    report["seconds"] = round(time.monotonic() - started, 1)
    report["provider"] = settings.llm_provider
    for row in report["rows"]:
        print(
            f"{row['id']}: topic={row['topic'] and row['topic']['subject']!r} "
            f"products={row['products']} missed={row['missed']}"
        )
    summary = {k: v for k, v in report.items() if k != "rows"}
    print(json.dumps(summary, indent=2))
    out = HERE / "reports" / f"reader_{datetime.now(UTC):%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
