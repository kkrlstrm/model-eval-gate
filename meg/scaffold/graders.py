"""Graders, and the validation that has to happen before you trust one.

THE FAILURE THIS MODULE EXISTS FOR. In a real bake-off, a rubric grader was
written to check that generated labels "name the situation, not the role". It
scored every arm at 27-50% and the obvious reading was that all five models were
bad at following the instruction. They weren't. The grader flagged any word
sharing a token with a role name, so "technology refresh deadline" was condemned
for containing "technology". It was measuring itself.

That is not a rare bug. It is the DEFAULT outcome of writing a grader and
immediately running it, because a broken grader produces confident, plausible,
uniformly-bad numbers -- which read as a finding. So:

    A grader that cannot separate known-good from known-bad is not a
    strict grader. It is a broken one, and it must be rejected before
    a single candidate call is paid for.

`validate()` enforces that. It is a required stage, not a linting nicety.

THREE GRADER KINDS, in order of preference:
  code    deterministic, free, replayable. Available whenever the output has a
          checkable contract (schema, enum, numeric range, exact-match key).
  panel   cross-family model judges for generative outputs. Requires >= 2 judges
          from DIFFERENT families, because same-family judges inflate their own
          arm -- measured at +0.32 on a 1-5 scale in the reference bake-off.
  human   everything else. Not automatable; the honest answer is to say so.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class GraderResult:
    score: float                  # 0..1
    passed: bool
    detail: dict = field(default_factory=dict)


class Grader:
    kind = "abstract"
    name = "abstract"

    def grade(self, output: Any, sample: dict) -> GraderResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# code graders
# --------------------------------------------------------------------------- #
class SchemaGrader(Grader):
    """Structural conformance: parses, has required keys, values in the allowed set.

    Cheap and total. Where a task has an output contract this should always be the
    primary grader -- a model panel adds cost and variance for a question that a
    dict lookup answers exactly.
    """
    kind = "code"
    name = "schema"

    def __init__(self, required: list[str], enums: dict[str, list] | None = None,
                 key: str | None = None):
        self.required, self.enums, self.key = required, enums or {}, key

    def grade(self, output: Any, sample: dict) -> GraderResult:
        detail: dict = {}
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                return GraderResult(0.0, False, {"error": "unparseable"})
        if not isinstance(output, dict):
            return GraderResult(0.0, False, {"error": f"not an object: {type(output).__name__}"})
        missing = [k for k in self.required if k not in output]
        detail["missing"] = missing
        bad = {k: output.get(k) for k, allowed in self.enums.items()
               if k in output and output[k] not in allowed}
        detail["out_of_enum"] = bad
        ok = not missing and not bad
        if ok and self.key and "expected" in sample:
            ok = output.get(self.key) == sample["expected"]
            detail["expected"] = sample["expected"]
            detail["got"] = output.get(self.key)
        return GraderResult(1.0 if ok else 0.0, ok, detail)


class ExactMatchGrader(Grader):
    """Gold-label agreement. Requires a `expected` on every sample."""
    kind = "code"
    name = "exact-match"

    def __init__(self, key: str | None = None, normalize: Callable[[Any], Any] | None = None):
        self.key = key
        self.norm = normalize or (lambda v: v)

    def grade(self, output: Any, sample: dict) -> GraderResult:
        if isinstance(output, str) and self.key:
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                return GraderResult(0.0, False, {"error": "unparseable"})
        got = output.get(self.key) if (self.key and isinstance(output, dict)) else output
        ok = self.norm(got) == self.norm(sample.get("expected"))
        return GraderResult(1.0 if ok else 0.0, ok,
                            {"got": got, "expected": sample.get("expected")})


class RegexGrader(Grader):
    """Pattern conformance. Use sparingly and validate hard.

    This is the shape of grader that produced the false-positive disaster in the
    module docstring. If a regex grader survives validation it is usually because
    the pattern is anchored to a whole field, not probing for substrings.
    """
    kind = "code"
    name = "regex"

    def __init__(self, pattern: str, must_match: bool = True, field_key: str | None = None):
        self.re = re.compile(pattern, re.I)
        self.must_match, self.field_key = must_match, field_key

    def grade(self, output: Any, sample: dict) -> GraderResult:
        text = output
        if isinstance(output, dict) and self.field_key:
            text = output.get(self.field_key, "")
        hit = bool(self.re.search(str(text)))
        ok = hit if self.must_match else not hit
        return GraderResult(1.0 if ok else 0.0, ok, {"matched": hit})


# --------------------------------------------------------------------------- #
# panel grader
# --------------------------------------------------------------------------- #
@dataclass
class PanelVerdict:
    mean: float
    per_judge: dict[str, float]
    self_preference: dict[str, float]
    agreement_exact: float | None
    agreement_within_1: float | None
    usable: bool
    warnings: list[str]


def score_panel(votes: dict[str, dict[str, list[float]]]) -> PanelVerdict:
    """Aggregate a blind panel and report the things that invalidate one.

    `votes` is {judge: {arm: [scores]}}.

    Two checks are mandatory and reported by default rather than on request:

      SELF-PREFERENCE. A judge scoring an arm from its own model family higher
      than the others is the standard failure of LLM-as-judge. Measured, not
      assumed: in the reference bake-off the same-family judge scored its own arm
      +0.32 while a cross-family judge scored it -0.04. The verdict survived only
      *because* a second family was on the panel.

      AGREEMENT. Judges that do not agree have not produced a verdict, they have
      produced two opinions. Exact agreement is a weak bar on a 1-5 scale, so
      within-1 is reported alongside it.
    """
    judges = list(votes)
    arms = sorted({a for j in votes.values() for a in j})
    per_judge = {j: {a: (statistics.fmean(votes[j][a]) if votes[j].get(a) else 0.0)
                     for a in arms} for j in judges}
    mean = {a: statistics.fmean([per_judge[j][a] for j in judges]) for a in arms}

    self_pref: dict[str, float] = {}
    for j in judges:
        fam = j.split("-")[-1].split("/")[0].lower()
        own = [per_judge[j][a] for a in arms if fam and fam in a.lower()]
        other = [per_judge[j][a] for a in arms if not (fam and fam in a.lower())]
        if own and other:
            self_pref[j] = round(statistics.fmean(own) - statistics.fmean(other), 3)

    exact = within1 = None
    warnings: list[str] = []
    if len(judges) >= 2:
        a_, b_ = judges[0], judges[1]
        pairs = [(x, y) for arm in arms
                 for x, y in zip(votes[a_].get(arm, []), votes[b_].get(arm, []))]
        if pairs:
            exact = round(sum(1 for x, y in pairs if x == y) / len(pairs), 3)
            within1 = round(sum(1 for x, y in pairs if abs(x - y) <= 1) / len(pairs), 3)
    else:
        warnings.append("single judge — cross-family disagreement cannot be measured; "
                        "a same-family judge inflates its own arm by design")

    fams = {j.split("-")[-1].split("/")[0].lower() for j in judges}
    if len(judges) >= 2 and len(fams) < 2:
        warnings.append("all judges share a model family — panel is not cross-family")
    for j, d in self_pref.items():
        if d >= 0.25:
            warnings.append(f"{j} shows self-preference of +{d:.2f} toward its own family")
    if within1 is not None and within1 < 0.8:
        warnings.append(f"judges agree within-1 only {within1:.0%} of the time — "
                        "the panel has not produced a verdict")

    return PanelVerdict(
        mean=mean, per_judge=per_judge, self_preference=self_pref,
        agreement_exact=exact, agreement_within_1=within1,
        usable=not any(w.startswith(("all judges", "judges agree", "single judge"))
                       for w in warnings),
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# the mandatory gate
# --------------------------------------------------------------------------- #
def validate(grader: Grader, known_good: list[tuple[Any, dict]],
             known_bad: list[tuple[Any, dict]]) -> dict:
    """Prove a grader discriminates BEFORE spending money on candidates.

    Pass condition is deliberately absolute: every known-good must pass and every
    known-bad must fail. A grader with "just a couple" of false positives is a
    grader whose numbers you cannot attribute -- when an arm scores 60% you will
    not know whether that is the model or the grader, which is exactly the
    position that makes a bake-off worthless.
    """
    gp = [(o, s, grader.grade(o, s)) for o, s in known_good]
    bp = [(o, s, grader.grade(o, s)) for o, s in known_bad]
    fn = [(o, r.detail) for o, _, r in gp if not r.passed]   # good wrongly failed
    fp = [(o, r.detail) for o, _, r in bp if r.passed]       # bad wrongly passed
    ok = not fn and not fp
    return {
        "grader": grader.name,
        "kind": grader.kind,
        "known_good": len(gp), "known_bad": len(bp),
        "false_negatives": len(fn), "false_positives": len(fp),
        "valid": ok,
        "examples": {"wrongly_failed": fn[:5], "wrongly_passed": fp[:5]},
        "verdict": ("grader discriminates — safe to spend on candidates" if ok else
                    "REJECTED: this grader does not separate good from bad. Fix it "
                    "before running any model. Numbers from a grader in this state "
                    "describe the grader, not the models."),
    }
