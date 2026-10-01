"""MySQL checkpoint source of truth with transaction-aware Redis cache."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import AgentCheckpoint


logger = logging.getLogger(__name__)
CACHE_TTL = 7 * 86400
_ACTIONS = "checkpoint_after_commit_actions"
_HOOKS = "checkpoint_hooks_installed"

TASK_STAGES = frozenset({
    "QUEUED", "CONSUMING", "VIDEO_CONTEXT", "CONTEXT_COMPLETED", "CHUNKS_COMPLETED",
    "RETRIEVAL", "AGENT_LOOP", "PLAN_COMPLETED", "EXECUTOR_STARTED", "EXECUTOR_COMPLETED",
    "CRITIC_STARTED", "CRITIC_PASSED", "CRITIC_RETRY_REQUIRED", "EVIDENCE_REFRESHED",
    "ANALYSIS_COMPLETED", "ANALYSIS_COMPLETED_WITH_WARNINGS", "BUDGET_EXHAUSTED",
    "RETRYING", "COMPLETED", "COMPLETED_REUSED", "FAILED", "DEAD_LETTERED",
    "MANUAL_REPLAY", "REVISION_PENDING", "REVISION_APPLIED", "TRANSCRIPTION", "ASR",
    "DISPATCH_FAILED",
})


def _clear_pending(session: Session) -> None:
    if session.in_nested_transaction():
        return
    session.info.pop(_ACTIONS, None)


def _discard_rolled_back_savepoint(session: Session, transaction) -> None:
    if not transaction.nested:
        session.info.pop(_ACTIONS, None)
        return

    def was_in_rolled_back_scope(origin) -> bool:
        while origin is not None:
            if origin is transaction:
                return True
            origin = origin.parent
        return False

    session.info[_ACTIONS] = [
        entry for entry in session.info.get(_ACTIONS, [])
        if not was_in_rolled_back_scope(entry[0])
    ]


def _publish_after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    actions = session.info.pop(_ACTIONS, [])
    for _origin, _key, action in actions:
        try:
            action()
        except Exception:
            logger.warning("checkpoint_cache_after_commit_failed", exc_info=True)


def _install_hooks(session: Session) -> None:
    if session.info.get(_HOOKS):
        return
    event.listen(session, "after_commit", _publish_after_commit)
    event.listen(session, "after_rollback", _clear_pending)
    event.listen(session, "after_soft_rollback", _discard_rolled_back_savepoint)
    session.info[_HOOKS] = True


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return _camelize(asdict(value))
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot checkpoint {type(value).__name__}")


def _camelize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key): _camelize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


def _json_payload(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _stage_name(stage: str | Enum) -> str:
    return str(stage.value if isinstance(stage, Enum) else stage)


class AgentCheckpointRepository:
    def __init__(self, session: Session, redis_client):
        self.session = session
        self.redis = redis_client
        _install_hooks(session)

    def after_commit(self, action: Callable[[], None], cache_key: str | None = None) -> None:
        self.session.info.setdefault(_ACTIONS, []).append(
            (self.session.get_nested_transaction(), cache_key, action)
        )

    def _is_dirty(self, cache_key: str) -> bool:
        return any(key == cache_key for _origin, key, _action in self.session.info.get(_ACTIONS, []))

    def find_payload(self, media_id: int, checkpoint_name: str) -> str | None:
        return self.session.scalar(
            select(AgentCheckpoint.payload).where(
                AgentCheckpoint.media_id == media_id,
                AgentCheckpoint.checkpoint_key == checkpoint_name,
            )
        )

    def find_stage(self, media_id: int, checkpoint_name: str) -> str | None:
        return self.session.scalar(
            select(AgentCheckpoint.stage).where(
                AgentCheckpoint.media_id == media_id,
                AgentCheckpoint.checkpoint_key == checkpoint_name,
            )
        )

    def upsert(self, media_id: int, checkpoint_name: str, stage: str, payload: str | None) -> None:
        values = {
            "media_id": media_id, "checkpoint_key": checkpoint_name,
            "stage": stage, "payload": payload,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "mysql":
            statement = mysql_insert(AgentCheckpoint).values(**values).on_duplicate_key_update(
                stage=stage, payload=payload, updated_at=text("CURRENT_TIMESTAMP(3)"),
            )
        elif dialect == "sqlite":
            statement = sqlite_insert(AgentCheckpoint).values(**values).on_conflict_do_update(
                index_elements=["media_id", "checkpoint_key"],
                set_={"stage": stage, "payload": payload, "updated_at": text("CURRENT_TIMESTAMP")},
            )
        else:
            raise RuntimeError(f"unsupported checkpoint database dialect: {dialect}")
        self.session.execute(statement)

    def _cache_field(self, cache_key: str, field: str, payload: str, stage: str) -> None:
        try:
            self.redis.hset(cache_key, mapping={field: payload, "stage": stage})
            self.redis.expire(cache_key, CACHE_TTL)
        except Exception:
            logger.warning("checkpoint_cache_write_failed key=%s field=%s", cache_key, field, exc_info=True)
            try:
                self.redis.hdel(cache_key, field, "stage")
            except Exception:
                pass

    def _cache_stage(self, cache_key: str, stage: str) -> None:
        try:
            self.redis.hset(cache_key, "stage", stage)
            self.redis.expire(cache_key, CACHE_TTL)
        except Exception:
            logger.warning("checkpoint_stage_cache_write_failed key=%s", cache_key, exc_info=True)
            try:
                self.redis.hdel(cache_key, "stage")
            except Exception:
                pass

    def read(
        self, media_id: int, checkpoint_name: str, cache_key: str, field: str,
        validator: Callable[[Any], None] | None = None,
    ) -> Any | None:
        def restore(payload: str) -> Any:
            value = json.loads(payload)
            if validator is not None:
                validator(value)
            return value

        if not self._is_dirty(cache_key):
            try:
                cached = self.redis.hget(cache_key, field)
                if cached is not None:
                    try:
                        return restore(cached)
                    except Exception:
                        logger.warning("checkpoint_cache_deserialize_failed key=%s field=%s", cache_key, field, exc_info=True)
                        self.redis.hdel(cache_key, field)
            except Exception:
                logger.warning("checkpoint_cache_read_failed key=%s field=%s", cache_key, field, exc_info=True)
        payload = self.find_payload(media_id, checkpoint_name)
        if payload is None:
            return None
        try:
            restored = restore(payload)
        except Exception as exc:
            raise RuntimeError(f"读取 Agent Checkpoint 失败: {checkpoint_name}") from exc
        if not self._is_dirty(cache_key):
            self._cache_field(cache_key, field, payload, self.find_stage(media_id, checkpoint_name))
        return restored

    def read_stage(self, media_id: int, checkpoint_name: str, cache_key: str) -> str | None:
        if not self._is_dirty(cache_key):
            try:
                cached = self.redis.hget(cache_key, "stage")
                if cached in TASK_STAGES:
                    return cached
                if cached is not None:
                    self.redis.hdel(cache_key, "stage")
            except Exception:
                logger.warning("checkpoint_stage_cache_read_failed key=%s", cache_key, exc_info=True)
        stage = self.find_stage(media_id, checkpoint_name)
        if stage in TASK_STAGES and not self._is_dirty(cache_key):
            self._cache_stage(cache_key, stage)
        return stage if stage in TASK_STAGES else None

    def write(
        self, media_id: int, checkpoint_name: str, stage_checkpoint_name: str,
        cache_key: str, field: str, stage: str | Enum, value: Any,
    ) -> None:
        payload = _json_payload(value)
        stage_name = _stage_name(stage)
        self.upsert(media_id, checkpoint_name, stage_name, payload)
        self.upsert(media_id, stage_checkpoint_name, stage_name, None)
        self.after_commit(lambda: self._cache_field(cache_key, field, payload, stage_name), cache_key)

    def write_standalone(
        self, media_id: int, checkpoint_name: str, cache_key: str,
        field: str, stage: str | Enum, value: Any,
    ) -> None:
        payload = _json_payload(value)
        stage_name = _stage_name(stage)
        self.upsert(media_id, checkpoint_name, stage_name, payload)
        self.after_commit(lambda: self._cache_field(cache_key, field, payload, stage_name), cache_key)

    def write_stage(self, media_id: int, checkpoint_name: str, cache_key: str, stage: str | Enum) -> None:
        stage_name = _stage_name(stage)
        self.upsert(media_id, checkpoint_name, stage_name, None)
        self.after_commit(lambda: self._cache_stage(cache_key, stage_name), cache_key)

    def delete_by_prefix(self, media_id: int, checkpoint_prefix: str) -> None:
        self.session.execute(delete(AgentCheckpoint).where(
            AgentCheckpoint.media_id == media_id,
            AgentCheckpoint.checkpoint_key.like(checkpoint_prefix + "%"),
        ))

    def delete(self, media_id: int, checkpoint_name: str, cache_key: str) -> None:
        self.session.execute(delete(AgentCheckpoint).where(
            AgentCheckpoint.media_id == media_id,
            AgentCheckpoint.checkpoint_key == checkpoint_name,
        ))
        self.after_commit(lambda: self.redis.delete(cache_key), cache_key)

    def delete_by_media_id(self, media_id: int) -> None:
        self.session.execute(delete(AgentCheckpoint).where(AgentCheckpoint.media_id == media_id))
        checkpoint_key = f"agent:checkpoint:{media_id}"

        def cleanup():
            try:
                goal_keys = self.redis.smembers(checkpoint_key + ":goals") or set()
                keys = [checkpoint_key, f"agent:feedback:{media_id}", checkpoint_key + ":goals", *goal_keys]
                self.redis.delete(*keys)
            except Exception:
                logger.warning("checkpoint_media_cache_cleanup_failed mediaId=%s", media_id, exc_info=True)

        self.after_commit(cleanup, checkpoint_key)
