"""Independent transcription and MP3 endpoints under the original /analysis path."""

from __future__ import annotations

import json
import re
from threading import Lock
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.analysis_status import get_task_event_service
from app.api.dependencies import get_current_user, get_redis, get_session
from app.api.errors import BusinessError, ErrorCode
from app.api.media import get_media_service
from app.api.responses import ok
from app.db.repositories.media import MediaRepository
from app.integrations.asr import ASRClient
from app.integrations.ffmpeg import FfmpegTools
from app.schemas.mode import AnalysisMode
from app.services.analysis_status import TaskStage
from app.services.audio_export import AudioExportService
from app.services.media import MediaService
from app.services.task_events import TRANSCRIPTION, TaskEventService
from app.services.telemetry import AgentTelemetry
from app.services.transcription import SegmentedTranscriptionService
from app.services.transcription_task import TranscriptionTaskService
from app.workers.executors import BoundedExecutor


router = APIRouter(prefix="/analysis")
_RESOURCE_LOCK = Lock()


class _WorkerMediaRepository(MediaRepository):
    def close(self) -> None:
        self.session.close()


def get_processing_media(media: MediaService = Depends(get_media_service)) -> MediaService:
    return media


def get_processing_events(events: TaskEventService = Depends(get_task_event_service)) -> TaskEventService:
    return events


def get_transcription_task(
    request: Request,
    session: Session = Depends(get_session),
    redis_client=Depends(get_redis),
    media: MediaService = Depends(get_processing_media),
    events: TaskEventService = Depends(get_processing_events),
) -> TranscriptionTaskService:
    if not hasattr(request.app.state, "transcription_executor"):
        with _RESOURCE_LOCK:
            if not hasattr(request.app.state, "transcription_executor"):
                settings = request.app.state.settings
                client = httpx.Client()
                asr = ASRClient(
                    client, api_key=settings.siliconflow_api_key,
                    url=settings.asr_url, model=settings.asr_model,
                )
                request.app.state.transcription_http_client = client
                request.app.state.transcription_service = SegmentedTranscriptionService(
                    asr, AgentTelemetry(redis_client), FfmpegTools(),
                )
                request.app.state.transcription_executor = BoundedExecutor(
                    "AI-Thread-", max_workers=8, queue_capacity=100,
                )
    session_factory = request.app.state.session_factory
    return TranscriptionTaskService(
        lambda: _WorkerMediaRepository(session_factory()),
        redis_client, media, request.app.state.transcription_service,
        events, request.app.state.transcription_executor,
    )


def get_audio_export(media: MediaService = Depends(get_processing_media)) -> AudioExportService:
    return AudioExportService(media, FfmpegTools())


@router.post("/transcribe", status_code=202)
def transcribe(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_processing_media),
    task: TranscriptionTaskService = Depends(get_transcription_task),
):
    media.require_owned_media(id, user_id)
    if not task.queue(id):
        raise BusinessError(ErrorCode.CONFLICT, "文字提取任务正在处理中")
    try:
        task.dispatch(id)
    except Exception as exc:
        task.reject_queued(id)
        raise BusinessError(ErrorCode.SERVICE_UNAVAILABLE, "任务队列已满，请稍后重试") from exc
    return ok()


@router.get("/transcription-status")
def transcription_status(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_processing_media),
    task: TranscriptionTaskService = Depends(get_transcription_task),
):
    media_file = media.require_owned_media(id, user_id)
    return ok(task.status(media_file).to_dict())


@router.get("/transcription-events")
def transcription_events(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_processing_media),
    task: TranscriptionTaskService = Depends(get_transcription_task),
    events: TaskEventService = Depends(get_processing_events),
):
    media_file = media.require_owned_media(id, user_id)
    initial_status = task.status(media_file)

    async def stream():
        async for event in events.subscribe(
            id, TRANSCRIPTION, "", AnalysisMode.GENERAL, initial_status, TaskStage.TRANSCRIPTION,
        ):
            payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            yield "event: task-status\ndata: " + payload + "\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/download")
def download(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_processing_media),
    audio: AudioExportService = Depends(get_audio_export),
):
    media_file = media.require_owned_media(id, user_id)
    output_path = audio.export_mp3(media_file)
    stem = re.sub(r"\.[^.]+$", "", media_file.filename) if media_file.filename is not None else "audio"
    filename = stem + ".mp3"
    return FileResponse(
        output_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote_plus(filename)},
        background=BackgroundTask(output_path.unlink, missing_ok=True),
    )
