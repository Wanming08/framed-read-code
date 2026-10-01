"""Sixty-second FFmpeg audio segments followed by independent ASR calls."""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


LOGGER = logging.getLogger(__name__)
SEGMENT_MS = 60_000


@dataclass(frozen=True)
class TranscriptSegment:
    startMs: int
    endMs: int
    text: str


class SegmentedTranscriptionService:
    def __init__(self, asr_client, telemetry, ffmpeg, *, work_root: str | Path | None = None):
        self.asr = asr_client
        self.telemetry = telemetry
        self.ffmpeg = ffmpeg
        self.work_root = Path(work_root) if work_root is not None else Path(tempfile.gettempdir())

    def transcribe(self, video_path: str, audio_dir: str | Path, trace_id: str | None = None) -> list[TranscriptSegment]:
        scope = (
            self.telemetry.trace_scope(trace_id)
            if trace_id is not None and hasattr(self.telemetry, "trace_scope")
            else nullcontext()
        )
        with scope:
            directory = Path(audio_dir)
            directory.mkdir(parents=True, exist_ok=True)
            audio_files = self.ffmpeg.extract_audio_segments(video_path, directory)
            result: list[TranscriptSegment] = []
            failed_segments = 0
            last_error: Exception | None = None
            for index, audio_file in enumerate(audio_files):
                try:
                    self.telemetry.increment(trace_id, "asrCalls", 1)
                    text = self.asr.audio_to_text(audio_file)
                    if text and text.strip():
                        result.append(TranscriptSegment(index * SEGMENT_MS, (index + 1) * SEGMENT_MS, text))
                except Exception as exc:
                    failed_segments += 1
                    last_error = exc
                    self.telemetry.increment(trace_id, "asrSegmentFailures", 1)
                    LOGGER.warning("asr_segment_failed segment=%s file=%s", index, Path(audio_file).name, exc_info=True)
            if not result and failed_segments:
                raise RuntimeError("所有 ASR 分片均处理失败") from last_error
            return result

    def transcribe_to_text(self, video_path: str) -> str:
        work_dir = self.work_root / f"transcription-{uuid4()}"
        try:
            return "\n".join(segment.text for segment in self.transcribe(video_path, work_dir) if segment.text.strip())
        except Exception as exc:
            raise RuntimeError("视频转写失败") from exc
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=False)
            except FileNotFoundError:
                pass
            except Exception:
                LOGGER.warning("transcription_temporary_directory_cleanup_failed path=%s", work_dir, exc_info=True)
