"""Subprocess entry point:

    python -m app.processing.extraction.cli <input> <output.json> <mime_type> <limits.json>

Exit codes: 0 = result written; 3 = permanent, file-specific failure (error JSON written);
anything else (crash, OOM kill, signal) is treated by the worker as retryable.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.processing.extraction.extractors import extract
from app.processing.extraction.types import ExtractionFailed, ExtractionLimits

EXIT_PERMANENT = 3


def main(argv: list[str]) -> int:
    source, output, mime_type, limits_json = argv
    limits = ExtractionLimits(**json.loads(limits_json))
    out = Path(output)
    try:
        result = extract(Path(source), mime_type, limits)
    except ExtractionFailed as exc:
        out.write_text(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return EXIT_PERMANENT
    out.write_text(json.dumps(result.to_json()))
    return 0


def limits_to_json(limits: ExtractionLimits) -> str:
    return json.dumps(asdict(limits))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
