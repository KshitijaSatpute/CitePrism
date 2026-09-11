#!/usr/bin/env python
"""Render the Phase 4 architecture diagram to SVG and PNG.

Requires mermaid-cli:  npm install -g @mermaid-js/mermaid-cli

    python scripts/render_architecture.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "architecture" / "phase4-architecture.mmd"


def main() -> int:
    binary = shutil.which("mmdc")
    if binary is None:
        print("mermaid-cli is not installed. Run: npm install -g @mermaid-js/mermaid-cli")
        return 1
    if not SOURCE.is_file():
        print(f"Diagram source is missing: {SOURCE}")
        return 1
    for extension, scale in (("svg", "1"), ("png", "2")):
        target = SOURCE.with_suffix(f".{extension}")
        result = subprocess.run(
            [binary, "-i", str(SOURCE), "-o", str(target),
             "-b", "white", "-s", scale, "-w", "1600"],
            check=False,
        )
        if result.returncode != 0:
            print(f"Rendering {extension} failed.")
            return result.returncode
        print(f"Wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
