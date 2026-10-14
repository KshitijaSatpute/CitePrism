"""Child process for `src.evidence.extraction.read_pdf_isolated`.

Reads one PDF and writes its pages as JSON to standard output. It runs as
`python -m src.evidence.pdf_worker <path> <method> <memory_mb>` so that it
imports only the reader — never the program that started it, which may be a
web server or a script without a main guard.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    path, method, memory_mb = argv[1], argv[2], int(argv[3])
    try:
        import resource
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass                                        # Windows has no RLIMIT_AS; the time limit still applies
    try:
        from src.evidence.extraction import pdf_source
        result = {"ok": True, "source": pdf_source(path, method)}
    except MemoryError:
        result = {"ok": False, "kind": "PdfLimitError", "message": "Reading the PDF needed more memory than allowed."}
    except RecursionError:
        result = {"ok": False, "kind": "ValueError", "message": "The PDF's structure is nested too deeply to read."}
    except Exception as error:  # noqa: BLE001 - reported to the parent, which raises
        result = {"ok": False, "kind": type(error).__name__, "message": str(error)[:500]}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
