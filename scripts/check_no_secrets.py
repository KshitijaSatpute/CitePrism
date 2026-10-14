#!/usr/bin/env python
"""Fail if secrets are tracked by git, or appear in tracked file contents.

Runs in CI and in scripts/verify_phase4.ps1. Exit code 0 means clean.

    python scripts/check_no_secrets.py           # check what git tracks
    python scripts/check_no_secrets.py --staged  # check what is about to be committed

It looks for two things:

1. Paths that must never be tracked: `.env`, anything under `secrets/` except
   its README, private-key files.
2. Contents that look like credentials: PEM private keys, Google/OpenAI/Hugging
   Face/GitHub/AWS key formats, and non-placeholder assignments to the API-key
   variables CitePrism uses.

It is a guard rail, not a substitute for a secret scanner such as gitleaks in
the release pipeline.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env(\.[^/]*)?$"),
    re.compile(r"(^|/)secrets/(?!README\.md$)"),
    re.compile(r"\.(key|pem|p12|pfx)$"),
    re.compile(r"(^|/)id_(rsa|ed25519)(\.pub)?$"),
]
ALLOWED_PATHS = {"env.example"}

CONTENT_PATTERNS = [
    ("PEM private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]
# Assignments in env-style files whose value is not an obvious placeholder.
ASSIGNMENT = re.compile(
    r"^\s*(GOOGLE_API_KEY|OPENAI_API_KEY|HF_API_TOKEN|SEMANTIC_SCHOLAR_API_KEY|OPENALEX_API_KEY|"
    r"ELSEVIER_API_KEY|SPRINGER_API_KEY|CITEPRISM_PROXY_SECRET|CITEPRISM_EXPORT_HMAC_SECRET)"
    r"[ \t]*[=:][ \t]*[\"']?([^\"'\s#]*)",
    re.MULTILINE,
)
# Only configuration-style files carry KEY=value assignments worth checking.
ASSIGNMENT_SUFFIXES = {".env", ".example", ".yml", ".yaml", ".ini", ".cfg", ".toml", ".ps1", ".sh", ""}
PLACEHOLDER = re.compile(r"^$|^(none|null|false|true)$|your|example|replace|changeme|placeholder|xxx|<|\$|dev-only",
                         re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
                 ".md", ".txt", ".ps1", ".sh", ".env", ".example", ".sql", ".html", ".css", ""}
SKIP_CONTENT = {"tests/fixtures/", "scripts/check_no_secrets.py", "web/package-lock.json"}


def tracked_files(staged: bool) -> list[str]:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged \
        else ["git", "ls-files"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print("check_no_secrets: not a git checkout; nothing to check.")
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def scan(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized not in ALLOWED_PATHS and any(p.search(normalized) for p in FORBIDDEN_PATHS):
            problems.append(f"{normalized}: this file must never be committed")
            continue
        if any(normalized.startswith(prefix) for prefix in SKIP_CONTENT):
            continue
        file = ROOT / normalized
        if not file.is_file() or file.suffix.lower() not in TEXT_SUFFIXES or file.stat().st_size > 2_000_000:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in CONTENT_PATTERNS:
            if pattern.search(text):
                problems.append(f"{normalized}: contains what looks like a {label}")
        if file.suffix.lower() not in ASSIGNMENT_SUFFIXES and file.name != "env.example":
            continue
        for match in ASSIGNMENT.finditer(text):
            if not PLACEHOLDER.search(match.group(2)):
                problems.append(f"{normalized}: {match.group(1)} is assigned a real-looking value")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check staged changes only")
    arguments = parser.parse_args(argv)
    problems = scan(tracked_files(arguments.staged))
    if problems:
        print("Secret check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nRemove these from git (git rm --cached <path>), rotate anything that was "
              "exposed, and see docs/SECURITY_KEY_ROTATION.md.")
        return 1
    print("Secret check passed: no secrets are tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
