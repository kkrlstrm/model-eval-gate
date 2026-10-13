"""The routes.json schema, in Python. One validator for every Python reader.

src/schema.ts (zod) is the TypeScript half. The two must reach the same verdict on
every policy file; test/routes_cases.json is read by both test/routes_parity.ts and
test/test_pipeline.py, so a rule added to one side and not the other fails a test.
src/router.py and meg/policy.py both validate through this module, and RunTune uses it
when model-eval-gate is installed.

What a policy file must say, beyond its shape:

  * A CLEARED mode (the default `status`) names its evidence (`evidence_ref`) and when
    not to use it (`do_not_use_when`). The governance rule is that a mode exists because
    an eval cleared it; a cleared mode with no evidence entry is a claim nobody can check.
  * A TRIAL mode (`status: "trial"`) is authorised for a bounded trial before any eval
    has cleared it. It may omit both, and its `use_when` should say what bounds it.
  * Keys starting with `_` are annotations (`_readme`, `_caveat`) on the file, a mode or
    a retired entry. Any other unknown key is an error, so a misspelt field cannot be
    silently ignored.
  * No mode is in both `modes` and `retired`, and no mode is named for a model or a
    vibe (`cheap`, `fast`, …).
"""
from __future__ import annotations

import re

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESERVED_GENERIC = {"chat", "cheap", "cheapest", "free", "online", "general", "default",
                    "auto", "fast", "model", "llm", "any", "fallback"}
STATUSES = ("cleared", "trial")

_TOP = {"_schema", "_readme", "provider_pin_note", "staleness_warn_days", "modes", "retired"}
_MODE = {"model", "priceIn", "priceOut", "purpose", "use_when", "do_not_use_when", "evidence_ref",
         "verified_date", "provider", "constraints", "status"}
_RETIRED = {"retired_date", "reason"}
_PIN = {"order", "only", "allowFallbacks", "quantizations"}
_CONSTRAINTS = {"min_rows", "requires_human_review", "forbid_single_row_decision",
                "allowed_input_types", "max_stakes"}


def _unknown(obj: dict, allowed: set, where: str, errs: list, annotations: bool = True) -> None:
    for k in obj:
        if k not in allowed and not (annotations and k.startswith("_")):
            errs.append(f'{where}: Unrecognized key: "{k}"')


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and len(v) > 0


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str_list(v) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) for x in v)


def _pin(p, where: str, errs: list) -> None:
    if p is None:
        return
    if not isinstance(p, dict):
        errs.append(f"{where}: must be an object or null")
        return
    _unknown(p, _PIN, where, errs, annotations=False)
    for k in ("order", "only", "quantizations"):
        if k in p and not _str_list(p[k]):
            errs.append(f"{where}.{k}: must be a non-empty array of strings")
    if "allowFallbacks" in p and not isinstance(p["allowFallbacks"], bool):
        errs.append(f"{where}.allowFallbacks: must be a boolean")


def _constraints(c, where: str, errs: list) -> None:
    if not isinstance(c, dict):
        errs.append(f"{where}: must be an object")
        return
    _unknown(c, _CONSTRAINTS, where, errs, annotations=False)
    if "min_rows" in c and not (isinstance(c["min_rows"], int) and not isinstance(c["min_rows"], bool)
                                and c["min_rows"] > 0):
        errs.append(f"{where}.min_rows: must be a positive integer")
    for k in ("requires_human_review", "forbid_single_row_decision"):
        if k in c and not isinstance(c[k], bool):
            errs.append(f"{where}.{k}: must be a boolean")
    if "allowed_input_types" in c and not (
            _str_list(c["allowed_input_types"]) and set(c["allowed_input_types"]) <= {"text", "image"}):
        errs.append(f"{where}.allowed_input_types: must be a non-empty subset of text, image")
    if "max_stakes" in c and c["max_stakes"] not in ("low", "medium", "high"):
        errs.append(f"{where}.max_stakes: must be low, medium or high")


def _mode(name: str, m, errs: list) -> None:
    where = f"modes.{name}"
    if not isinstance(m, dict):
        errs.append(f"{where}: must be an object")
        return
    _unknown(m, _MODE, where, errs)
    status = m.get("status", "cleared")
    if status not in STATUSES:
        errs.append(f"{where}.status: must be one of {', '.join(STATUSES)}")
    required = ["model", "purpose", "use_when"]
    if status != "trial":
        required += ["do_not_use_when", "evidence_ref"]
    for f in required:
        if not _nonempty_str(m.get(f)):
            errs.append(f"{where}.{f}: required non-empty string"
                        + (" (a cleared mode names its evidence)" if f == "evidence_ref" else ""))
    for f in ("do_not_use_when", "evidence_ref"):
        if f not in required and f in m and not _nonempty_str(m[f]):
            errs.append(f"{where}.{f}: must be a non-empty string when present")
    for f in ("priceIn", "priceOut"):
        if not (_number(m.get(f)) and m[f] >= 0):
            errs.append(f"{where}.{f}: required non-negative number")
    vd = m.get("verified_date")
    if not (isinstance(vd, str) and ISO_DATE.match(vd)):
        errs.append(f"{where}.verified_date: must be an ISO date (YYYY-MM-DD)")
    if "provider" in m:
        _pin(m["provider"], f"{where}.provider", errs)
    if "constraints" in m:
        _constraints(m["constraints"], f"{where}.constraints", errs)


def validate_routes(raw) -> list[str]:
    """Every problem with a parsed routes.json, or [] when it is a valid policy."""
    if not isinstance(raw, dict):
        return ["(root): must be an object"]
    errs: list[str] = []
    _unknown(raw, _TOP, "(root)", errs)
    swd = raw.get("staleness_warn_days")
    if swd is not None and not (isinstance(swd, int) and not isinstance(swd, bool) and swd >= 0):
        errs.append("staleness_warn_days: must be a non-negative integer")
    for k in ("_schema", "_readme", "provider_pin_note"):
        if k in raw and not isinstance(raw[k], str):
            errs.append(f"{k}: must be a string")
    modes = raw.get("modes")
    if not isinstance(modes, dict):
        return errs + ["modes: required object"]
    if not modes:
        errs.append("modes: at least one mode is required")
    retired = raw.get("retired", {})
    if not isinstance(retired, dict):
        errs.append("retired: must be an object")
        retired = {}
    for name, m in modes.items():
        if name.lower() in RESERVED_GENERIC:
            errs.append(f"modes.{name}: reserved generic name — name a mode for its task shape (GOVERNANCE.md)")
        _mode(name, m, errs)
    for name, r in retired.items():
        where = f"retired.{name}"
        if not isinstance(r, dict):
            errs.append(f"{where}: must be an object")
            continue
        _unknown(r, _RETIRED, where, errs)
        if not _nonempty_str(r.get("reason")):
            errs.append(f"{where}.reason: required non-empty string")
        rd = r.get("retired_date")
        if rd is not None and not (isinstance(rd, str) and ISO_DATE.match(rd)):
            errs.append(f"{where}.retired_date: must be an ISO date (YYYY-MM-DD)")
        if name in modes:
            errs.append(f'"{name}" appears in BOTH modes and retired — a mode is one or the other')
    return errs
