"""Concurrent ASR/OCR context building with Java-compatible degradation rules."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout, wait
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from PIL import Image

from app.schemas.video import VideoContext, VideoSegment


LOGGER = logging.getLogger(__name__)
SEGMENT_MS = 60_000
EVIDENCE_PREFIX = "evidence-frames"
BRANCH_BUDGET_SECONDS = 60 * 60
CANCEL_WAIT_SECONDS = 10


@dataclass(frozen=True)
class FramePart:
    timestamp_ms: int
    ocr_text: str
    frame_name: str


@dataclass(frozen=True)
class _BranchResult:
    items: list
    error: Exception | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


class VideoContextService:
    def __init__(
        self, transcription, ocr, object_storage, ffmpeg, telemetry,
        asr_executor, ocr_executor, *, work_root: str | Path | None = None,
    ):
        self.transcription = transcription
        self.ocr = ocr
        self.storage = object_storage
        self.ffmpeg = ffmpeg
        self.telemetry = telemetry
        self.asr_executor = asr_executor
        self.ocr_executor = ocr_executor
        self.work_root = Path(work_root) if work_root is not None else Path(tempfile.gettempdir())

    def build(self, video_path: str, user_goal: str, trace_id: str | None = None) -> VideoContext:
        processing_source = getattr(self.storage, "processing_source", self.storage.readable_source)
        readable_path = processing_source(video_path)
        work_dir = self.work_root / f"video-context-{uuid4()}"
        uploaded: list[str] = []
        futures: list[Future] = []
        cleanup_work_dir = True
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            futures.append(self._submit(self.asr_executor, lambda: self.transcription.transcribe(
                readable_path, work_dir / "audio", trace_id,
            )))
            futures.append(self._submit(self.ocr_executor, lambda: self._extract_frames(
                readable_path, work_dir / "frames", trace_id, uploaded,
            )))
            deadline = time.monotonic() + BRANCH_BUDGET_SECONDS
            transcript_result = futures[0].result(timeout=max(0, deadline - time.monotonic()))
            frame_result = futures[1].result(timeout=max(0, deadline - time.monotonic()))
            return self._finish(video_path, user_goal, trace_id, transcript_result, frame_result, uploaded)
        except FutureTimeout as exc:
            cleanup_work_dir = self._cancel_and_wait(futures)
            self._delete_evidence_frames(uploaded)
            raise RuntimeError("VideoContext 构建失败") from RuntimeError("VideoContext 分支处理超过总时间预算")
        except (KeyboardInterrupt, SystemExit):
            cleanup_work_dir = self._cancel_and_wait(futures)
            self._delete_evidence_frames(uploaded)
            raise
        except Exception as exc:
            self._delete_evidence_frames(uploaded)
            raise RuntimeError("VideoContext 构建失败") from exc
        finally:
            if cleanup_work_dir:
                try:
                    shutil.rmtree(work_dir)
                except FileNotFoundError:
                    pass
                except Exception:
                    LOGGER.warning("temporary_directory_cleanup_failed path=%s", work_dir, exc_info=True)
            else:
                LOGGER.warning("video_context_workdir_retained path=%s reason=branch_still_running", work_dir)

    def delete_evidence_frames(self, context: VideoContext | dict | None) -> None:
        if context is None:
            return
        segments = context.get("segments", []) if isinstance(context, dict) else context.segments
        frames = []
        for segment in segments or []:
            evidence = segment.get("evidenceFrames", []) if isinstance(segment, dict) else segment.evidenceFrames
            frames.extend(evidence or [])
        self._delete_evidence_frames(frames)

    def _submit(self, executor, function) -> Future:
        try:
            return executor.submit(self._branch, function)
        except Exception as exc:
            future = Future()
            future.set_result(_BranchResult([], exc))
            return future

    @staticmethod
    def _branch(function) -> _BranchResult:
        try:
            return _BranchResult(function())
        except Exception as exc:
            return _BranchResult([], exc)

    @staticmethod
    def _cancel_and_wait(futures: list[Future]) -> bool:
        for future in futures:
            if not future.done():
                future.cancel()
        _, pending = wait(futures, timeout=CANCEL_WAIT_SECONDS)
        return not pending

    def _finish(
        self, video_path: str, user_goal: str, trace_id: str | None,
        transcript_result: _BranchResult, frame_result: _BranchResult, uploaded: list[str],
    ) -> VideoContext:
        if transcript_result.failed and frame_result.failed:
            raise RuntimeError("ASR 和 OCR 分支均失败") from transcript_result.error
        if transcript_result.failed:
            self.telemetry.increment(trace_id, "asrBranchFailures", 1)
            LOGGER.warning("video_context_asr_branch_failed", exc_info=transcript_result.error)
        if frame_result.failed:
            self.telemetry.increment(trace_id, "ocrBranchFailures", 1)
            LOGGER.warning("video_context_ocr_branch_failed", exc_info=frame_result.error)
            self._delete_evidence_frames(uploaded)
            uploaded.clear()
        segments = self.merge(transcript_result.items, frame_result.items)
        if not segments:
            raise RuntimeError("视频未解析出有效语音或画面文字")
        return VideoContext(video_path, user_goal, segments)

    def _extract_frames(
        self, video_path: str, frame_dir: Path, trace_id: str | None, uploaded: list[str],
    ) -> list[FramePart]:
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_files = self.ffmpeg.extract_key_frames(video_path, frame_dir)
        result: list[FramePart] = []
        previous_hash: int | None = None
        failed_frames = 0
        for frame, timestamp_ms in frame_files:
            image_hash = self.difference_hash(frame)
            if previous_hash is not None and (previous_hash ^ image_hash).bit_count() <= 5:
                continue
            previous_hash = image_hash
            try:
                self.telemetry.increment(trace_id, "ocrCalls", 1)
                ocr_text = self.ocr.recognize(frame)
            except Exception:
                failed_frames += 1
                self.telemetry.increment(trace_id, "ocrFrameFailures", 1)
                LOGGER.warning("ocr_frame_failed frame=%s timestamp_ms=%s", frame.name, timestamp_ms, exc_info=True)
                continue
            try:
                frame_url = self.storage.upload_local_file(frame, frame.name, EVIDENCE_PREFIX)
                uploaded.append(frame_url)
            except Exception:
                self.telemetry.increment(trace_id, "frameUploadFailures", 1)
                LOGGER.warning("evidence_frame_upload_failed frame=%s timestamp_ms=%s", frame.name, timestamp_ms, exc_info=True)
                frame_url = f"{video_path}#timestampMs={timestamp_ms}"
            result.append(FramePart(timestamp_ms, ocr_text, frame_url))
        if not result and failed_frames:
            raise RuntimeError("所有 OCR 关键帧均处理失败")
        return result

    @staticmethod
    def difference_hash(path: str | Path) -> int:
        with Image.open(path) as image:
            pixels = list(image.convert("L").resize((9, 8), Image.Resampling.NEAREST).get_flattened_data())
        value = 0
        for row in range(8):
            for column in range(8):
                value <<= 1
                if pixels[row * 9 + column] > pixels[row * 9 + column + 1]:
                    value |= 1
        return value

    @staticmethod
    def merge(transcripts: list, frames: list[FramePart]) -> list[VideoSegment]:
        windows: dict[int, dict[str, list[str]]] = {}
        for transcript in transcripts:
            start_ms = transcript.startMs if hasattr(transcript, "startMs") else transcript[0]
            text = transcript.text if hasattr(transcript, "text") else transcript[2]
            window = start_ms // SEGMENT_MS * SEGMENT_MS
            windows.setdefault(window, {"transcripts": [], "ocr": [], "frames": []})["transcripts"].append(text)
        for frame in frames:
            window = frame.timestamp_ms // SEGMENT_MS * SEGMENT_MS
            segment = windows.setdefault(window, {"transcripts": [], "ocr": [], "frames": []})
            if frame.ocr_text and frame.ocr_text.strip():
                segment["ocr"].append(frame.ocr_text)
            segment["frames"].append(frame.frame_name)
        return [
            VideoSegment(start, start + SEGMENT_MS, "\n".join(parts["transcripts"]), parts["ocr"], parts["frames"])
            for start, parts in sorted(windows.items())
        ]

    def _delete_evidence_frames(self, frames: list[str]) -> None:
        for frame in dict.fromkeys(frames):
            if not self.storage.is_managed_file(frame):
                continue
            if f"/{EVIDENCE_PREFIX}/" not in urlparse(frame).path:
                continue
            try:
                self.storage.remove_file(frame)
            except Exception:
                LOGGER.warning("evidence_frame_cleanup_failed frame=%s", frame, exc_info=True)
