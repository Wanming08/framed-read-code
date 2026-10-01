"""yt-dlp subprocess and public URL checks from YtDlpUtils.java."""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4


LOG = logging.getLogger(__name__)
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
VIDEO_FORMAT = "bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a][ext=m4a]/b[vcodec^=avc1][ext=mp4]/bv*[vcodec^=avc1]+ba[acodec^=mp4a]"


def _resolve_addresses(host: str) -> list[str]:
    return list({entry[4][0] for entry in socket.getaddrinfo(host, None)})


class YtDlpTools:
    def __init__(
        self, command: str = "yt-dlp", ffmpeg_dir: str = "", *,
        runner: Callable = subprocess.run,
        resolver: Callable[[str], list[str]] = _resolve_addresses,
        temp_dir: str | Path | None = None,
    ) -> None:
        self.command = command
        self.ffmpeg_dir = ffmpeg_dir
        self.runner = runner
        self.resolver = resolver
        self.temp_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())

    def validate_public_http_url(self, value: str) -> None:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            _ = parsed.port
        except (ValueError, AttributeError) as exc:
            raise ValueError("仅支持合法的公网 HTTP/HTTPS 视频链接") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise ValueError("仅支持合法的公网 HTTP/HTTPS 视频链接")
        try:
            addresses = self.resolver(host)
        except OSError as exc:
            raise ValueError("无法解析视频链接的主机地址") from exc
        if not addresses:
            raise ValueError("无法解析视频链接的主机地址")
        try:
            if any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ValueError("不允许访问本机、内网或保留网段地址")
        except ValueError as exc:
            if str(exc) == "不允许访问本机、内网或保留网段地址":
                raise
            raise ValueError("无法解析视频链接的主机地址") from exc

    def download_video(self, url: str) -> Path:
        self.validate_public_http_url(url)
        output_path = self.temp_dir / f"{uuid4()}.mp4"
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="yt-dlp-", suffix=".log", dir=self.temp_dir, delete=False,
        ) as output:
            log_path = Path(output.name)
        command = [
            self.command, "--no-playlist", "--socket-timeout", "30", "--retries", "3",
            "--max-filesize", "2048M", "-f", VIDEO_FORMAT,
            "--merge-output-format", "mp4", "--recode-video", "mp4",
        ]
        if self.ffmpeg_dir.strip():
            command += ["--ffmpeg-location", self.ffmpeg_dir]
        command += ["-o", str(output_path), url]
        try:
            with log_path.open("w+b") as output:
                try:
                    result = self.runner(
                        command, stdout=output, stderr=subprocess.STDOUT,
                        timeout=DOWNLOAD_TIMEOUT_SECONDS, check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("视频链接下载超时") from exc
            if result.returncode != 0 or not output_path.is_file():
                logs = log_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError("yt-dlp 下载失败: " + logs[-2000:])
            LOG.info("url_video_downloaded host=%s bytes=%s", urlsplit(url).hostname, output_path.stat().st_size)
            return output_path
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            log_path.unlink(missing_ok=True)
