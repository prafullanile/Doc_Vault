"""Runs extraction in a child process with a timeout (and, on POSIX, a memory cap).

A malicious or pathological file can then hang, exhaust memory or crash only the child.
The worker survives, records the failure and moves on.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

from app.core.config import Settings
from app.processing.errors import PermanentError, RetryableError
from app.processing.extraction.cli import EXIT_PERMANENT, limits_to_json
from app.processing.extraction.types import ExtractionLimits, ExtractionResult

BACKEND_DIR = Path(__file__).resolve().parents[3]


def limits_from_settings(settings: Settings) -> ExtractionLimits:
    return ExtractionLimits(
        max_pages=settings.max_pages,
        max_decompressed_bytes=settings.max_decompressed_bytes,
        ocr_language=settings.ocr_language,
        ocr_dpi=settings.ocr_dpi,
        ocr_min_chars=settings.ocr_min_chars,
    )


def _memory_limiter(megabytes: int) -> Callable[[], None] | None:
    if sys.platform == "win32":
        return None

    def _apply() -> None:  # runs in the child between fork and exec
        import resource

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


async def run_extraction(
    source: Path, mime_type: str, settings: Settings, workdir: Path
) -> ExtractionResult:
    output = workdir / "extraction.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.processing.extraction.cli",
        str(source),
        str(output),
        mime_type,
        limits_to_json(limits_from_settings(settings)),
        cwd=BACKEND_DIR,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_memory_limiter(settings.extraction_memory_mb),
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.extraction_timeout_seconds
        )
    except TimeoutError as exc:
        # The same file will almost certainly time out again, so don't retry it.
        raise PermanentError(
            "EXTRACTION_TIMEOUT",
            f"Extraction exceeded {settings.extraction_timeout_seconds}s",
        ) from exc
    finally:
        if process.returncode is None:  # timed out or the job was cancelled
            process.kill()
            await process.wait()

    if process.returncode == EXIT_PERMANENT:
        error = json.loads(output.read_text())["error"]
        raise PermanentError(error["code"], error["message"])
    if process.returncode != 0:
        tail = stderr.decode(errors="replace")[-500:]
        raise RetryableError("EXTRACTOR_CRASHED", f"exit code {process.returncode}: {tail}")
    return ExtractionResult.from_json(json.loads(output.read_text()))
