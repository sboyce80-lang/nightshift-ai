# Runbook — Validating Multi-Pass Median on a Live Job

The multi-pass median extraction (commit `4160408`, gated by env var) is
disabled in production after the 2026-05-29 Ridgeview catastrophe where
the per-room match was too strict and the merge produced 0 rooms / a
$335K footprint-fallback estimate. The merge-empty fallback (commit
`43f549b`) catches that case now, but the fix has not been validated on a
live job yet.

## Pre-flight check

1. Confirm worker is on the deploy with the fallback:
   ```bash
   # On Render shell (nightshift-worker-fast):
   git log -1 --oneline
   # Want to see e8a3ab4 or newer.
   ```
2. Confirm no jobs in flight (re-enabling will restart the worker):
   ```bash
   python3 -c "
   from sqlalchemy import select
   from sqlalchemy.orm import Session
   from db import engine
   from models import Submission
   with Session(engine) as ss:
       subs = ss.execute(select(Submission)
           .where(Submission.status.in_(['queued','processing','running']))
           ).scalars().all()
       for s in subs:
           print(f'{s.id}  {s.status}  {s.business_name}')
       if not subs: print('No active jobs.')
   "
   ```
   If anything's running, wait for it to finish (or fail) before enabling
   multi-pass.

## Enable multi-pass

In the Render dashboard, on **nightshift-worker-fast** (the queue
Ridgeview routes to at 9.6 MB / 0 reported pages):

1. **Environment** tab.
2. Add or update:
   - `NIGHTSHIFT_MULTI_PASS = 1`
   - Optionally: `NIGHTSHIFT_MULTI_PASS_N = 3` (default 3, set explicitly
     so the value is auditable)
   - Optionally: `NIGHTSHIFT_MULTI_PASS_KEEP_RATIO = 0.5` (default; tune
     down to 0.3 if the fallback fires too eagerly, up to 0.7 if you want
     stricter merge requirements)
3. Save — triggers a redeploy. Wait for the deploy to finish
   (~2 min); confirm `git log -1 --oneline` on the new shell still shows
   the right commit.

Do the same on **nightshift-worker-heavy** if you also want multi-pass
on the bigger queue (>10 pages or >30 MB).

## Run the validation job

Re-queue Ridgeview (the known case that broke last time):

```bash
# On Render shell:
python3 scripts/requeue_submission.py 79ec14d3 --force --dry-run
# Confirm timeout is ~135 min (the 30-min single-pass bucket times the
# 3x multi-pass multiplier with 1.5x safety factor, baked into
# scripts/requeue_submission.py). If you see 30 min, NIGHTSHIFT_MULTI_PASS
# isn't reaching the requeue script — fix env-var scoping before proceeding.

python3 scripts/requeue_submission.py 79ec14d3 --force
```

## What to watch in the worker logs

A successful multi-pass run prints these markers in order:

```
🔄 Multi-pass median (vector mode): running passes 2..3 of 3
   pass 2: <N> rooms
   pass 3: <N> rooms
📊 Multi-pass median merge: per-pass rooms [N1, N2, N3] → merged Nm
```

Or, if the per-room match is still too strict (expected on some
Ridgeview-class projects), the fallback fires:

```
🔄 Multi-pass median (vector mode): running passes 2..3 of 3
   pass 2: <N> rooms
   pass 3: <N> rooms
⚠️  Multi-pass merge kept K/M rooms (< X required) — falling back to
   pass N (Y rooms, closest to median)
```

Either outcome is success. What we DO NOT want to see is a completed
job with 0 floors / 0 rooms — that's the catastrophic case the fallback
exists to prevent.

## Validate the result against the harness

Pull the resulting JSON, run the corpus regression with just that file:

```bash
# Locally, after downloading the new result from R2:
python3 scripts/regression_corpus.py \
    --corpus output/regression_corpus \
    --glob 'construction_analysis_<timestamp>.json'
```

Look for:
- `_extracted_with_median_of_passes: 3` in the analysis (merged path) OR
  `_multi_pass_median_fallback: true` (fallback path)
- Subtotal lands in the same band as prior single-pass Ridgeview runs
  (~$250K–$350K). If it lands outside that band, the fallback may not be
  firing — pull the worker logs and look for the markers above.

## If something goes wrong

Kill switch — set `NIGHTSHIFT_MULTI_PASS=0` on both worker services and
save. Reverts to single-pass behavior on the next redeploy.

## What constitutes "validated"

Multi-pass is validated when:

1. At least one Ridgeview-class job completes with the `[Multi-Pass Median]`
   note or fallback note in the analysis.
2. Two consecutive runs of the SAME PDF land within ±15% of each other on
   subtotal. The fix's job is to reduce variance — if back-to-back runs
   still produce wildly different numbers, we haven't actually fixed it.

If both pass, leave multi-pass enabled. If not, log the failure with the
worker logs + result JSONs, set the kill switch, and we iterate.
