"""Best-effort AUTO mode classification from ModeRouter.java."""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.mode import AnalysisMode


LOG = logging.getLogger(__name__)
USER_ROUTES_PER_MINUTE = 10
GLOBAL_ROUTES_PER_MINUTE = 60
_NO_USER = object()

REASONS = {
    AnalysisMode.GENERAL: "目标较为通用,已选通用模式",
    AnalysisMode.LEARNING: "目标偏向知识梳理与复习,已选学习模式",
    AnalysisMode.REVIEW: "目标偏向查错与观点核验,已选审查模式",
    AnalysisMode.CREATION: "目标偏向内容再创作,已选创作模式",
}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


class ModeRouter:
    def __init__(self, model: Any, limiter: Any) -> None:
        self.model = model
        self.limiter = limiter

    def route(self, goal: str | None, user_id: int | None | object = _NO_USER) -> dict[str, str]:
        if user_id is not _NO_USER and not self._try_acquire_quota(user_id):
            return {"mode": AnalysisMode.GENERAL.value, "reason": "自动路由当前繁忙,已按通用模式分析"}
        if goal is None or not goal.strip():
            return {"mode": AnalysisMode.GENERAL.value, "reason": "未提供分析目标,已按通用模式分析"}
        try:
            classification = self.model.classify_mode(goal.strip())
            mode = AnalysisMode.from_nullable(_field(classification, "mode"))
            reason = _field(classification, "reason")
            return {"mode": mode.value, "reason": reason.strip() if reason and reason.strip() else REASONS[mode]}
        except Exception:
            LOG.warning("自动意图路由失败,回退 GENERAL。goalLength=%s", len(goal), exc_info=True)
            return {"mode": AnalysisMode.GENERAL.value, "reason": "意图识别暂不可用,已按通用模式分析"}

    def _try_acquire_quota(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        try:
            return self.limiter.try_acquire_pair(
                f"limit:ai:route:user:{user_id}", USER_ROUTES_PER_MINUTE,
                "limit:ai:route:global", GLOBAL_ROUTES_PER_MINUTE,
            )
        except Exception:
            LOG.warning("自动意图路由限流器不可用,回退 GENERAL。userId=%s", user_id, exc_info=True)
            return False
