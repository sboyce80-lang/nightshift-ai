# Incident: Rider's stuck jobs on knightshiftai.com — 2026-04-30

## Summary for the Rider team

Over the past two days, several of Rider's submissions on knightshiftai.com
have appeared to "error out" — the job sits in the **Processing** state forever,
no email arrives, and no estimate is ever produced. We confirmed this is a
silent worker crash on our side, not a bug in your uploads. The same files
that "disappeared" once also produced a complete $96K estimate on a different
attempt — the failure is intermittent infrastructure pressure, not anything
wrong with your drawings.

Your data is safe. The original PDFs are still in our storage and we are
re-running the affected jobs today.

## What was happening

The webapp routes large submissions (DD-scale architectural sets with
hundreds of pages and 100+ MB PDFs) to a dedicated **heavy worker** on Render.
That worker is currently provisioned with **2 GB of RAM**. When Rider's
SWS5 / Architectural set (336 MB across 3 PDFs) is processed, peak memory
during the building-inventory and per-page extraction phases brushes right
up against that 2 GB ceiling. Sometimes it fits. Sometimes it doesn't —
and when it doesn't, the Linux OOM-killer terminates the worker process
instantly.

When the worker is killed by the kernel:
- Python doesn't get to run any cleanup code, so the database row never
  flips from "processing" to "failed."
- No error email is sent.
- The job appears frozen in the UI with no diagnostic message.
- A new worker boots, the old job is marked `AbandonedJobError`, but the
  user-facing state is unchanged.

That's why these jobs read as "errored out" with "0 results" — the
processing run was killed, but the bookkeeping never caught up.

## Confirmed evidence

Four submissions are stuck in "processing" with no error message:

| Submitted (UTC) | Submitter | Files | Size |
|---|---|---|---|
| 04-30 19:46 | Steve Boyce (test) | 4 PDFs (SUMMIT) | 432 MB |
| 04-30 18:14 | Elliott @ Rider | 3 PDFs (SWS5) | 336 MB |
| 04-30 11:53 | Elliott @ Rider | 3 PDFs (SWS5) | 336 MB |
| 04-29 12:00 | Elliott @ Rider | 2 PDFs (school repaint) | 32 MB |

Render's infrastructure logs show explicit `oomKilled` events on the heavy
worker at:
- 2026-04-30 19:03:08 UTC — killed Elliott's 18:14 submission mid-extraction
- 2026-04-30 12:58:09 UTC — killed Elliott's 11:53 submission mid-extraction

The worker's RAM limit is recorded as `2Gi` in those events. The worker
process had been running normally for 30–45 minutes, talking to the
Anthropic API, before being killed without warning.

For comparison, a successful run of the same SWS5 file set on 04-30 at
15:16 UTC completed in 1h 28m and produced a $96,429.90 estimate. Same
files, different memory peak, different outcome.

## Why Streamlit kept working

The Streamlit version (the one Rider has been using as a fallback) is
architecturally different in one important way: it runs the analysis in
a **subprocess** with its own memory cap. When the subprocess is killed,
the parent Streamlit process survives, sees the exit code, and updates
the job status. The webapp's worker runs the analysis **in-process** —
when the worker dies, there is no parent left to record the failure.

Both code paths share the exact same takeoff engine (`Takeoff_DIRECT.py`),
so the quality of the output should be identical. The only differences
are the user interface and the failure-recovery wrapper.

## Immediate fix (applied today, 2026-04-30)

1. **Heavy worker upgraded from 2 GB to 4 GB of RAM** (Render Pro plan).
   This gives the building-inventory and per-page extraction phases
   enough headroom to handle DD-scale jobs without bumping the ceiling.

2. **All four stuck submissions are being re-enqueued** so Rider receives
   the estimates that were lost. Email notifications will fire normally
   on completion.

3. The pre-existing `nightshift-worker` service (a stale leftover from
   before the fast/heavy queue split) is being decommissioned — it was
   not consuming jobs but was still billing.

## Near-term hardening (next 1–2 weeks)

The 4 GB bump fixes today's failure mode but doesn't fix the underlying
"silent crash" class of problem. Two follow-ups are queued:

1. **Crash-aware reconciliation.** On worker startup, scan for any
   `submissions.status='processing'` whose RQ job is in the abandoned
   registry and flip them to `failed` with a clear error message + email.
   This way, even if a future OOM happens, the user is notified within
   minutes instead of staring at a frozen "Processing" indicator.

2. **Subprocess isolation in the webapp.** Match Streamlit's pattern:
   run `run_analysis` in a child process so a kernel-level kill becomes
   a normal exit code we can react to. Defense-in-depth alongside the
   memory bump.

## Real-scale roadmap (next 1–2 months)

A single 4 GB worker still processes only **one DD-scale job at a time**.
If two large Rider jobs land at once, the second waits in queue for the
duration of the first (typically 30–90 minutes). Adding more workers
on Render is possible but expensive — every instance is billed 24/7
whether or not it's processing anything.

The right shape for this workload is **per-job ephemeral compute**: each
submission spins up a fresh container with generous RAM, runs the takeoff,
exits, and bills only for the seconds it ran. Candidates being evaluated:

- **Modal** — Python-native, fastest to integrate, billed per-second.
- **Fly Machines** — flexible, also per-second.
- **AWS Batch / GCP Cloud Run Jobs** — heavier setup but most cost-efficient
  at sustained volume.

The webapp, database, and queue stay where they are; only the heavy
analysis function moves. Estimated effort: 1–2 days of engineering plus
production cutover. This is what unlocks "Rider can submit five DD-scale
sets simultaneously without anyone waiting."

## What Rider should do today

1. **Do not resubmit the four stuck jobs manually** — we are re-running
   them on your behalf with the new memory headroom. You'll receive the
   estimate emails as they finish.
2. New submissions on knightshiftai.com from this point forward will
   route to the upgraded worker.
3. The Streamlit fallback remains available if you'd like a second-path
   sanity check on any specific run.

We'll update this doc with the re-run results once the queue is drained.
