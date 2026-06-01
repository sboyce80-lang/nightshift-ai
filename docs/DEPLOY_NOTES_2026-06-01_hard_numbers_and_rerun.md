# Deploy Notes — Hard-Numbers Pricing + No-File Re-Runs Without Will Inflation

- **Date:** 2026-06-01
- **Branch:** `deploy/hard-numbers-and-rerun-fix`
- **Theme:** Stop the estimating engine from adding scope/cost it did not measure.

This deploy bundles two related anti-inflation efforts:

1. A **Hard-Numbers-Only pricing policy** that suppresses heuristic scope-fabrication
   across the engine (the larger, pre-existing initiative).
2. **No-file re-runs** plus **disabling Will on re-runs** (the re-run feature work).

Both push in the same direction: every dollar should trace to a measured quantity,
and genuine gaps become RFIs for a human instead of guessed numbers.

> ⚠️ **Highest-impact item:** `HARD_NUMBERS_ONLY = True` changes core pricing for
> **every new job**, not just re-runs. Validate against representative jobs before
> push — it lowers quotes wherever heuristics were compensating for under-extraction.

---

## 1. Hard-Numbers-Only policy

New flag in `config.py`:

```python
HARD_NUMBERS_ONLY = True
```

When `True`, the engine prices **only** quantities measured from the drawings.
Paths that fabricate scope from heuristics (perimeter / footprint / keyword /
building-type assumptions used when extraction found nothing) are suppressed; the
gap becomes an RFI. Compensating boosts that scale an **already-measured (>0)**
quantity are *not* affected. Set `False` to restore the old heuristic-fill behavior.

### Heuristics suppressed when the flag is on

| File / function | Heuristic now off | Effect |
|---|---|---|
| `will_synthesis.run_will_synthesis` | Every Will quantity adjustment → converted to an RFI instead of applied | Will stops reshaping measured quantities toward "typical" ranges on **all** runs; still writes scope narrative + RFIs |
| `Takeoff_DIRECT._apply_schedule_overrides` | Fabricating exterior paint sqft for commercial buildings from note/legend keywords | No guessed exterior paint |
| `Takeoff_DIRECT._recalculate_totals` | Synthesizing rooms from unit-mix when 0 rooms but ≥4 units | No invented residential rooms |
| `Takeoff_DIRECT._recalculate_totals` | Reclassifying "EXPOSED" ceilings → dryfall by building-type keyword | No guessed dryfall ceiling |
| `Takeoff_DIRECT._recalculate_totals` | Fabricating wallcovering (accent walls) when 0 | No guessed wallcovering |
| `Takeoff_DIRECT._recalculate_totals` | Fabricating wallcovering from a bathroom heuristic when no finish schedule | Same |
| `Takeoff_DIRECT._recalculate_totals` | Fabricating stained-wood sqft from keywords | No guessed stain scope |
| `Takeoff_DIRECT._recalculate_totals` | Boosting CMU concrete floor to full room floor area | No assumed bare-concrete sealing beyond measured |
| `Takeoff_DIRECT.calculate_costs` | Estimating cornice LF from footprint × stories | No guessed cornice |
| `Takeoff_DIRECT.calculate_costs` | Estimating stain siding from building envelope when notes mention stain | No guessed stain siding |
| `Takeoff_DIRECT.calculate_costs` | **Footprint × rate pricing** substituting for measured per-room line items | **Biggest swing** — extracted room scope is no longer discarded for a building-size estimate |
| `Takeoff_DIRECT.run_analysis` | Geometry-based stair-count estimate + its undercount "boost" | Trusts the extracted/parsed stair count; gaps become RFIs |

**Net effect:** estimates become more conservative and defensible. Genuinely-missing
scope shows as `$0` + an RFI rather than a silent guess, so quotes generally come in
lower (or simply un-padded) wherever those heuristics used to fire. Magnitude depends
on how often they fired on a given job.

Validation helper: `scripts/verify_hard_numbers_ridgeview.py` (offline replay proving
the policy suppresses fabricated scope on the Ridgeview job).

