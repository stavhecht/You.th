"""The Trend Search Agent stays out of scientific evidence and article generation.

``app/research/trends`` finds and scores trends. Literature retrieval,
evidence evaluation and the article pipeline belong downstream, and this test
fails the day a module here imports one of them directly. Transitive imports
are not checked — the HTTP throttle imports retrieval's base types, and the
triage client imports the LLM layer's — only what this package reaches for
itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

TRENDS = Path(__file__).resolve().parents[2] / "app" / "research" / "trends"

FORBIDDEN = (
    "app.research.providers.scientific",
    "app.research.evidence",
    "app.retrieval.factory",
    "app.retrieval.pubmed",
    "app.retrieval.europe_pmc",
    "app.retrieval.providers",
    "app.retrieval.query_builder",
    "app.retrieval.cache",
    "app.pipeline",
    "app.evidence",
    "app.domain.contracts",
    "app.llm.factory",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_the_trend_package_imports_no_science_or_generation_code() -> None:
    files = sorted(TRENDS.rglob("*.py"))
    assert files, "the trend package moved; update this test"
    offences = [
        f"{path.relative_to(TRENDS)}: {module}"
        for path in files
        for module in _imports(path)
        if any(module == banned or module.startswith(banned + ".") for banned in FORBIDDEN)
    ]
    assert offences == []


def test_the_trend_score_has_no_evidence_dimension() -> None:
    from app.research.trends.scoring import DIMENSIONS, SUB_SIGNAL_NAMES

    assert not [name for name in (*DIMENSIONS, *SUB_SIGNAL_NAMES) if "evidence" in name]
