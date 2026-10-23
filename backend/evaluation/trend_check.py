"""Can a research run's trends be trusted? Checks one run's topics against the web.

    python -m evaluation.trend_check                 # the latest research run
    python -m evaluation.trend_check <run_id>

The quality suites score the *article* pipeline; nothing scored what the Trend
Search Agent hands a reviewer. Three questions per topic:

* **Real** — every supporting source's URL is fetched. ``verified`` means it
  answered 2xx and the page names the topic's subject (or carries the
  headline we stored); ``blocked`` is a 401/403/429/451 (the page exists, a
  bot wall stopped us); ``dead`` is a 404/410 or a host that did not resolve.
  A topic is *corroborated* when two or more distinct publishers verify.
* **Fresh** — the source's date falls inside the run's collection window
  (two days' slack either side for time zones and feed lag).
* **Wellness** — one structured call to the configured llama-server model
  labels the topic ``yes`` / ``borderline`` / ``no``. That model never saw
  these topics before (triage is skipped on llamacpp), but it is a model:
  read the reasons, they are in the report.

Read-only: nothing is written to the database. Needs llama-server free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.models import ResearchRun
from app.llm.factory import llamacpp_chat
from app.research import service
from app.research.trends.contracts import ResultTopic, SupportingSource

REPORTS = Path(__file__).parent / "reports"
DATASETS = Path(__file__).parent / "datasets"
FRESH_SLACK = timedelta(days=2)
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BLOCKED = {401, 403, 429, 451}
_STRIP = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_WORD = re.compile(r"[a-z0-9]{4,}")


class WellnessLabel(BaseModel):
    related: Literal["yes", "borderline", "no"]
    reason: str


_WELLNESS_SYSTEM = """\
You decide whether a trending topic belongs in a consumer wellness publication \
that checks health claims against research. It belongs ("yes") when it is about \
health, fitness, exercise, nutrition, diet, supplements, sleep, recovery, mental \
wellbeing or preventive health, and it asks something research could answer. \
"borderline" is a health-adjacent topic with no researchable question (a product \
launch, a celebrity's routine, a policy story). "no" is anything else. Give a \
one-sentence reason."""


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def page_text(html: str) -> str:
    return " ".join(_STRIP.sub(" ", html).split()).lower()


def judge_page(status: int | None, text: str, subject: str, title: str) -> str:
    """verified | unconfirmed | blocked | dead | error, from one fetch."""
    if status is None:
        return "dead"
    if status in _BLOCKED:
        return "blocked"
    if status in (404, 410):
        return "dead"
    if not 200 <= status < 300:
        return "error"
    on_page = words(text)
    subject_words = words(subject)
    title_words = words(title)
    names_subject = bool(subject_words) and subject_words <= on_page
    carries_title = bool(title_words) and len(title_words & on_page) >= 0.6 * len(title_words)
    return "verified" if names_subject or carries_title else "unconfirmed"


async def fetch(
    client: httpx.AsyncClient, gate: asyncio.Semaphore, url: str
) -> tuple[int | None, str, str]:
    async with gate:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return None, "", type(exc).__name__
        return response.status_code, page_text(response.text[:400_000]), str(response.url)


def fresh(source: SupportingSource, start: datetime, end: datetime) -> bool | None:
    if source.published_at is None:
        return None
    published = source.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return start - FRESH_SLACK <= published <= end + FRESH_SLACK


async def label_wellness(topic: ResultTopic) -> WellnessLabel | str:
    headlines = "\n".join(f"- {s.title}" for s in topic.supporting_sources[:5])
    user = (
        f"Topic: {topic.topic}\nCategory the agent chose: {topic.category}\n"
        f"Summary: {topic.summary}\nHeadlines behind it:\n{headlines}"
    )
    try:
        result = await llamacpp_chat(get_settings()).chat(
            call="trend wellness check",
            system=_WELLNESS_SYSTEM,
            user=user,
            output_format=WellnessLabel,
            max_tokens=300,
            temperature=0.0,
        )
    except Exception as exc:  # the label is advisory; a failed call is reported, not fatal
        return f"label failed: {exc}"
    return result.output


async def check(run_id: str | None) -> Path:
    factory = get_session_factory()
    async with factory() as session:
        run = (
            await session.get(ResearchRun, run_id)
            if run_id
            else (await service.list_runs(session, 1))[0]
        )
        if run is None:
            raise SystemExit(f"no research run {run_id}")
        result = service.build_result(run, await service.candidates_for(session, run.id))
    window = result.collection_window

    gate = asyncio.Semaphore(6)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20, headers={"User-Agent": _UA}
    ) as client:
        pages = {
            source.url: asyncio.create_task(fetch(client, gate, source.url))
            for topic in result.topics
            for source in topic.supporting_sources
        }
        await asyncio.gather(*pages.values())

    rows = []
    for topic in result.topics:
        sources = []
        for source in topic.supporting_sources:
            status, text, final = pages[source.url].result()
            sources.append(
                {
                    "title": source.title,
                    "url": source.url,
                    "publisher": source.publisher or urlparse(source.url).hostname,
                    "source": source.source_name,
                    "http": status,
                    "final_url": final,
                    "page": judge_page(status, text, topic.subject, source.title),
                    "fresh": fresh(source, window.start, window.end),
                }
            )
        verified_by = {s["publisher"] for s in sources if s["page"] == "verified"}
        label = await label_wellness(topic)
        rows.append(
            {
                "rank": topic.rank,
                "topic": topic.topic,
                "subject": topic.subject,
                "outcome": topic.outcome,
                "category": str(topic.category),
                "status": topic.status,
                "evidence_gate": topic.downstream.evidence_gate,
                "trend_score": topic.trend_score,
                "confidence": topic.confidence,
                "data_coverage": topic.data_coverage,
                "corroborated": len(verified_by) >= 2,
                "verified_publishers": sorted(verified_by),
                "wellness": label.related if isinstance(label, WellnessLabel) else "unknown",
                "wellness_reason": label.reason if isinstance(label, WellnessLabel) else label,
                "sources": sources,
            }
        )

    out = REPORTS / f"{datetime.now():%Y%m%d-%H%M%S}_trends"
    out.mkdir(parents=True)
    (out / "trends.json").write_text(
        json.dumps(
            {
                "run_id": result.run_id,
                "label": result.label,
                "status": result.status,
                "window": [window.start.isoformat(), window.end.isoformat()],
                "counts": result.counts,
                "sources": result.sources.model_dump(),
                "errors": result.errors,
                "topics": rows,
            },
            indent=2,
            default=str,
        )
    )
    (out / "report.md").write_text(render(result.label, rows))
    write_cases(rows)
    return out