---

## 2. New extraction safety-net ("Check 9")

`_validate_extraction` gains a `project_overview` parameter and a new check that
compares total extracted room area against the cover-sheet declared GSF / work area.
If extracted area falls outside **85–120%** of declared, it emits a `[HIGH]` warning.
`run_analysis` now passes `project_overview` through.

**Effect:** catches gross under/over-extraction before it becomes a bad quote —
specifically the Dobbin Rd renovation case where the viewport selector measured the
**EXISTING** plan instead of **PROPOSED**. Warning/RFI only; not an auto-correction.

---

## 3. No-file re-runs (re-run feature)

The "Re-run with revisions" flow on a completed job no longer requires a file upload.

- **Frontend** (`templates/job_detail.html`): file input is optional; copy updated;
  client-side validation blocks only a fully-empty submit (no file **and** no notes).
- **Backend** (`web_app.py` `resubmit`): a submit with notes but no file is accepted
  and routed through the existing merge path with zero new files.
- **Worker** (`Takeoff_DIRECT.run_analysis_merge`): with no new file it re-runs off the
  stored result JSON — **no architectural re-extraction**. It skips the non-idempotent
  post-extraction passes (which were re-applying supplements, ~+18% phantom inflation)
  and **carries the prior cost estimate forward verbatim**.

**Effect:** a re-run with no new file is cheap (no LLM extraction) and never inflates;
a no-op note reproduces the prior subtotal exactly. With-file re-runs still integrate
the new files and re-price off the parent's rate snapshot.

---

## 4. Will synthesis disabled on re-runs

Will's senior-estimator pass proposes ±25% line-item adjustments whose upward edits
were padding re-run quotes. It is now **disabled on all re-runs by default**.

- Env toggle: `NIGHTSHIFT_WILL_ON_RERUN` (default `0` = off). Set to `1` to restore
  Will on re-runs.
- Fresh first-time submissions are **unaffected** and still run Will (and with the
  Hard-Numbers policy on, Will can't inflate there either — its adjustments become RFIs).

---

## 5. Housekeeping in this deploy

- **Model bumps:** `claude-sonnet-4-20250514` → `claude-sonnet-4-6` in `NYP_rfp.py`,
  `NY_rfp.py`, `TAKEOFF.py`, `takeoff_plan.py`, `will_synthesis.py`.
- **Will adaptive-thinking A/B hook** (`will_synthesis.py`): optional
  `use_adaptive_thinking` / `effort` params, **off by default** — no effect unless enabled.
- **New dev/diagnostic scripts** (not imported by the app at runtime; deploy-safe):
  `scripts/auto_takeoff.py`, `scripts/normalize_pdf.py`, `scripts/pull_ridgeview_run.py`,
  `scripts/verify_hard_numbers_ridgeview.py`, `verify_ridgeview_dedup.py`,
  `scripts/diagnose_regression_bisect.sh`, `scripts/diagnose_regression_my_fixes.sh`.

---

## Rollback / toggles

| Behavior | Toggle | Default |
|---|---|---|
| Hard-numbers pricing | `config.HARD_NUMBERS_ONLY` | `True` (set `False` to restore heuristic fill) |
| Will on re-runs | env `NIGHTSHIFT_WILL_ON_RERUN` | `0` / off (set `1` to restore) |

Full rollback is `git revert` of the deploy commit; the two toggles above let you
disable the headline behaviors without a redeploy.

---

## Verification status

- All changed `.py` files compile; both `.sh` scripts pass `bash -n`.
- No-file re-run smoke-tested against two real result JSONs: Will provably not called,
  subtotal reproduces the prior exactly (delta `$0.00`), JSON + PDF generated.
- **Not yet run on the live stack:** a real end-to-end run with the Will API + a true
  with-file merge regression, and broad validation of `HARD_NUMBERS_ONLY` beyond the
  Ridgeview replay. Confirm before/after push as appropriate.
