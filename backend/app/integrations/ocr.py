"""Tesseract wrapper with original language, timeout, and cleanup behavior."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable


class OcrTools:
    def __init__(
        self, command: str = "tesseract", *, runner: Callable = subprocess.run,
        temp_dir: str | Path | None = None,
    ):
        self.command = command
        self.runner = runner
        self.temp_dir = Path(temp_dir) if temp_dir is not None else None

    def recognize(self, image: str | Path) -> str:
        image = Path(image)
        if not image.is_file():
            raise ValueError("OCR image does not exist")
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="dovideo-ocr-", suffix=".txt",
            dir=self.temp_dir, delete=False,
        ) as output:
            output_path = Path(output.name)
        try:
            with output_path.open("w+b") as output:
                try:
                    result = self.runner(
                        [self.command, str(image.resolve()), "stdout", "-l", "chi_sim+eng"],
                        stdout=output, stderr=subprocess.STDOUT, timeout=120, check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("OCR execution timed out") from exc
                if result.returncode != 0:
                    raise RuntimeError(f"OCR process failed with exit code {result.returncode}")
                output.seek(0)
                return output.read().decode("utf-8").strip()
        except Exception as exc:
            raise RuntimeError(f"OCR failed for {image.name}") from exc
        finally:
            output_path.unlink(missing_ok=True)
