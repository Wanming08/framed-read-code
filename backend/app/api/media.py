"""Original /media upload, URL import, list, playback and deletion routes."""

import os
from threading import Lock

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_redis, get_session
from app.api.responses import ok
from app.db.checkpoints import AgentCheckpointRepository
from app.db.repositories.media import MediaRepository
from app.integrations.object_storage import ObjectStorage
from app.integrations.qdrant_store import QdrantStore
from app.integrations.yt_dlp import YtDlpTools
from app.services.chunk_upload import ChunkUploadService
from app.services.checkpoints import AgentCheckpointService
from app.services.media import MediaService, summary
from app.services.telemetry import AgentTelemetry
from app.services.url_ingest import UrlIngestService
from app.services.video_context import VideoContextService


router = APIRouter(prefix="/media")
_STORAGE_LOCK = Lock()


def get_storage(request: Request) -> ObjectStorage:
    if not hasattr(request.app.state, "object_storage"):
        with _STORAGE_LOCK:
            if not hasattr(request.app.state, "object_storage"):
                request.app.state.object_storage = ObjectStorage.from_settings(request.app.state.settings)
    return request.app.state.object_storage


def get_media_service(
    session: Session = Depends(get_session),
    redis_client=Depends(get_redis),
    storage: ObjectStorage = Depends(get_storage),
) -> MediaService:
    return MediaService(MediaRepository(session), redis_client, storage)


def get_chunk_upload_service(
    redis_client=Depends(get_redis),
    storage: ObjectStorage = Depends(get_storage),
    media: MediaService = Depends(get_media_service),
) -> ChunkUploadService:
    return ChunkUploadService(redis_client, storage, media)


def get_url_ingest_service(media: MediaService = Depends(get_media_service)) -> UrlIngestService:
    downloader = YtDlpTools(
        command=os.getenv("YTDLP_PATH", "yt-dlp"),
        ffmpeg_dir=os.getenv("FFMPEG_DIR", ""),
    )
    return UrlIngestService(downloader, media)


def get_delete_media_service(
    request: Request,
    session: Session = Depends(get_session),
    redis_client=Depends(get_redis),
    storage: ObjectStorage = Depends(get_storage),
):
    settings = request.app.state.settings
    vector_store = QdrantStore(
        settings.qdrant_enabled, settings.qdrant_url,
        settings.qdrant_api_key, settings.qdrant_collection,
    )
    try:
        yield MediaService(
            MediaRepository(session), redis_client, storage,
            checkpoints=AgentCheckpointService(AgentCheckpointRepository(session, redis_client), redis_client),
            telemetry=AgentTelemetry(redis_client),
            vector_store=vector_store,
            video_context=VideoContextService(None, None, storage, None, None, None, None),
        )
    finally:
        vector_store.close()


@router.post("/init-upload")
def init_upload(
    filename: str = Query(...), totalChunks: int = Query(...),
    user_id: int = Depends(get_current_user), chunks: ChunkUploadService = Depends(get_chunk_upload_service),
):
    return ok(chunks.initialize(filename, totalChunks, user_id))


@router.get("/upload-status")
def upload_status(
    uploadId: str = Query(...), user_id: int = Depends(get_current_user),
    chunks: ChunkUploadService = Depends(get_chunk_upload_service),
):
    return ok(chunks.uploaded_chunks(uploadId, user_id))


@router.post("/upload-chunk")
def upload_chunk(
    uploadId: str = Form(...), chunkIndex: int = Form(...), totalChunks: int = Form(...),
    file: UploadFile = File(...), user_id: int = Depends(get_current_user),
    chunks: ChunkUploadService = Depends(get_chunk_upload_service),
):
    size = file.size
    if size is None:
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
    chunks.upload_chunk(uploadId, chunkIndex, totalChunks, file.file, size, user_id)
    return ok()


@router.post("/complete-upload")
def complete_upload(
    uploadId: str = Query(...), user_id: int = Depends(get_current_user),
    chunks: ChunkUploadService = Depends(get_chunk_upload_service),
):
    return ok(summary(chunks.complete(uploadId, user_id)))


@router.post("/upload")
def direct_upload(
    file: UploadFile = File(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_media_service),
):
    return ok(summary(media.ingest_file(file, user_id)))


@router.post("/upload-url")
def upload_url(
    url: str | None = Query(default=None), form_url: str | None = Form(default=None, alias="url"),
    user_id: int = Depends(get_current_user),
    ingest: UrlIngestService = Depends(get_url_ingest_service),
):
    selected_url = url if url is not None else form_url
    if selected_url is None:
        raise ValueError("url 不能为空")
    return ok(summary(ingest.ingest_url(selected_url, user_id)))


@router.get("/list")
def list_media(user_id: int = Depends(get_current_user), media: MediaService = Depends(get_media_service)):
    return ok(media.list_by_user(user_id))


@router.get("/playback")
def playback(id: int = Query(...), user_id: int = Depends(get_current_user), media: MediaService = Depends(get_media_service)):
    return ok(media.playback(id, user_id))


@router.delete("/delete")
def delete_media(
    id: int = Query(...), user_id: int = Depends(get_current_user),
    media: MediaService = Depends(get_delete_media_service),
):
    media.delete_owned_media(id, user_id)
    return ok()
