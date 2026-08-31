#!/usr/bin/env python3
"""Fail when a git-TRACKED file contains a live credential or real telemetry.

WHY THIS EXISTS AT ALL. This repository's README promises it "ships no real
data — every fixture, sample workload, example observation and preset number is
invented." Until this gate existed that promise was enforced by nobody: a
human's memory at commit time, on a repo whose entire thesis is that unenforced
promises rot silently. A project that gates model delegation on evidence should
not gate its own central claim on good intentions.

Two failure shapes, both easy to commit by accident:

  1. A CREDENTIAL. Every file here is read by agents and copied into other
     people's projects. `.env` is gitignored and is the sanctioned home for real
     values; anything tracked is public.

  2. REAL TELEMETRY. The natural way to write a convincing example is to paste
     one from the system you actually run — a customer name, an internal
     hostname, a real workload tag, a database URL. It reads better and it is
     exactly the leak this repo tells other people to avoid.

    python3 gates/verify_no_real_data.py           # fail on any finding
    python3 gates/verify_no_real_data.py --list    # report, always exit 0
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".pyc", ".db")
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "vendor", ".git"}

# --------------------------------------------------------------------------- #
# Credentials. Patterns are deliberately provider-shaped rather than a generic
# "looks like entropy" heuristic, which fires on hashes and base64 fixtures.
# --------------------------------------------------------------------------- #
CREDENTIAL = [
    (r"sk-or-v1-[A-Za-z0-9]{16,}", "OpenRouter API key"),
    (r"sk-ant-[A-Za-z0-9\-_]{16,}", "Anthropic API key"),
    (r"\bsk-[A-Za-z0-9]{32,}", "OpenAI-style API key"),
    (r"glpat-[A-Za-z0-9\-_]{16,}", "GitLab personal access token"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"xox[baprs]-[A-Za-z0-9\-]{10,}", "Slack token"),
    (r"postgres(?:ql)?://[^\s:/]+:[^\s@]{6,}@", "Postgres DSN with a password"),
    (r"mysql://[^\s:/]+:[^\s@]{6,}@", "MySQL DSN with a password"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
]

# --------------------------------------------------------------------------- #
# Real-telemetry tells. Hostnames of managed services and absolute home paths
# are the two that actually slip through: they arrive attached to a copied
# example rather than being typed deliberately.
# --------------------------------------------------------------------------- #
REAL_DATA = [
    (r"\b[a-z0-9-]+\.neon\.tech\b", "a real Neon database host"),
    (r"\b[a-z0-9-]+\.supabase\.co\b", "a real Supabase host"),
    (r"\b[a-z0-9-]+\.rds\.amazonaws\.com\b", "a real RDS host"),
    (r"/Users/[a-z0-9_.-]+/", "an absolute macOS home path"),
    (r"/home/[a-z0-9_.-]+/", "an absolute Linux home path"),
]

# Documentation placeholders that must stay literal-matchable. Allowlisting by
# EXACT string, never by relaxing a pattern -- a loosened DSN regex would let a
# real credential through, which is the failure mode this trades against.
SAFE_LITERALS = {
    "postgresql://user:pass@host:5432/db",
    "postgresql://user:pass@localhost:5432/meg_pg_test",
    "postgres://u:secret@h/db",           # the redaction test's own input
    "sk-or-...",
    "sk-or-v1-...",
}

# Files whose job is to describe these patterns. Scanning them for the patterns
# they define is circular; they are checked for credentials only.
PATTERN_DEFINING = {"gates/verify_no_real_data.py"}

# A DSN regex in the list above is itself a valid match for the pattern it
# defines, so this file trips its own wire. Skipping the whole file would stop it
# catching a credential genuinely pasted here, so skip only the lines that ARE
# pattern definitions -- everything else in this file is still scanned. Note the
# comments here deliberately avoid spelling out a matchable example: loosening a
# pattern to accommodate prose is exactly the trade this gate's own failure
# message tells you not to make.
PATTERN_LITERAL = re.compile(r'^\s*\(\s*r"')

# Deliberate fixtures -- a test that proves this gate catches credentials has to
# CONTAIN credential-shaped strings. Marked per line, never per file: exempting a
# whole file would stop scanning real code that happens to live beside a fixture,
# and the marker has to be typed on purpose, so it cannot be reached by accident.
FIXTURE_MARKER = "meg-gate: fixture"


def _skippable(rel: str) -> bool:
    return (Path(rel).suffix.lower() in SKIP_SUFFIXES
            or bool(SKIP_PARTS & set(Path(rel).parts)))


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    files = []
    for rel in filter(None, out.split("\0")):
        p = ROOT / rel
        if _skippable(rel):
            continue
        if p.is_file():
            files.append(p)
    return files


def staged_sources() -> list[tuple[str, str]]:
    """[(path, content)] for what is ABOUT TO BE COMMITTED, read from the index.

    A pre-commit hook must not scan the working tree. The two differ in both
    directions and each is a real way to leak:

      * `git add` a file with a key, then fix the key on disk but not re-add it
        -> the working tree is clean and the COMMIT still carries the key.
      * `git add -p` a clean hunk from a file whose unstaged remainder has a key
        -> the working tree looks dirty and the commit is fine.

    Reading `git show :<path>` is the only thing that answers "what am I about to
    put in history", which is the question that matters because a credential in
    a commit stays there after the fix.
    """
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    sources: list[tuple[str, str]] = []
    for rel in filter(None, out.split("\0")):
        if _skippable(rel):
            continue
        blob = subprocess.run(["git", "show", f":{rel}"], cwd=ROOT,
                              capture_output=True, check=False)
        if blob.returncode != 0:
            continue
        try:
            sources.append((rel, blob.stdout.decode("utf-8", errors="replace")))
        except Exception:  # noqa: BLE001 — binary or undecodable: nothing to scan
            continue
    return sources


def scan_text(rel: str, text: str) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    checks = list(CREDENTIAL)
    if rel not in PATTERN_DEFINING:
        checks += REAL_DATA
    for i, line in enumerate(text.splitlines(), 1):
        if any(lit in line for lit in SAFE_LITERALS):
            continue
        if rel in PATTERN_DEFINING and PATTERN_LITERAL.match(line):
            continue
        if FIXTURE_MARKER in line:
            continue
        for pat, label in checks:
            m = re.search(pat, line)
            if m:
                snippet = m.group(0)
                if len(snippet) > 24:
                    snippet = snippet[:12] + "…" + snippet[-6:]
                findings.append((i, label, snippet))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="report findings but always exit 0")
    ap.add_argument("--staged", action="store_true",
                    help="scan the staged content about to be committed, not the "
                         "working tree (used by the pre-commit hook)")
    a = ap.parse_args()

    if a.staged:
        sources = staged_sources()
        what = f"{len(sources)} staged file(s)"
    else:
        sources = []
        for path in tracked_files():
            try:
                sources.append((str(path.relative_to(ROOT)),
                                path.read_text(errors="replace")))
            except OSError:
                continue
        what = f"{len(sources)} tracked files"

    total = 0
    for rel, text in sources:
        for line_no, label, snippet in scan_text(rel, text):
            total += 1
            print(f"{rel}:{line_no}  {label}: {snippet}")

    if total == 0:
        print(f"ok — {what}, no credentials or real telemetry found.")
        return 0
    print(f"\n{total} finding(s) across {what}.")
    print("This repository promises it ships no real data. Replace the value with a "
          "fictional one,\nor add an EXACT documentation literal to SAFE_LITERALS — "
          "never by loosening a pattern.")
    if a.staged:
        print("\nA credential in a commit stays in history after you fix it. If this is "
              "a\ndeliberate test fixture, mark that line `# meg-gate: fixture`.")
    return 0 if a.list else 1


if __name__ == "__main__":
    raise SystemExit(main())
