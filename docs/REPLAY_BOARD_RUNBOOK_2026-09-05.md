# Replay board runbook (Phase 2 — evaluation as infrastructure)

## The rule
Before merging any accuracy-affecting PR, run the board twice: once on
main, once on the branch. The DIFF between the two boards is the change's
measured effect — against fixed stored extractions, so zero sampling
noise, zero API cost, seconds of wall clock.

```bash
python3 replay_board.py            # score + append to golden/accuracy_history.jsonl
python3 replay_board.py --no-log   # score without recording (exploration)
python3 replay_board.py --determinism  # pricing chain must replay bit-identically
```

## What it scores
15 golden cases (Rider 8 + JW 5 + Northwell + Academy), each against its
component targets, on component-wise mean absolute % error. Subtotal is
recorded but never the score — a subtotal in band with components ±100%
is a coin flip (Northwell rerun3: +8.3% subtotal, walls −34%, ceilings
+138%).

- Tier 1 vector jobs (11) carry the exit criterion: MAE ≤ 10% on two
  consecutive boards → mandatory review lifts for that class.
- Academy 88 is raster-class BY POLICY: scored for visibility, outside
  the ±10% program (VME cannot see scanned sheets; those jobs route to
  the vision path with mandatory review, permanently).

## Why stored rosters, not fresh runs
A fresh run resamples ±30% draw noise at $2.50–7 per job — a fix's
effect is indistinguishable from luck (rounds 1–5, all of August).
Stored rosters make the comparison paired. Fresh extractions are for
NEW golden intake, not for measuring code changes.

## First board (2026-09-05, baseline for the program)
12 scored: MAE 17.9% (hudson) → 131.7% (honey); tier-1 streak 0/2.
Determinism check: build_priced_takeoff replayed BIT-IDENTICAL on 364
Main under the committed prod posture — the deterministic layer holds;
the variance is upstream in extraction, which is the whole Phase 1 case.

## Missing stored results
grenadier_danbury, route22_condo, fishkill_cenhud have no local stored
result.json — they need one archived run each (golden-intake task), then
the board covers 15/15.
