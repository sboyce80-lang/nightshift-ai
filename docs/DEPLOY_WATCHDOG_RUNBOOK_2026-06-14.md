# Runbook — safely ship the v2 stuck-job watchdog (tracker item #1)

**Date:** 2026-06-14 · **Why:** prod has **no working watchdog** (`sweep_stuck_jobs.py` docstring: "v1 was NEVER deployed"; [[stuck-jobs-watchdog-missing]]) → zombie `processing` jobs never reaped. The v2 watchdog is coded + wired on `phase0-review-fixes` but **must be deployed in a specific order or it reaps healthy long jobs.** This is **independent of the golden regression** (job-reliability infra, not extraction accuracy) — it can ship on its own (cherry-pick) ahead of the extraction-flag merge.

## Corrected risk picture (was overstated in the master tracker)
The v2 code is safe-by-design: it reaps only when **RQ inactive AND heartbeat stale**, and falls back gracefully when heartbeat is NULL. The failure modes from wrong ordering are **bounded**, not catastrophic:

| Wrong action | Consequence | Severity |
|---|---|---|
| Cron live **before** migration 0022 | sweep ORM query references `Submission.heartbeat_at` → **column missing → cron errors, no-ops** | low (broken, not harmful) |
| Cron live **after** migration but **before** heartbeat-writing worker | all heartbeats NULL → falls to `--legacy-stale-min` (default **120 min**) → **healthy jobs running >2h get marked failed** (DD-scale heavy jobs, `job_timeout` up to 7200s) | **medium — the real care-point** |
| Correct order | reaps only genuinely dead jobs | safe |

Second safety net: `_BLOCKED_TRANSITIONS` (jobs.py:67) prevents stomping a `completed`/`cancelled` job — but does NOT protect a live `processing` job from being failed, so ordering still matters.

## Safe deploy order
1. **Run migration `0022_add_heartbeat_progress_routing`** (adds nullable `heartbeat_at`, `progress`, `queue_name`, `job_timeout`). Online-safe (nullable cols, no rewrite), reversible downgrade present. Verify columns exist before step 3.
2. **Deploy the worker build** containing `_start_heartbeat` (jobs.py:119) so running jobs actively write `heartbeat_at` every ~60s. Confirm fresh jobs populate `heartbeat_at` in the DB.
3. **Enable the v2 cron** (`render.yaml:109`, `*/10`, `sweep_stuck_jobs.py --hb-stale-min 10 --queued-grace-min 30`).
4. **First run `--dry-run`** (the script supports it) and inspect what it *would* reap before the live cron acts. Consider a larger `--legacy-stale-min` (e.g. 240) for the first day as a belt-and-suspenders against any worker not yet heartbeating.

## Verification after deploy
- A deliberately-killed worker mid-job → row reaped within ~10 min (heartbeat stale + RQ inactive).
- A genuine long (>2h) heavy job that IS heartbeating → **not** reaped.
- A lost-enqueue (Redis flush) queued row → reaped past `--queued-grace-min` (30 min).

## Rollback
Disable the cron (no data change). If needed, `alembic downgrade` drops the four nullable columns cleanly.

## Note on prod divergence
Memory records prod diverged from `render.yaml`. **Confirm the actual running cron + worker image in the Render dashboard** before/after — "in render.yaml on the branch" ≠ "running in prod." This runbook assumes a controlled re-convergence of prod to the branch's render.yaml.
