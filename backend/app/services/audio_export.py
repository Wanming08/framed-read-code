"""MP3 export from a locally readable or signed video source."""

from __future__ import annotations

from pathlib import Path


class AudioExportService:
    def __init__(self, media_service, ffmpeg):
        self.media = media_service
        self.ffmpeg = ffmpeg

    def export_mp3(self, media_file) -> Path:
        storage = getattr(self.media, "storage", None)
        if storage is not None and hasattr(storage, "processing_source"):
            source = storage.processing_source(media_file.file_path)
        elif hasattr(self.media, "readable_source"):
            source = self.media.readable_source(media_file.file_path)
        else:
            source = storage.readable_source(media_file.file_path)
        return self.ffmpeg.export_mp3(source)
