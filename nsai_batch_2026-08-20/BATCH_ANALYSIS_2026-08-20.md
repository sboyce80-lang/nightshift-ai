# JW Estimates Validation Batch — 2026-08-20 Overnight Run

**Setup:** 5 jobs from the "JW Estimates" Drive folder run through KnightShift at
SHA `04b6807` (prod `0b5e80c` + annotation-strip guard), prod-equivalent flags,
`NIGHTSHIFT_MARKUP_TAKEOFF=0`, and **all JW markups physically stripped from the
PDFs before processing** (≈1,300 annotation objects removed; verified visually —
the raw MARKUPS sheets contain JW's full quantity legends, so any run on
unstripped PDFs is circular). Fresh, blind takeoffs; compared line-by-line
against JW's takeoff spreadsheets. Local sequential run under caffeinate,
multi-pass, 3.87h wall clock, zero job crashes.

**Blocked:** Liberty Saratoga North/South and Cedar Point have takeoff xlsx but
NO plan PDFs anywhere in the shared Drive — unrunnable until plans are provided.

## Scoreboard

| Job | JW bid | KnightShift | Δ | Manual review | Headline cause |
|---|---|---|---|---|---|
| Harlem Valley (26-376) | $43,491 | $2,691 | **−94%** | ✅ flagged | extraction collapse on 1-page mega-sheet |
| Hudson Hotel (26-390) | $146,024 | $46,427 | **−68%** | ✅ flagged | unit-typical multiplication; exterior + windows missed |
| Caris Hyde Park (26-385) | $87,609 | $43,784 | **−50%** | ✅ flagged | floor-plan sheet lost to parse-death; exterior + windows |
| Under Canvas ULUM | $41,575 | $50,506 | **+21%** | ✅ flagged | offsetting errors (walls 2x over, floors missed, L5 added) |
| Homewood Suites (RP 26-002) | $1,322,983 | $673,843 | **−49%** | ✅ flagged | 6 pages lost; WC schedule guessed not read |
| **Total** | **$1,641,682** | **$817,251** | **−50.2%** | 5/5 | |

**Fail-safe verdict: 5/5 estimates were correctly flagged manual-review — none
would have auto-shipped to a customer.** Accuracy verdict: none is usable as-is.

## What worked

- **Door counts, when sheets parsed:** Caris 63 vs 67; Homewood 372 vs 393;
  ULUM 28 vs 26. Door pipeline is near-estimator-grade.
- **Ceilings on parsed sheets:** Caris −18%; Homewood −33% despite losing 6 pages.
- **Railings:** Hudson $191 vs $190.
- **Fail-safe machinery:** manual-review gates, missing-page disclosure,
  calibrated-confidence hard gate, VME abstention logic all fired correctly.
- **VME correctness:** on Harlem, VME shadow measured 1,372 LF of wall runs
  (≈13.7k SF vs JW 15.7k) — the geometry was RIGHT — and its refusal to promote
  without height anchors was principled. The data was there; the plumbing wasn't.

## Failure taxonomy (ranked by $ impact × frequency)

### F1 — Response-parse death (P0, hit ALL 5 jobs)
Structured-output schema rejected by API ("compiled grammar is too large", 400)
→ text-mode fallback → huge responses (68–95k chars) open with narration
("I'll systematically analyze…") and/or truncate mid-JSON → all repair attempts
fail → sheet dropped. Casualties: Harlem entire set (1 room survived); Hudson
A7.01 finish plan; Caris A1.2 floor plan (~half the rooms); ULUM A201/A301/A500;
Homewood p3,8,13,19,23,25 (21% of set, incl. WC schedule pages).
Direct damage ≈ $700k+ of the total $824k shortfall.

**Fixes:**
1. Shrink/split the structured-output grammar below the API limit (split the
   room-extraction schema into per-section calls, or drop optional fields);
   grammar 400 should be impossible, not a fallback trigger.
2. Harden text-mode recovery in the JSON repair path: strip narration prefix to
   first `{`, balance-close truncated JSON, and re-request continuation when
   `finish_reason=length` instead of discarding 95k chars of good data.
3. Deploy the **failed-sheet retry** already built on the `failed-sheet-retry`
   worktree (commit `98a0b10`, flag `NIGHTSHIFT_FAILED_SHEET_RETRY`, currently
   OFF and unmerged) — end-of-run auto-retry of failed sheets was designed for
   exactly this and would have salvaged every job above.
4. Retry an unparseable sheet once with a smaller ask (rooms-only, no notes) —
   a 95k-char response is a symptom of over-asking one call.

### F2 — Schedule quantities not captured as authoritative (P0)
JW's accuracy comes from reading schedules; KS guessed where schedules existed:
- **Homewood WC schedule** (per-type SF: WC-01 87,219 …) never captured → WC
  approximated per-room ("~50% of wall area", "~480 SF/unit") → −92k SF, and the
  same walls double-erred into painted-gyp lines. Cost ≈ $700k on this job alone.
- **Window schedules:** "door/window schedule(s) not detected" on Hudson AND
  Homewood; windows priced $0 on all 5 jobs vs JW $40k + $7.4k + $43k
  (stool/apron + sash ops). The hard-numbers gate then correctly zeroes them —
  garbage-in for a good gate.
- **Door schedules:** not found on Hudson (doors from plan counts only, 62 vs 197).

