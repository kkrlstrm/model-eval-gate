# Adding a mode

A mode is a promise that a specific cheap model is safe for a specific task shape. You earn that promise with an eval. Here's the loop.

## 1. Write the eval as a regression spec

The fastest path is to write the spec you'll keep anyway. A spec is a JSON file in `eval/regression/`:

```jsonc
{
  "mode": "my-new-mode",
  "title": "What this mode does",
  "k": 3,                       // trials per task (non-determinism)
  "threshold": 0.66,            // per-trial pass line (fraction of graders passing)
  "drift_tolerance": 0.12,      // how far meanScore may fall before it's "drift"
  "consistency_floor": 0.5,     // minimum pass^k (all-k-pass rate) before it's "drift"
  "baseline": { "meanScore": null, "passHatK": null, "model": null, "verified_date": null },
  "input_template": "….{{var1}}….{{var2}}",   // {{...}} filled from each task's vars
  "graders": [
    { "kind": "fieldAgreement", "name": "agreement", "fields": ["field_a", "field_b"] }
  ],
  "tasks": [
    { "id": "t-01", "vars": { "var1": "…", "var2": "…" }, "gold": { "field_a": "…", "field_b": "…" } }
  ]
}
```

Use **real data** with **unambiguous gold** — a task where two people would independently agree on the label. Ambiguity in the task becomes noise in the metric. 20–50 tasks is plenty to start.

Grader kinds (`eval/graders.ts`) — **code** graders are deterministic, free, and reproducible (prefer them):

- `fieldAgreement` / `normalizedFieldAgreement` — compare fields to the frozen gold (exact, or whitespace/punctuation/case-normalized).
- `enumMatch` — a field must equal gold **and** be a member of an allowed enum.
- `numericWithinTolerance` — numeric fields within an absolute tolerance.
- `arraySetMatch` — compare an array field as a set (order/dupes ignored).
- `mustNotContain` — the output must not contain given substrings.
- `regexMatch` — a field must match a pattern.
- `jsonSubset` — every key/value in gold must be present in the output.
- `human` — a recorded verdict carried in each task's `gold.human_verdict` (`true`/`false`), to freeze a hand-made judgment call.
- `llmJudge` — an LLM scores the output against a rubric (`{ "kind": "llmJudge", "model": "…", "rubric": "…" }`). Opt-in; spends tokens per trial.

**Optional `constraints`** on a mode turn its advisory `use_when` / `do_not_use_when` into machine-checkable eligibility (`min_rows`, `forbid_single_row_decision`, `requires_human_review`, `allowed_input_types`, `max_stakes`). The CLI and `can_delegate()` refuse a task that breaks them — so a mode can't be misused on a task it wasn't proven for, not just misnamed.

Validate everything offline anytime (no API key): `npm run check` (or `python src/router.py check`).

## 2. Score the candidate against a frontier anchor

Add the candidate model to `routes.json` under a temporary mode name (or point the spec's model at it) and run:

```bash
npx tsx eval/regression.ts --mode my-new-mode --k 5
```

Also run the same tasks through your frontier/orchestrator model to get the anchor numbers. The candidate has to clear a **strict bar** — measurable parity for the use case. If it only wins with prompt scaffolding or "usually," it fails.

## 3. If it passes, promote it

- Add the mode to `routes.json` `modes` with `model`, `purpose`, and a `use_when` / `do_not_use_when` pair that each name something the eval actually showed.
- Set `verified_date`.
- Add an evidence entry to your observations log (see `docs/OBSERVATIONS.example.md`) — what you tested, on what data, the numbers, and the decision.
- Bootstrap the regression baseline: `npx tsx eval/regression.ts --mode my-new-mode --update-baseline`.

## 4. If it fails, retire it (don't delete it)

Move it to `routes.json` `retired` with a `retired_date` and a `reason`. The CLI will refuse it with that reason, so the next person who reaches for it learns why it's off the table.

## 5. Keep it honest

The regression spec you wrote in step 1 is now your guard. Run it on a schedule or in CI; it fails the moment the model drifts below its verified baseline, the allowlist swaps the model, or consistency collapses. Re-eval when the CLI's staleness warning fires.
