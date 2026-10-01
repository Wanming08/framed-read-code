"""Standalone one-slot analysis worker; run with ``python -m app.worker``."""

from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass

import httpx
import redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.loop import AgentLoopService
from app.agent.budget import AgentExecutionBudget
from app.agent.modes import ModeRegistry
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
from app.integrations.rocketmq import RocketMQConsumer, RocketMQProducer
from app.services.analysis import AiService
from app.services.checkpoints import AgentCheckpointService
from app.services.chunking import VideoChunkingService
from app.services.evidence import EvidenceVerificationService
from app.services.failed_tasks import FailedAnalysisTaskService, FailedTaskRepository
from app.services.long_video_context import LongVideoContextService
from app.services.media import MediaService
from app.services.retrieval import VideoEvidenceRetrievalService
from app.services.task_events import TaskEventService
from app.services.telemetry import AgentTelemetry
from app.services.transcription import SegmentedTranscriptionService
from app.services.video_context import VideoContextService
from app.workers.analysis_consumer import AnalysisConsumerWorker, LeasePolicy
from app.workers.business_handler import AnalysisBusinessHandler


LOG = logging.getLogger(__name__)


def validate_worker_settings(settings: Settings) -> None:
    if not settings.siliconflow_api_key.strip():
        raise ConfigError("SILICONFLOW_API_KEY is required for the analysis worker")
    if not settings.minio_access_key.strip():
        raise ConfigError("MINIO_ACCESS_KEY is required for the analysis worker")
    if not settings.minio_secret_key.strip():
        raise ConfigError("MINIO_SECRET_KEY is required for the analysis worker")
    if not settings.rocketmq_endpoints.strip():
        raise ConfigError("ROCKETMQ_ENDPOINTS is required for the analysis worker")
    if not settings.rocketmq_analysis_topic.strip() or not settings.rocketmq_analysis_dead_topic.strip():
        raise ConfigError("RocketMQ analysis and dead-letter topics are required")
    if not settings.rocketmq_analysis_group.strip():
        raise ConfigError("ROCKETMQ_ANALYSIS_GROUP is required for the analysis worker")
    if settings.agent_max_estimated_cost > 0 and (
        settings.llm_input_price_per_million <= 0 or settings.llm_output_price_per_million <= 0
    ):
        raise ConfigError(
            "LLM_INPUT_PRICE_PER_MILLION and LLM_OUTPUT_PRICE_PER_MILLION are required "
            "when AGENT_MAX_ESTIMATED_COST is enabled"
        )
    settings.mysql_url  # validates DB_PASSWORD or DATABASE_URL


def message_transaction_boundary(handler, session):
    """Give every message a fresh MySQL read snapshot, including after failures.

    The worker intentionally reuses a session for its one-slot consumer. MySQL's
    REPEATABLE READ otherwise keeps a read-only transaction open after a lookup,
    so a later upload can appear missing to the next message.
    """

    def handle(message):
        session.rollback()
        try:
            return handler(message)
        finally:
            session.rollback()

    return handle


@dataclass
class WorkerRuntime:
    worker: AnalysisConsumerWorker
    resources: ExitStack

    def close(self) -> None:
        self.resources.close()


