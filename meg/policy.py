"""The delegation decision, as one framework-agnostic function.

Agent runtimes decide WHAT work to do. This decides whether a given piece of that
work may be handed to a smaller model. Those are different questions, and the
second one is the only thing in this module.

Every integration -- an OpenClaw plugin, a Hermes adapter, a bare Python call --
funnels into `decide()` and gets the same verdict object back. Keeping one
decision function means a framework adapter is a thin translation layer with no
policy of its own, so a new runtime cannot accidentally ship a slightly different
interpretation of the rules.

    from meg.policy import decide
    d = decide("extract-bulk", {"rows": 5000, "single_row_decision": False})
    if d.allowed:
        call_with(d.model, provider=d.provider)   # earned permission
    else:
        call_with(frontier_model)                 # unearned downgrade refused
        log(d.reason)

DEFAULT IS REFUSE. An unknown mode, a retired mode, a malformed policy file, or
unmet constraints all return `allowed=False`. The caller then does the work on
whatever model it was already going to use, which is the safe direction: the
worst case of a wrong refusal is that you paid frontier prices for one task, and
the worst case of a wrong allow is a silent quality regression on real work.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ROUTES = ROOT / "routes.json"


# Graduated response, strongest last. A binary allow/refuse throws away the two
# most useful middle states:
#
#   monitor  record the decision, change nothing. How you roll a new mode out
#            without risking a workload: watch what it WOULD have done first.
#   nudge    let the call proceed, but hand the caller the reason it is
#            questionable. In an agent runtime this becomes context the model
#            reads and self-corrects on — a correction that costs nothing when
#            the model was right and saves a bad call when it was not.
#   refuse   the mode exists but this task does not qualify.
#   block    hard stop; nothing about this may be delegated.
ACTION_RANK = {"allow": 0, "monitor": 1, "nudge": 2, "refuse": 3, "block": 4}


@dataclass
class Decision:
    """Why a call may or may not be delegated. Always safe to log."""
    allowed: bool
    mode: str
    model: str | None = None
    provider: dict | None = None
    reason: str = ""
    violations: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    stale_days: int | None = None
    # "allow" | "monitor" | "nudge" | "refuse" | "block"
    action: str = "allow"
    # Text an agent runtime can surface to the model so it self-corrects.
    # Populated for `nudge`; empty otherwise.
    notes: list[str] = field(default_factory=list)

    @property
    def proceeds(self) -> bool:
        """Whether the DELEGATION happens. `monitor` and `nudge` still delegate;
        they differ in what they tell you about it."""
        return self.action in ("allow", "monitor", "nudge")

    def to_dict(self) -> dict:
        return asdict(self)


def load_routes(path: Path | str | None = None) -> dict:
    """Read the policy file. A malformed file yields an EMPTY policy, not a crash
    and not a permissive default: with no readable policy, nothing is earned."""
    p = Path(path) if path else ROUTES
    try:
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — unreadable policy == no permissions
        return {"modes": {}, "retired": {}, "_error": f"unreadable policy at {p}"}
    if not isinstance(data.get("modes"), dict):
        return {"modes": {}, "retired": {}, "_error": "policy has no modes object"}
    return data


def _days_since(iso: str | None) -> int | None:
    try:
        return (date.today() - date.fromisoformat(str(iso))).days
    except (TypeError, ValueError):
        return None


def check_constraints(spec: dict, meta: dict) -> tuple[list[str], list[str]]:
    """Machine-checkable task eligibility. Returns (violations, unchecked).

    `unchecked` matters as much as `violations`: a constraint the caller supplied
    no metadata for has NOT been satisfied, it has been skipped. Integrations
    should surface that rather than read silence as consent -- see
    `require_full_metadata` in `decide()`.
    """
    c = spec.get("constraints") or {}
    violations: list[str] = []
    unchecked: list[str] = []

    if (mr := c.get("min_rows")) is not None:
        rows = meta.get("rows")
        if rows is None:
            unchecked.append(f"min_rows={mr} (caller supplied no `rows`)")
        elif rows < mr:
            violations.append(f"min_rows={mr} but rows={rows}")

    if c.get("forbid_single_row_decision"):
        srd = meta.get("single_row_decision")
        if srd is None:
            unchecked.append("forbid_single_row_decision (caller supplied no "
                             "`single_row_decision`)")
        elif srd:
            violations.append("mode forbids single-row decisions")

    if c.get("requires_human_review"):
        hr = meta.get("human_reviewed")
        if hr is None:
            unchecked.append("requires_human_review (caller supplied no "
                             "`human_reviewed`)")
        elif not hr:
            violations.append("mode requires human review of the output")

    if allowed_in := c.get("allowed_input_types"):
        it = meta.get("input_type")
        if it is None:
            unchecked.append(f"allowed_input_types={allowed_in} (no `input_type`)")
        elif it not in allowed_in:
            violations.append(f"input_type={it!r} not in {allowed_in}")

    if (mx := c.get("max_stakes")) is not None:
        order = ["low", "medium", "high"]
        st = meta.get("stakes")
        if st is None:
            unchecked.append(f"max_stakes={mx} (caller supplied no `stakes`)")
        elif st not in order or order.index(st) > order.index(mx):
            violations.append(f"stakes={st!r} exceeds max_stakes={mx!r}")

    return violations, unchecked


def decide(mode: str, meta: dict | None = None, *, routes: dict | None = None,
           require_full_metadata: bool = False, posture: str = "attended",
           observe_only: bool = False, stale_after_days: int = 120,
           audit_path=None) -> Decision:
    """Should this work be delegated to the mode's smaller model?

    POSTURE decides what an *unproven* condition means, which is the only place
    reasonable people differ:

      "attended"    a human is reading the output. An unchecked constraint or a
                    stale verdict becomes a NUDGE: the call proceeds, and the
                    caller is handed the reason it is questionable.
      "unattended"  nobody is reading anything -- a cron job, a fleet worker, an
                    agent loop at 3am. There, "nobody objected" is not evidence,
                    so an unchecked constraint becomes a REFUSAL. This is what
                    `require_full_metadata` does, and setting posture handles it
                    without every caller having to remember the flag.

    `observe_only=True` downgrades every allow to MONITOR: the decision is
    recorded, nothing is enforced. That is how you roll a new mode out onto a
    live workload -- watch what it would have done for a week before letting it
    do it.
    """
    meta = meta or {}
    r = routes if routes is not None else load_routes()
    strict = require_full_metadata or posture == "unattended"

    def _finish(d: Decision) -> Decision:
        if observe_only and d.action == "allow":
            d.action = "monitor"
            d.notes.append("observe_only: recorded, not enforced — the call runs on "
                           "the model it would have used anyway.")
        if audit_path is not False:
            from . import audit as _audit
            _audit.append(d, path=audit_path,
                          posture=posture, observe_only=observe_only)
        return d

    if err := r.get("_error"):
        return _finish(Decision(False, mode, action="block", reason=(
            f"policy unavailable — refusing all delegation ({err})")))

    if (ret := (r.get("retired") or {}).get(mode)):
        return _finish(Decision(False, mode, action="block", reason=(
            f"mode {mode!r} is RETIRED ({ret.get('retired_date','?')}): "
            f"{ret.get('reason','no reason recorded')}")))

    spec = (r.get("modes") or {}).get(mode)
    if not spec:
        known = ", ".join(sorted(r.get("modes") or {})) or "(none)"
        return _finish(Decision(False, mode, action="block", reason=(
            f"mode {mode!r} is not on the allowlist. Earned modes: {known}. "
            "Unearned work stays on the frontier model.")))

    violations, unchecked = check_constraints(spec, meta)
    if strict:
        violations += [f"unverified constraint: {u}" for u in unchecked]
        unchecked = []

    stale = _days_since(spec.get("verified_date"))
    if violations:
        return _finish(Decision(False, mode, action="refuse", reason=(
            f"mode {mode!r} exists but this task does not qualify: "
            + "; ".join(violations)),
            violations=violations, unchecked=unchecked, stale_days=stale))

    d = Decision(
        True, mode, model=spec.get("model"), provider=spec.get("provider"),
        reason=f"earned permission: {spec.get('use_when', '')}".strip(),
        unchecked=unchecked, stale_days=stale, action="allow")

    # Proceed, but say what is unproven. In an attended session these are the two
    # things a human would want to know and would otherwise never be told.
    if unchecked:
        d.action = "nudge"
        d.notes.append(
            "delegated with UNVERIFIED constraints: " + "; ".join(unchecked)
            + ". Pass the metadata, or run with posture='unattended' to refuse instead.")
    if stale is not None and stale > stale_after_days:
        d.action = "nudge" if d.action == "allow" else d.action
        d.notes.append(
            f"mode {mode!r} was last verified {stale}d ago (> {stale_after_days}d). "
            "An old verdict is a hypothesis, not a fact — re-run its regression spec.")
    return _finish(d)


def explain(mode: str, routes: dict | None = None) -> dict:
    """Everything a human needs to judge a mode. Used by adapters for logging."""
    r = routes if routes is not None else load_routes()
    if ret := (r.get("retired") or {}).get(mode):
        return {"mode": mode, "status": "retired", **ret}
    spec = (r.get("modes") or {}).get(mode)
    if not spec:
        return {"mode": mode, "status": "unknown"}
    return {"mode": mode, "status": "allowed", "stale_days": _days_since(
        spec.get("verified_date")), **spec}
