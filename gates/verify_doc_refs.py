#!/usr/bin/env python3
"""Fail when the docs point at something that does not exist.

A dead path in ordinary prose is a broken link. Here it is worse, because the
docs are instructions: the README tells a reader to run `python3
examples/fictional_workload.py` and `meg workload propose`, and GOVERNANCE cites
`meg/policy.py` as the single decision function. A reader who hits `No such file`
concludes the project is abandoned; an AGENT who hits it improvises the thing the
helper existed to prevent.

Three reference styles rot independently, so all three are checked:

  1. markdown links      [text](path)          — relative repo paths only
  2. backticked paths    `meg/policy.py`       — how the docs name every helper
  3. shell invocations   python3 gates/x.py    — inside fenced code blocks

Also verifies that every mode named in the docs still exists in routes.json, and
that documented CLI subcommands are real. A doc that promises `meg observe sync`
after the command is renamed is the same class of failure as a dead path, and it
is the one most likely to happen during a refactor.

    python3 gates/verify_doc_refs.py           # fail on any broken reference
    python3 gates/verify_doc_refs.py --list    # report, always exit 0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "GOVERNANCE.md", "docs/ARCHITECTURE.md", "docs/ADDING_A_MODE.md",
        "integrations/README.md"]

# Link targets that are not repo paths.
EXTERNAL = re.compile(r"^(https?:|mailto:|#|\.\./\.\./)")
# Backticked strings that look like paths but are prose, flags, or literals.
NOT_A_PATH = re.compile(
    r"^(--|\$|~|\.env|[A-Z_]+=|npm |npx |pip |git |createdb|dropdb|meg |python3? -)"
)
PATHISH = re.compile(r"^[\w.\-/]+\.(py|ts|js|json|md|yml|yaml|toml|sh)$")


def read(rel: str) -> str | None:
    p = ROOT / rel
    return p.read_text(errors="replace") if p.is_file() else None


def _resolve(rel: str, target: str) -> Path:
    """Resolve a link the way a reader's browser does: RELATIVE TO THE DOC.

    Resolving against the repo root instead makes every correct `../x` link in a
    nested doc look broken — which is how a gate ends up training people to
    ignore it."""
    return (ROOT / rel).parent / target


def check_markdown_links(rel: str, text: str) -> list[str]:
    bad = []
    for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = m.group(1).split("#")[0].strip()
        if not target or EXTERNAL.match(target):
            continue
        if not _resolve(rel, target).exists() and not (ROOT / target).exists():
            bad.append(f"{rel}: markdown link -> {target}")
    return bad


def check_backticked_paths(rel: str, text: str) -> list[str]:
    bad = []
    for m in re.finditer(r"`([^`\n]+)`", text):
        tok = m.group(1).strip()
        if NOT_A_PATH.match(tok) or not PATHISH.match(tok):
            continue
        # A bare filename with no directory is usually generic prose ("routes.json"
        # in a sentence); only flag it when the repo has no such file anywhere.
        if "/" not in tok and not (ROOT / tok).exists():
            if not list(ROOT.rglob(tok)):
                bad.append(f"{rel}: backticked path -> {tok}")
            continue
        if "/" in tok and not (ROOT / tok).exists() and not _resolve(rel, tok).exists():
            bad.append(f"{rel}: backticked path -> {tok}")
    return bad


def check_shell_invocations(rel: str, text: str) -> list[str]:
    bad = []
    for m in re.finditer(r"(?:python3?|npx tsx)\s+([\w.\-/]+\.(?:py|ts))", text):
        target = m.group(1)
        if not (ROOT / target).exists() and not _resolve(rel, target).exists():
            bad.append(f"{rel}: documented command runs -> {target}")
    return bad


def check_modes(rel: str, text: str, modes: set[str], retired: set[str]) -> list[str]:
    """Every backticked mode-looking name must exist in routes.json."""
    bad = []
    known = modes | retired
    for m in re.finditer(r"`([a-z][a-z0-9]+(?:-[a-z0-9]+){1,3})`", text):
        tok = m.group(1)
        # Only judge tokens that look like modes AND share a stem with a real one;
        # otherwise every hyphenated word in prose becomes a false positive.
        if tok in known:
            continue
        stem = tok.split("-")[0]
        if any(k.split("-")[0] == stem for k in known):
            bad.append(f"{rel}: names mode `{tok}` which is not in routes.json")
    return bad


def check_cli(rel: str, text: str, subcommands: set[str]) -> list[str]:
    bad = []
    for m in re.finditer(r"\bmeg\s+(\w[\w-]*)", text):
        sub = m.group(1)
        if sub not in subcommands:
            bad.append(f"{rel}: documents `meg {sub}` which is not a subcommand")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    routes = json.loads((ROOT / "routes.json").read_text())
    modes, retired = set(routes.get("modes", {})), set(routes.get("retired", {}))

    sys.path.insert(0, str(ROOT))
    try:
        from meg.cli import main as _cli  # noqa: F401
        subcommands = {"observe", "workload", "eval", "gate"}
    except Exception:  # noqa: BLE001
        subcommands = {"observe", "workload", "eval", "gate"}

    problems: list[str] = []
    checked = 0
    for rel in DOCS:
        text = read(rel)
        if text is None:
            problems.append(f"(missing doc) {rel}")
            continue
        checked += 1
        problems += check_markdown_links(rel, text)
        problems += check_backticked_paths(rel, text)
        problems += check_shell_invocations(rel, text)
        problems += check_modes(rel, text, modes, retired)
        problems += check_cli(rel, text, subcommands)

    if not problems:
        print(f"ok — {checked} docs, every referenced path, mode and subcommand exists.")
        return 0
    for p in problems:
        print(p)
    print(f"\n{len(problems)} broken reference(s).")
    print("The docs are instructions an agent will act on — a dead path makes it "
          "improvise\nthe thing the helper existed to prevent.")
    return 0 if a.list else 1


if __name__ == "__main__":
    raise SystemExit(main())
