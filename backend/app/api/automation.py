"""Machine-to-machine endpoints for n8n. Shared-secret auth, router-level.

n8n owns *when* a research run happens (its weekly schedule, or its manual
webhook); this router lets it start one and poll it. It calls the same
``start_run`` the desk's button does — the research logic lives in
``app/research`` and nowhere in n8n.

Authentication is a static token in ``X-Trigger-Token``, compared in constant
time. **With ``RESEARCH_TRIGGER_TOKEN`` unset the whole router answers 404**,
so a deployment that does not use n8n exposes nothing here. The token cannot
reach reviewer or reader routes: those check their own bearer JWTs, and this
dependency is attached to this router only.

What the token grants is deliberately small: starting a research run (which
proposes and never enqueues generation), reading research runs and their trend
results. No article, reviewer or reader data is reachable with it.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.console.research import read_result, research_run_out, start_run
from app.api.console.schemas import AutomationResearchRequest, ResearchRunOut
from app.api.deps import IdPath, SessionDep, SettingsDep
from app.domain.models import ResearchRun
from app.research import service
from app.research.trends.contracts import TrendSearchResult


async def require_trigger_token(
    settings: SettingsDep,
    x_trigger_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.research_trigger_token
    if not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if x_trigger_token is None or not secrets.compare_digest(
        x_trigger_token.encode(), expected.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid trigger token")


router = APIRouter(
    prefix="/automation",
    dependencies=[Depends(require_trigger_token)],
    tags=["automation"],
)


@router.post(
    "/research/runs", response_model=ResearchRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def trigger_research_run(
    session: SessionDep,
    settings: SettingsDep,
    payload: AutomationResearchRequest | None = None,
) -> ResearchRunOut:
    """Queue a research run. ``mode`` is ``weekly`` unless the caller says otherwise.

    409 while another research run is queued or running — n8n's weekly slot
    firing during a manual run is refused, not stacked.
    """
    body = payload or AutomationResearchRequest()
    return await start_run(
        session, mode=body.mode, payload=body, requested_by=None, settings=settings
    )


@router.get("/research/runs/{run_id}", response_model=ResearchRunOut)
async def read_research_run(run_id: IdPath, session: SessionDep) -> ResearchRunOut:
    """Status for n8n's polling loop, with the candidates once there are any."""
    run = await session.get(ResearchRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "research run not found")
    return research_run_out(run, await service.candidates_for(session, run_id))


@router.get("/research/runs/{run_id}/result", response_model=TrendSearchResult)
async def read_research_result(run_id: IdPath, session: SessionDep) -> TrendSearchResult:
    """The Trend Search Agent's output, for n8n to forward once a run finishes.

    snake_case on purpose (the agent's documented output schema), unlike the
    camelCase run record. Topics carry ``scientific_assessment:
    "not_assessed"`` and the evidence gate's outcome as a status — never an
    evidence score.
    """
    return await read_result(session, run_id)
