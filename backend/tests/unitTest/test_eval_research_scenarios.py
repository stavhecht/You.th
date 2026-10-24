"""The eval's research-agent scenarios still run against the production graph.

They crashed on import for eleven days after the agent split into trend and
evidence halves (``ResearchDeps`` was gone), and nothing in ``pytest`` noticed:
the failure suite was the only caller. Scripted, so this needs no network.
"""

from __future__ import annotations

import pytest

from evaluation.harness.research import SCENARIOS, run_scenario
from evaluation.schema import Outcome

_FAILS = {"all_science_down", "trends_down"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_each_scenario_ends_the_way_its_case_expects(name: str) -> None:
    trace = await run_scenario(f"test_{name}", name)

    expected = Outcome.RESEARCH_FAILED if name in _FAILS else Outcome.RESEARCH_COMPLETED
    assert trace.outcome == expected, (trace.error, (trace.research or {}).get("failed"))
    research = trace.research or {}
    assert all(check["passed"] for check in research["checks"])
    if research["fallback"] is not None:
        assert research["fallback"]["passed"], research["fallback"]["detail"]
