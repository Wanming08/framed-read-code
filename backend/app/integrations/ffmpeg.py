"""FFmpeg command adapter with the exact upstream extraction parameters."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable


PTS_TIME = re.compile(r"pts_time:([0-9.]+)")
FFMPEG_TIMEOUT_SECONDS = 15 * 60
FALLBACK_FRAME_INTERVAL_MS = 30_000
FRAME_FILTER = r"select=eq(n\,0)+gt(scene\,0.35)+gte(t-prev_selected_t\,30),showinfo"


class FfmpegTools:
    def __init__(
        self, command: str = "ffmpeg", *, runner: Callable = subprocess.run,
        temp_dir: str | Path | None = None,
    ):
        self.command = command
        self.runner = runner
        self.temp_dir = Path(temp_dir) if temp_dir is not None else None

    def run_command(self, command: list[str], *, collect_timestamps: bool = False) -> list[int]:
        """Capture process output in a temp file, parse showinfo, then always remove it."""
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="dovideo-ffmpeg-", suffix=".log",
            dir=self.temp_dir, delete=False,
        ) as output:
            log_path = Path(output.name)
        try:
            with log_path.open("w+b") as output:
                try:
                    result = self.runner(
                        command, stdout=output, stderr=subprocess.STDOUT,
                        timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("FFmpeg 执行超时") from exc
                if result.returncode != 0:
                    raise RuntimeError("FFmpeg 执行失败")
                if not collect_timestamps:
                    return []
                output.seek(0)
                timestamps = []
                for line in output:
                    decoded = line.decode("utf-8", errors="replace")
                    if "showinfo" not in decoded:
                        continue
                    match = PTS_TIME.search(decoded)
                    if match:
                        timestamps.append(int(float(match.group(1)) * 1000))
                return timestamps
        finally:
            log_path.unlink(missing_ok=True)

    def extract_audio_segments(self, video_path: str, audio_dir: str | Path) -> list[Path]:
        directory = Path(audio_dir)
        directory.mkdir(parents=True, exist_ok=True)
        output_pattern = directory / "audio_%03d.mp3"
        self.run_command([
            self.command, "-y", "-i", video_path,
            "-vn", "-acodec", "libmp3lame", "-f", "segment", "-segment_time", "60",
            "-reset_timestamps", "1", str(output_pattern),
        ])
        return sorted(path for path in directory.glob("audio_*.mp3") if path.is_file())

    def extract_key_frames(self, video_path: str, frame_dir: str | Path) -> list[tuple[Path, int]]:
        directory = Path(frame_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamps = self.run_command([
            self.command, "-y", "-i", video_path,
            "-vf", FRAME_FILTER, "-vsync", "vfr", str(directory / "frame_%06d.jpg"),
        ], collect_timestamps=True)
        frames = sorted(path for path in directory.glob("frame_*.jpg") if path.is_file())
        return [
            (path, timestamps[index] if index < len(timestamps) else index * FALLBACK_FRAME_INTERVAL_MS)
            for index, path in enumerate(frames)
        ]

    def export_mp3(self, input_path: str) -> Path:
        if not input_path or (not input_path.startswith("http") and not Path(input_path).is_file()):
            raise FileNotFoundError("视频源文件不存在")
        with tempfile.NamedTemporaryFile(
            prefix="dovideo-audio-", suffix=".mp3", dir=self.temp_dir, delete=False,
        ) as output:
            output_path = Path(output.name)
        try:
            self.run_command([
                self.command, "-y", "-i", input_path,
                "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(output_path),
            ])
            return output_path
        except RuntimeError as exc:
            output_path.unlink(missing_ok=True)
            if str(exc) == "FFmpeg 执行超时":
                raise RuntimeError("音频转换超时") from exc
            raise RuntimeError("音频转换失败") from exc
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
