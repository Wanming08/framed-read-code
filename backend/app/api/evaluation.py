"""Read-only evaluation endpoint from AnalysisController.java."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.analysis_status import _normalized_goal
from app.api.dependencies import get_current_user, get_redis, get_session
from app.api.media import get_media_service
from app.api.responses import ok
from app.db.checkpoints import AgentCheckpointRepository
from app.schemas.mode import AnalysisMode
from app.services.checkpoints import AgentCheckpointService
from app.services.evaluation import AgentEvaluationService
from app.services.evidence import EvidenceVerificationService
from app.services.media import MediaService


router = APIRouter(prefix="/analysis")


def get_evaluation_media(media: MediaService = Depends(get_media_service)) -> MediaService:
    return media


def get_evaluation_service(
    session: Session = Depends(get_session), redis_client=Depends(get_redis),
) -> AgentEvaluationService:
    checkpoints = AgentCheckpointService(AgentCheckpointRepository(session, redis_client), redis_client)
    return AgentEvaluationService(checkpoints, EvidenceVerificationService())


@router.get("/agent-evaluation")
def agent_evaluation(
    id: int = Query(...), goal: str = Query(...), mode: str | None = Query(default=None),
    user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_evaluation_media),
    evaluation: AgentEvaluationService = Depends(get_evaluation_service),
):
    media.require_owned_media(id, user_id)
    return ok(evaluation.evaluate(id, _normalized_goal(goal), AnalysisMode.from_request(mode)))