**Fixes:**
5. WC-schedule authoritative gate: when a wallcovering schedule with per-type SF
   exists, it overrides per-room WC estimates (mirror of `NIGHTSHIFT_WC_SCHEDULE_GATE`
   / VME-authoritative pattern), and those SF are subtracted from painted-wall scope.
6. Window scope pass: targeted window-schedule extraction (the text-scan already
   finds the pages — Hudson log: "Window schedule detection: found in upload")
   + count from elevations as fallback; price stool/apron and sash as separate
  ops like JW does. Zero-with-RFI stays only when genuinely absent.
7. Extend targeted-schedule extraction (PR #15 pattern) to door schedules on
   multi-building/unit-plan sets.

### F3 — Unit-typical multiplication (P0 for hospitality/multifamily)
Hudson guestroom scope multiplied typicals by units *drawn* (1–3) instead of the
unit-mix × floor count (150 keys): walls 22,940 vs 72,382 SF, doors 62 vs 197.
Same class as July's "multifamily over-extracts ~2x" — either direction, the
unit-count basis is non-deterministic. (LaGrange fix-list item, still open.)

**Fix 8:** deterministic unit-mix matrix: extract per-floor unit counts from
code/life-safety plans or unit schedules; require `typical_sqft × unit_count`
provenance on every multiplied quantity; manual-review when absent.

### F4 — Elevation/exterior scope invisibility (P1)
Exterior scope printed on elevation sheets is never read: Hudson power-washing
spec block ("±24,652 SF facade", $40.9k) and 577 LF tuck-pointing note; Caris
siding/columns/fascia ($20.8k). Elevation sheets aren't room-extraction targets
and nothing else consumes their text scope. (Related: `NIGHTSHIFT_RESIDENTIAL_ELEV_PASS`
exists but is commercial-blind — the 2026-07-21 Biddle finding, inverse case.)

**Fix 9:** elevation-sheet scope pass on ALL building classes: extract exterior
finish keynotes, power-wash/cleaning spec blocks, and painted-element callouts
(the scope-sweep Haiku pass from 7/06, `NIGHTSHIFT_SCOPE_SWEEP`, is the natural
vehicle — currently uncommitted/undeployed).

### F5 — Floor-coating wiring gap (P2, hard bug)
ULUM extraction explicitly logged "EP-1 (Epoxy) confirmed in Kitchen + Kitchen
Storage" — and the estimate priced $0 epoxy. Extracted floor finishes never
reach cost lines (epoxy/sealed-concrete line items exist in the pricing model).
JW floors missed: ULUM $18.9k, Homewood $10.2k, Harlem $13.0k.

**Fix 10:** map extracted floor-finish codes (EP-x, SC-x, sealed) → cost lines;
polished-concrete (PC-1) stays excluded-by-policy but must emit a priced-option
RFI line, not silence.

### F6 — Over-extraction on large DD sets (P2)
ULUM walls +71% / ceilings +122%: RCP-scaled approximate dims admitted into
quantities, multi-building room-numbering conflicts, dedup helping (removed
19.4k dup SF) but insufficient. Offsetting errors produced a deceptively good
+21% total — the LaGrange "identical PDF, offsetting errors" hazard.

**Fix 11:** quarantine scaled/approximate dims (RCP "do not use as final" notes
are already in the extraction!) from pricing unless corroborated by a dimensioned
plan or VME; keep as shadow.

### F7 — Judgment divergences (not bugs — Steven to adjudicate)
- **Level 5 finish:** priced by KS on Hudson ($13.5k, D18 keynote) and ULUM
  ($13.0k, finish schedule "Painted LVL 5") — REAL scope per documents; JW
  absorbs it in rates. Recommend a separately-labeled allowance line so an
  estimator can strike it, instead of silent base-bid inflation.
- **Polished vs sealed concrete** (ULUM $10.4k): KS's hard-numbers exclusion vs
  JW pricing seal-on-polish. Needs a policy call + RFI line either way.
- **Door frames vs full paint** (Homewood): count right, type/rate differ
  ($39.3k JW frames-only vs $59.1k KS full-paint) — door-type reconciliation
  (PR #25 class) should read "frames only" from the schedule.

## Recommended fix order (impact-first)

| # | Fix | Est. impact on this batch |
|---|---|---|
| 1 | F1.1–F1.4 parse robustness + failed-sheet retry | recovers ~$700k of missed scope across 5/5 jobs |
| 2 | F2.5 WC-schedule authoritative | Homewood −49% → est. ±15% |
| 3 | F2.6 window scope pass | +$90k across 3 jobs |
| 4 | F3.8 unit-mix determinism | Hudson walls/doors ~3x correction |
| 5 | F4.9 elevation scope pass | +$62k across 2 jobs |
| 6 | F5.10 floor-finish wiring | +$42k across 3 jobs |
| 7 | F6.11 scaled-dim quarantine | ULUM composition fix |

All to be built flag-gated per house rules, validated on this batch's PDFs
(now a local golden set with parsed JW ground truth JSONs) before any deploy.

## Harness/method notes
- run_one.py recorded `manual_review=False` in run_meta while result.json says
  True (read the key before the pipeline set it) — harness bug, cosmetic;
  result.json is authoritative. Fixed in any rerun.
- Comparator auto-bucketer misclassifies multi-keyword lines; per-job tables
  above are hand-mapped from raw lines (see per-job comparison_*.md appendices).
- Per-job artifacts: `<job>/result.{json,pdf}`, `run_multi.log`,
  `comparison_<job>.md`, `ground_truth*.json`, `plans_clean.pdf`.
