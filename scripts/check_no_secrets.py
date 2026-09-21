#!/usr/bin/env python
"""Pre-commit backstop (plan §7.1): scan staged files for credential-shaped
strings and block the commit if one is found. This is a safety net, not a
substitute for the "secrets only ever come from os.environ" policy — it
exists to catch an accidental paste, not to be the only thing enforcing it.

Wire it up as a git hook:

    # PowerShell, from the repo root:
    Copy-Item scripts\\check_no_secrets.py .git\\hooks\\pre-commit
    # then make .git/hooks/pre-commit executable if your git needs it to be

Or run it manually before committing: `python scripts/check_no_secrets.py`.
"""

from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = {
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Perplexity API key": re.compile(r"pplx-[A-Za-z0-9]{32,}"),
    "Alpaca key-shaped string": re.compile(r"\bPK[A-Z0-9]{18,}\b"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "generic long hex/base64 secret": re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
}


def staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def scan(path: str) -> list[str]:
    try:
        text = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    hits = []
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            hits.append(label)
    return hits


def main() -> int:
    problems = {}
    for path in staged_files():
        hits = scan(path)
        if hits:
            problems[path] = hits

    if not problems:
        return 0

    print("check_no_secrets: possible credentials in staged files — commit blocked.")
    for path, hits in problems.items():
        print(f"  {path}: {', '.join(hits)}")
    print("If this is a false positive, fix the pattern in scripts/check_no_secrets.py")
    print("rather than committing anyway.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
