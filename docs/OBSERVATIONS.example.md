# Observations (example)

This is the **evidence trail** that sits behind `routes.json`. Every mode and every retirement points here. It's a chronological, append-only log: what you tested, on what data, the numbers, and the decision. `routes.json` is the binding policy; this file is the *why*.

The entries below are illustrative — they show the shape and the kind of reasoning that clears (or fails) the bar. Replace them with your own runs.

---

## Eval 1 — bulk field extraction → enabled `extract-bulk`

**Setup.** 50 real leads, 3 fields each (`title_seniority`, `function`, `org_type`) against a fixed enum. Three models: a cheap high-throughput model, a cheap flash model, and the frontier anchor. The anchor is the agreement baseline.

**Result.** The throughput model: 49/50 parsed, agreement vs anchor 98% (`function`) / 96% (`org_type`) / 80% (`title_seniority`). Cost ~1.6% of the anchor. The flash model was faster but **systematically wrong on ambiguous titles** in a way the throughput model wasn't (it literal-pattern-matched job titles the anchor read correctly).

**Decision.** Enabled the throughput model as **`extract-bulk`, aggregate analytics only.** The 80% on `title_seniority` is fine when the output is *counted* across thousands of rows (errors average out) and disqualifying when an operator acts on a single row — hence the `do_not_use_when`. The flash model was **not** enabled for judgment-laden extraction.

## Eval 1b — precision sibling → enabled `extract-accurate`

**Setup.** Same 50 leads, same schema. Challenger: a newer cheap model.

**Result.** The challenger beat the throughput incumbent on anchor-agreement across all three fields and parsed 50/50, at ~3× the cost and ~3× the latency (both still trivial).

**Decision.** Kept both. `extract-bulk` = cheapest/fastest for pure aggregate; `extract-accurate` = higher agreement when per-field accuracy matters and latency isn't the constraint. A refinement, not a replacement — the `do_not_use_when` (no single-row decisions) still holds for both.

## Eval 4 — code critique → `lint-code` (supplement only), primary review stays with the anchor

**Setup.** Two real source files (~40K chars), prompt: "top risks at 10× scale." Cheap linter vs a reasoning model vs the frontier anchor. A human then read every output to confirm which findings were real.

**Result.** The cheap linter found correctness/edge-case bugs fast and cheap. But **only the frontier anchor surfaced a real SQL-injection vector** (f-string interpolation into a subprocess) as a standalone finding; the others missed it or mentioned it in passing.

**Decision.** Enabled the linter as **`lint-code`, a supplemental pre-pass whose findings must be re-evaluated before action** — never the primary review. Primary architecture/security review stays with the orchestrator. This is the canonical "cheap model adds a lens, doesn't replace the reviewer" case.

## Eval 8 — scanned-PDF vision → enabled `extract-multimodal`

**Setup.** Real roster pages rendered to images (text layer removed), gold derived from the original text layer; the value on the page is the join key. A cheap multimodal model vs the frontier vision anchor, on both clean renders and degraded scans (noise + skew + JPEG artifacts).

**Result.** The cheap model matched the anchor on recall and name↔value pairing with **zero hallucinated values**, on both clean and degraded inputs, at ~1/7 the cost. A second candidate was **rejected** for output-reliability failures and 8× the latency.

**Decision.** Enabled as **`extract-multimodal`** for text-empty (scanned/image) pages only — the `do_not_use_when` steers text-extractable pages to the cheaper text modes, and per-value output still gets a human verify.

---

### The pattern

Each entry is: **real data → scored against the anchor → a strict pass/fail → a mode with a narrow contract, or a retirement with a reason.** When a re-eval later fails, append the new evidence and move the mode to `retired` — never rewrite history.
