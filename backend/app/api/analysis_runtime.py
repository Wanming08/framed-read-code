"""Shared API resources with request-scoped analysis services and sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

import httpx
import redis

from app.agent.budget import AgentExecutionBudget
from app.agent.loop import AgentLoopService
from app.agent.modes import ModeRegistry
from app.agent.router import ModeRouter
from app.config import ConfigError, Settings
from app.db.checkpoints import AgentCheckpointRepository
from app.db.repositories.media import MediaRepository
from app.integrations.asr import ASRClient
from app.integrations.embeddings import EmbeddingClient
from app.integrations.ffmpeg import FfmpegTools
from app.integrations.llm import LLMClient
from app.integrations.object_storage import ObjectStorage
from app.integrations.ocr import OcrTools
from app.integrations.qdrant_store import QdrantStore
from app.integrations.rocketmq import RocketMQProducer
from app.services.analysis import AiService
from app.services.checkpoints import AgentCheckpointService
from app.services.chunking import VideoChunkingService
from app.services.dispatch import AnalysisDispatchService
from app.services.evidence import EvidenceVerificationService
from app.services.long_video_context import LongVideoContextService
from app.services.media import MediaService
from app.services.redis_coordination import SlidingWindowLimiter
from app.services.retrieval import VideoEvidenceRetrievalService
from app.services.task_events import TaskEventService
from app.services.telemetry import AgentTelemetry
from app.services.transcription import SegmentedTranscriptionService
from app.services.video_context import VideoContextService
from app.workers.executors import BoundedExecutor


class _UnavailableModel:
    def close(self) -> None:
        pass

    def __getattr__(self, _name: str):
        def unavailable(*_args, **_kwargs):
            raise ConfigError("SILICONFLOW_API_KEY is required for model requests")
        return unavailable


class _LazyProducer:
    """Start gRPC only on the first submitted task, then share it across requests."""

    def __init__(self, settings: Settings, producer: Any | None) -> None:
        self.settings = settings
        self._producer = producer
        self._started = False
        self._lock = Lock()

    def send_task(self, payload: dict[str, Any], *, topic: str) -> Any:
        if not self._started:
            with self._lock:
                if not self._started:
                    if self._producer is None:
                        self._producer = RocketMQProducer(
                            self.settings.rocketmq_endpoints,
                            topics=(self.settings.rocketmq_analysis_topic,
                                    self.settings.rocketmq_analysis_dead_topic),
                        )
                    self._producer.startup()
                    self._started = True
        return self._producer.send_task(payload, topic=topic)

    def close(self) -> None:
        with self._lock:
            if self._started:
                self._producer.shutdown()
                self._started = False


class AnalysisApiRuntime:
    def __init__(
        self, settings: Settings, *, redis_client: Any | None = None,
        producer: Any | None = None, model: Any | None = None,
        storage: Any | None = None,
    ) -> None:
        self.settings = settings
        self._owned_redis = redis_client is None
        self.redis = redis_client or redis.Redis(
            host=settings.redis_host, port=settings.redis_port, db=settings.redis_database,
            password=settings.redis_password or None, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        self.telemetry = AgentTelemetry(self.redis)
        self.limiter = SlidingWindowLimiter(self.redis)
        self.events = TaskEventService(self.redis, None)
        self.producer = _LazyProducer(settings, producer)
        self.mode_registry = ModeRegistry()
        self.executor = BoundedExecutor("AI-Thread-", max_workers=8, queue_capacity=100)
        self._storage = storage
        self._storage_lock = Lock()
        self._analysis_lock = Lock()
        self._http: httpx.Client | None = None
        self._model = model
        self._vector: QdrantStore | None = None
        self._asr_executor: ThreadPoolExecutor | None = None
        self._ocr_executor: ThreadPoolExecutor | None = None
        if self._model is None and not settings.siliconflow_api_key.strip():
            self._model = _UnavailableModel()
        self.mode_router = ModeRouter(self.model, self.limiter)

    @property
    def model(self) -> Any:
        if self._model is None:
            with self._analysis_lock:
                if self._model is None:
                    self._model = LLMClient(
                        self.http, api_key=self.settings.siliconflow_api_key,
                        base_url=self.settings.siliconflow_base_url,
                        model=self.settings.llm_model,
                        timeout_seconds=self.settings.llm_timeout_seconds,
                        input_price_per_million=self.settings.llm_input_price_per_million,
                        output_price_per_million=self.settings.llm_output_price_per_million,
                        max_estimated_cost=self.settings.agent_max_estimated_cost,
                        remaining_ms=AgentExecutionBudget.remaining_millis,
                        telemetry=self.telemetry,
                    )
        return self._model

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            with self._storage_lock:
                if self._http is None:
                    self._http = httpx.Client()
        return self._http

    @property
    def storage(self) -> Any:
        if self._storage is None:
            with self._storage_lock:
                if self._storage is None:
                    self._storage = ObjectStorage.from_settings(self.settings)
        return self._storage

    def _shared_analysis(self) -> tuple[Any, Any, Any, Any, Any]:
        with self._analysis_lock:
            if self._vector is None:
                self._vector = QdrantStore(
                    self.settings.qdrant_enabled, self.settings.qdrant_url,
                    self.settings.qdrant_api_key, self.settings.qdrant_collection,
                    client=self.http,
                )
            if self._asr_executor is None:
                self._asr_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dovideo-asr")
            if self._ocr_executor is None:
                self._ocr_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dovideo-ocr")
            model = self._model
            if model is None:
                model = LLMClient(
                    self.http, api_key=self.settings.siliconflow_api_key,
                    base_url=self.settings.siliconflow_base_url,
                    model=self.settings.llm_model,
                    timeout_seconds=self.settings.llm_timeout_seconds,
                    input_price_per_million=self.settings.llm_input_price_per_million,
                    output_price_per_million=self.settings.llm_output_price_per_million,
                    max_estimated_cost=self.settings.agent_max_estimated_cost,
                    remaining_ms=AgentExecutionBudget.remaining_millis,
                    telemetry=self.telemetry,
                )
                self._model = model
            return model, self._vector, self._asr_executor, self._ocr_executor, self.http

    @staticmethod
    def _checkpoints(session: Any, redis_client: Any) -> AgentCheckpointService:
        return AgentCheckpointService(AgentCheckpointRepository(session, redis_client), redis_client)

    def dispatch_service(self, session: Any, redis_client: Any) -> AnalysisDispatchService:
        media_repository = MediaRepository(session)
        # Dispatch only reads content_hash and invalidates no object-storage data.
        media = MediaService(media_repository, redis_client, None)
        return AnalysisDispatchService(
            redis_client, self.limiter, self.producer, media,
            self._checkpoints(session, redis_client), self.events,
            topic=self.settings.rocketmq_analysis_topic,
        )

    def ai_service(self, session: Any, redis_client: Any) -> AiService:
        model, vector, asr_executor, ocr_executor, http = self._shared_analysis()
        media_repository = MediaRepository(session)
        checkpoints = self._checkpoints(session, redis_client)
        storage = self.storage
        ffmpeg = FfmpegTools()
        asr = ASRClient(http, api_key=self.settings.siliconflow_api_key,
                        url=self.settings.asr_url, model=self.settings.asr_model,
                        telemetry=self.telemetry)
        transcription = SegmentedTranscriptionService(asr, self.telemetry, ffmpeg)
        video_context = VideoContextService(
            transcription, OcrTools(), storage, ffmpeg, self.telemetry,
            asr_executor, ocr_executor,
        )
        embeddings = EmbeddingClient(
            http, api_key=self.settings.siliconflow_api_key,
            base_url=self.settings.siliconflow_base_url,
            model=self.settings.embedding_model,
            telemetry=self.telemetry,
        )
        retrieval = VideoEvidenceRetrievalService(model, embeddings, vector, self.telemetry)
        chunking = VideoChunkingService(model, embeddings, self.telemetry)
        long_context = LongVideoContextService(self.telemetry, checkpoints, chunking, retrieval)
        loop = AgentLoopService(
            model, long_context, checkpoints, self.telemetry,
            EvidenceVerificationService(), self.events,
            max_rounds=self.settings.agent_max_rounds,
            max_duration_ms=self.settings.agent_max_duration_ms,
            max_estimated_tokens=self.settings.agent_max_estimated_tokens,
            max_estimated_cost=self.settings.agent_max_estimated_cost,
        )
        media = MediaService(
            media_repository, redis_client, storage,
            checkpoints=checkpoints, telemetry=self.telemetry,
            vector_store=vector, video_context=video_context,
        )
        return AiService(
            media_repository, video_context, long_context, loop, checkpoints,
            self.telemetry, media, self.events, redis_client, self.mode_registry,
        )

    def close(self) -> None:
        self.producer.close()
        self.executor.shutdown()
        if self._asr_executor is not None:
            self._asr_executor.shutdown(wait=False, cancel_futures=True)
        if self._ocr_executor is not None:
            self._ocr_executor.shutdown(wait=False, cancel_futures=True)
        if self._model is not None and hasattr(self._model, "close"):
            self._model.close()
        if self._vector is not None:
            self._vector.close()
        if self._http is not None:
            self._http.close()
        if self._owned_redis:
            self.redis.close()


def install_analysis_runtime(app: Any, settings: Settings | None = None, **kwargs) -> AnalysisApiRuntime:
    runtime = AnalysisApiRuntime(settings or app.state.settings, **kwargs)
    app.state.analysis_runtime = runtime
    app.state.analysis_ai_factory = runtime.ai_service
    app.state.analysis_dispatch_factory = runtime.dispatch_service
    app.state.analysis_telemetry = runtime.telemetry
    app.state.analysis_mode_router = runtime.mode_router
    app.state.analysis_executor = runtime.executor
    return runtime