def build_worker(settings: Settings) -> WorkerRuntime:
    validate_worker_settings(settings)
    resources = ExitStack()
    try:
        engine = create_engine(settings.mysql_url, pool_pre_ping=True,
                               pool_size=settings.db_pool_max_size, pool_timeout=3)
        resources.callback(engine.dispose)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        resources.callback(session.close)
        redis_client = redis.Redis(
            host=settings.redis_host, port=settings.redis_port,
            db=settings.redis_database, password=settings.redis_password or None,
            decode_responses=True, socket_connect_timeout=3, socket_timeout=3,
        )
        resources.callback(redis_client.close)
        http = resources.enter_context(httpx.Client())
        storage = ObjectStorage.from_settings(settings)
        vector = QdrantStore(settings.qdrant_enabled, settings.qdrant_url,
                             settings.qdrant_api_key, settings.qdrant_collection)
        resources.callback(vector.close)

        media_repository = MediaRepository(session)
        checkpoints = AgentCheckpointService(AgentCheckpointRepository(session, redis_client), redis_client)
        telemetry = AgentTelemetry(redis_client)
        events = TaskEventService(redis_client, None)
        ffmpeg = FfmpegTools()
        asr = ASRClient(http, api_key=settings.siliconflow_api_key,
                        url=settings.asr_url, model=settings.asr_model,
                        telemetry=telemetry)
        transcription = SegmentedTranscriptionService(asr, telemetry, ffmpeg)
        asr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dovideo-asr")
        ocr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dovideo-ocr")
        resources.callback(asr_executor.shutdown, wait=False, cancel_futures=True)
        resources.callback(ocr_executor.shutdown, wait=False, cancel_futures=True)
        video_context = VideoContextService(transcription, OcrTools(), storage, ffmpeg,
                                            telemetry, asr_executor, ocr_executor)
        model = LLMClient(http, api_key=settings.siliconflow_api_key,
                          base_url=settings.siliconflow_base_url, model=settings.llm_model,
                          timeout_seconds=settings.llm_timeout_seconds,
                          input_price_per_million=settings.llm_input_price_per_million,
                          output_price_per_million=settings.llm_output_price_per_million,
                          max_estimated_cost=settings.agent_max_estimated_cost,
                          remaining_ms=AgentExecutionBudget.remaining_millis,
                          telemetry=telemetry)
        resources.callback(model.close)
        embeddings = EmbeddingClient(http, api_key=settings.siliconflow_api_key,
                                     base_url=settings.siliconflow_base_url,
                                     model=settings.embedding_model,
                                     telemetry=telemetry)
        retrieval = VideoEvidenceRetrievalService(model, embeddings, vector, telemetry)
        chunking = VideoChunkingService(model, embeddings, telemetry)
        long_context = LongVideoContextService(telemetry, checkpoints, chunking, retrieval)
        agent_loop = AgentLoopService(
            model, long_context, checkpoints, telemetry, EvidenceVerificationService(), events,
            max_rounds=settings.agent_max_rounds,
            max_duration_ms=settings.agent_max_duration_ms,
            max_estimated_tokens=settings.agent_max_estimated_tokens,
            max_estimated_cost=settings.agent_max_estimated_cost,
        )
        media_service = MediaService(media_repository, redis_client, storage,
                                     checkpoints=checkpoints, telemetry=telemetry,
                                     vector_store=vector, video_context=video_context)
        ai_service = AiService(media_repository, video_context, long_context, agent_loop,
                               checkpoints, telemetry, media_service, events, redis_client,
                               ModeRegistry())
        producer = RocketMQProducer(settings.rocketmq_endpoints,
                                    topics=(settings.rocketmq_analysis_topic,
                                            settings.rocketmq_analysis_dead_topic))
        producer.startup()
        resources.callback(producer.shutdown)
        consumer = RocketMQConsumer(
            settings.rocketmq_endpoints, topic=settings.rocketmq_analysis_topic,
            group=settings.rocketmq_analysis_group,
            invisible_duration=LeasePolicy().visibility_seconds,
        )
        failed_tasks = FailedAnalysisTaskService(FailedTaskRepository(session), producer,
                                                 redis_client, events,
                                                 topic=settings.rocketmq_analysis_topic)

        def analyze(media_id, goal, mode):
            try:
                return ai_service.async_analyze(media_id, goal, mode)
            except BaseException:
                session.rollback()
                raise

        handler = AnalysisBusinessHandler(
            redis_client, producer, checkpoints, failed_tasks, events,
            media_exists=media_service.exists,
            purge_media=media_service.purge_runtime_artifacts,
            analyze=analyze,
            reuse_result=ai_service.reuse_result,
            dead_topic=settings.rocketmq_analysis_dead_topic,
            lock_ttl_seconds=LeasePolicy().lock_ttl_seconds,
        )
        worker = AnalysisConsumerWorker(consumer, message_transaction_boundary(handler, session),
                                        policy=LeasePolicy(),
                                        renew_lock=handler.renew_lock,
                                        finalize=handler.finalize)
        return WorkerRuntime(worker, resources)
    except BaseException:
        resources.close()
        raise


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    runtime = build_worker(Settings())
    stop = threading.Event()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_name, lambda _signal, _frame: stop.set())
    try:
        runtime.worker.run_forever(stop)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