def write_cases(rows: list[dict]) -> None:
    """The topics that passed the evidence gate, as eval cases (``--suite trends``).

    Expectations are only what the topic itself states: its subject must be
    extracted and its outcome's words claimed. No gold papers, so recall is N/A.
    """
    lines = [
        json.dumps(
            {
                "id": f"trend_{index:02d}",
                "category": "latest_research",
                "tags": ["trend"],
                "description": f"Trend topic, rank {row['rank']} ({row['category']})",
                "query": row["topic"],
                "expected_subject": [row["subject"]],
                "expected_outcome_terms": sorted(words(row["outcome"])),
            }
        )
        for index, row in enumerate((r for r in rows if r["evidence_gate"] == "passed"), 1)
    ]
    header = "// Written by evaluation/trend_check.py from the latest research run.\n"
    (DATASETS / "trends.jsonl").write_text(header + "\n".join(lines) + "\n")


def render(label: str, rows: list[dict]) -> str:
    links = [s for r in rows for s in r["sources"]]
    pages = Counter(s["page"] for s in links)
    dated = [s["fresh"] for s in links if s["fresh"] is not None]
    wellness = Counter(r["wellness"] for r in rows)
    lines = [
        f"# Trend check: {label}",
        "",
        f"- Topics: {len(rows)}; corroborated by 2+ publishers: "
        f"{sum(r['corroborated'] for r in rows)}",
        f"- Links: {len(links)}; " + ", ".join(f"{k} {v}" for k, v in pages.most_common()),
        f"- Fresh (inside the window): {sum(dated)}/{len(dated)} dated links",
        "- Wellness: " + ", ".join(f"{k} {v}" for k, v in wellness.most_common()),
        "",
        "| # | topic | status | gate | links ok/all | corroborated | wellness | reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ok = sum(s["page"] == "verified" for s in r["sources"])
        lines.append(
            f"| {r['rank']} | {r['topic']} | {r['status']} | {r['evidence_gate']} | "
            f"{ok}/{len(r['sources'])} | {'yes' if r['corroborated'] else 'no'} | "
            f"{r['wellness']} | {r['wellness_reason']} |"
        )
    lines += ["", "## Links that did not verify", ""]
    for r in rows:
        for s in r["sources"]:
            if s["page"] != "verified":
                lines.append(
                    f"- [{r['topic']}] {s['page']} (HTTP {s['http']}): {s['title']}"
                    f" <{s['url']}>"
                )
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args()
    try:
        print(await check(args.run_id))
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
