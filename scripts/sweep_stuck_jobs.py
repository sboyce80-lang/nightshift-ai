"""Sweep zombie submissions — heartbeat + RQ-aware (v2, 2026-06-12).

The worker can die mid-job — Render container restart on deploy, OOM
kill on a heavy PDF, or a hung Claude API call. When that happens the
Submission row stays at status="processing" forever with no result, no
error, no notification (Submission 45c7eca6 sat 5h29m in 'processing'
on 2026-05-29 before anyone noticed).

v1 of this script swept on `updated_at` wall-clock age alone. That was
NEVER deployed — and must not be: nothing touched updated_at while a
job ran, and legitimate DD-scale takeoffs run 90+ minutes, so v1 would
have marked healthy jobs failed at minute 31, emailed the customer
"please resubmit", and then the worker would have flipped the row back
to completed and sent the estimate anyway (the 2026-06 review's worst-
case trust scenario).

v2 rules — a submission is only reaped when BOTH liveness signals are
dead:

  processing/running rows:
    * RQ says the job is active (queued/started/deferred/scheduled)
        -> LEAVE, always. Never kill a job the queue says is running.
    * RQ says inactive (finished/failed/stopped/canceled/missing):
        - heartbeat fresh (< --hb-stale-min)        -> leave (likely a
          completion race; the worker is writing results right now)
        - heartbeat stale                           -> REAP
        - heartbeat NULL (pre-migration row / old worker) -> reap only
          past --legacy-stale-min of updated_at age
  queued rows:
    * RQ job exists in an active state -> leave (waiting behind a long
      job is normal)
    * RQ job missing/inactive past --queued-grace-min -> REAP (the
      enqueue was lost — Redis flush, TTL expiry, failed enqueue)

Reaped rows are marked failed (the transition guard in jobs.update_status
refuses to stomp completed/cancelled), the dead RQ job is deleted so the
DB and queue cannot disagree, and the contact + admins are emailed.

Designed for a Render cron every 5-10 min. Idempotent. Safe to dry-run.

Required env: DATABASE_URL, REDIS_URL. Optional: RESEND_* for email.

Usage:
    python3 scripts/sweep_stuck_jobs.py
    python3 scripts/sweep_stuck_jobs.py --hb-stale-min 15 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# DB imports are deferred into sweep()/_resolve_contact so the pure
# decision core (classify_stuck) stays importable for offline tests on
# machines without sqlalchemy/DATABASE_URL.

logger = logging.getLogger("nightshift.sweep_stuck_jobs")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

ACTIVE_STATES = ("queued", "processing", "running")

# RQ statuses that mean "the queue still owns this job".
RQ_ACTIVE = ("queued", "started", "deferred", "scheduled")

DEFAULT_ERROR_MSG = (
    "Worker process died before finishing the job (likely killed by a "
    "deploy, out-of-memory event, or a hung API call). The submission "
    "was automatically marked failed by the stuck-job watchdog so it "
    "could be retried. Please resubmit through the web app."
)


def classify_stuck(db_status, heartbeat_age_s, row_age_s, rq_status,
                   hb_stale_s=600, queued_grace_s=1800,
                   legacy_stale_s=7200):
    """Pure decision core (offline-testable, no DB/Redis).

    Args:
        db_status: submissions.status ('queued'/'processing'/'running').
        heartbeat_age_s: seconds since heartbeat_at, or None if never set.
        row_age_s: seconds since updated_at.
        rq_status: RQ job status string, or 'missing' if not in Redis.

    Returns (action, reason) where action is 'leave' or 'reap'.
    """
    rq_active = rq_status in RQ_ACTIVE

    if db_status in ("processing", "running"):
        if rq_active:
            # The queue says it's running. A stale heartbeat here means
            # the heartbeat thread died or the job pre-dates it — noisy,
            # but killing a possibly-live job is strictly worse.
            return ("leave", "rq active")
        # RQ no longer owns the job (finished/failed/stopped/missing).
        if heartbeat_age_s is not None and heartbeat_age_s < hb_stale_s:
            return ("leave", "heartbeat fresh — completion race, recheck next sweep")
        if heartbeat_age_s is None:
            if row_age_s >= legacy_stale_s:
                return ("reap", f"no heartbeat support, rq={rq_status}, "
                                f"row idle {row_age_s // 60}min")
            return ("leave", "no heartbeat yet — within legacy grace")
        return ("reap", f"heartbeat stale {heartbeat_age_s // 60}min and "
                        f"rq={rq_status}")

    if db_status == "queued":
        if rq_active:
            return ("leave", "rq active")
        if row_age_s >= queued_grace_s:
            return ("reap", f"queued {row_age_s // 60}min but rq={rq_status} "
                            f"— enqueue lost")
        return ("leave", "queued within grace")

    return ("leave", f"status {db_status} not swept")


def _rq_status_for(sid):
    """Look up the RQ job status for a submission id ('missing' if absent).
    Jobs re-routed fast -> heavy run under '<sid>-heavy'."""
    try:
        from redis import Redis
        from rq.job import Job
        from rq.exceptions import NoSuchJobError
        from config import REDIS_URL
    except ImportError as exc:
        logger.warning("RQ/redis unavailable (%s) — treating as missing", exc)
        return "missing", None
    try:
        conn = Redis.from_url(REDIS_URL)
    except Exception as exc:
        logger.warning("Redis connection failed (%s)", exc)
        return "unknown", None
    for jid in (str(sid), f"{sid}-heavy"):
        try:
            job = Job.fetch(jid, connection=conn)
            return job.get_status(), job
        except NoSuchJobError:
            continue
        except Exception as exc:
            logger.warning("Job.fetch(%s) failed: %s", jid, exc)
            return "unknown", None
    return "missing", None


def _age_seconds(ts, now):
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, int((now - ts).total_seconds()))


def _resolve_contact(session, sub) -> dict:
    """Build the contact_info dict (same shape as web_app passes to
    process_submission) for a submission. Falls back gracefully when
    user/org records can't be loaded."""
    from models import Organization, User
    name = ""
    email = ""
    business = sub.business_name or ""
    try:
        user = session.get(User, sub.user_id)
        if user:
            name = user.name or ""
            email = user.email or ""
    except Exception as exc:
        logger.warning("Could not load User(id=%s): %s", sub.user_id, exc)
    if not business:
        try:
            org = session.get(Organization, sub.org_id)
            if org:
                business = org.name or business
        except Exception as exc:
            logger.warning("Could not load Organization(id=%s): %s",
                           sub.org_id, exc)
    return {
        "name": name,
        "email": email,
        "phone": sub.phone or "",
        "business_name": business,
    }


def _send_stuck_email(contact_info: dict, sub,
                       stuck_minutes: int) -> bool:
    """Email the contact + admin that their job was reaped. Uses Resend
    via notifications._send so it works whether or not Gmail SMTP is
    configured. Returns True on apparent success."""
    try:
        from notifications import _send, notifications_configured, ADMIN_EMAILS
    except ImportError as exc:
        logger.warning("notifications module unavailable: %s", exc)
        return False
    if not notifications_configured():
        logger.warning("Resend not configured — skipping stuck-job email "
                       "for submission %s", sub.id)
        return False

    to_addrs = []
    if contact_info.get("email"):
        to_addrs.append(contact_info["email"])
    for admin in ADMIN_EMAILS or []:
        if admin and admin not in to_addrs:
            to_addrs.append(admin)
    if not to_addrs:
        logger.warning("No recipients (contact+admin) for submission %s",
                       sub.id)
        return False

    subject = "Knight Shift — your submission needs to be resubmitted"
    body = f"""Hi {contact_info.get('name') or 'there'},

Your Knight Shift submission ran into an issue:

  Job ID:     {sub.id}
  Document:   {contact_info.get('business_name') or '(see web app)'}
  Submitted:  {sub.submitted_at}
  Status:     marked failed after {stuck_minutes} minute(s) without
              progress — the processing worker almost certainly died
              mid-job (deploy, container restart, or hung API call).

The original PDF is still in our system; please resubmit it through
the web app and it will be re-queued. We apologize for the friction —
our watchdog is in place so this is caught automatically going forward.

— Knight Shift AI
"""
    return _send(to_addrs, subject, body)


def sweep(hb_stale_min: int = 10, queued_grace_min: int = 30,
          legacy_stale_min: int = 120, dry_run: bool = False,
          send_email: bool = True) -> int:
    """Reap dead-by-both-signals submissions. Returns count reaped."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from db import engine
    from models import Submission

    now = datetime.now(timezone.utc)
    # Pre-filter: nothing younger than 5 min is ever a candidate; keeps
    # the row scan cheap.
    prefilter_cutoff = now - timedelta(minutes=5)
    reaped = 0

    with Session(engine) as session:
        q = (sa.select(Submission)
             .where(Submission.status.in_(ACTIVE_STATES))
             .where(Submission.updated_at < prefilter_cutoff)
             .order_by(Submission.updated_at.asc()))
        candidates = session.execute(q).scalars().all()

        if not candidates:
            logger.info("No active submissions older than 5 min.")
            return 0

        logger.info("Examining %d candidate submission(s):", len(candidates))
        for sub in candidates:
            hb_age = _age_seconds(getattr(sub, "heartbeat_at", None), now)
            row_age = _age_seconds(sub.updated_at, now) or 0
            rq_status, rq_job = _rq_status_for(sub.id)

            action, reason = classify_stuck(
                sub.status, hb_age, row_age, rq_status,
                hb_stale_s=hb_stale_min * 60,
                queued_grace_s=queued_grace_min * 60,
                legacy_stale_s=legacy_stale_min * 60,
            )
            logger.info("  • %s status=%s hb_age=%s row_age=%dmin rq=%s -> %s (%s)",
                        sub.id, sub.status,
                        f"{hb_age // 60}min" if hb_age is not None else "never",
                        row_age // 60, rq_status, action.upper(), reason)
            if action != "reap" or dry_run:
                continue

            contact = _resolve_contact(session, sub)
            stuck_min = row_age // 60
            # Delete the dead RQ job FIRST so DB and queue can't disagree
            # (a later worker pickup can't resurrect a row we just failed;
            # the update_status transition guard is the second lock).
            if rq_job is not None:
                try:
                    rq_job.delete()
                except Exception as exc:
                    logger.warning("Could not delete RQ job %s: %s",
                                   sub.id, exc)
            sub.status = "failed"
            sub.error = (
                f"[Watchdog v2] {DEFAULT_ERROR_MSG} "
                f"(reaped: {reason})"
            )
            session.commit()
            reaped += 1

            if send_email:
                ok = _send_stuck_email(contact, sub, stuck_min)
                logger.info("    notification: %s",
                            "sent" if ok else "skipped/failed")

    logger.info("Reaped %d submission(s).", reaped)
    return reaped


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--hb-stale-min", type=int, default=10,
                   help="Heartbeat older than this is stale (default 10)")
    p.add_argument("--queued-grace-min", type=int, default=30,
                   help="Queued rows missing from RQ are reaped after "
                        "this (default 30)")
    p.add_argument("--legacy-stale-min", type=int, default=120,
                   help="Rows with NO heartbeat (pre-migration) need this "
                        "much updated_at age + inactive RQ (default 120)")
    p.add_argument("--threshold-min", type=int, default=None,
                   help="DEPRECATED v1 flag — mapped to --legacy-stale-min "
                        "(floored at 60) for cron-arg compatibility")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what WOULD be reaped but don't change DB")
    p.add_argument("--no-email", action="store_true",
                   help="Skip sending notification emails")
    args = p.parse_args()

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set")
        return 2

    legacy = args.legacy_stale_min
    if args.threshold_min is not None:
        # v1 compat: old cron definitions passed --threshold-min 30; that
        # aggressiveness is exactly what v1 got wrong. Honor it only as
        # the legacy-row threshold, floored at 60 min.
        legacy = max(60, args.threshold_min)

    sweep(hb_stale_min=args.hb_stale_min,
          queued_grace_min=args.queued_grace_min,
          legacy_stale_min=legacy,
          dry_run=args.dry_run,
          send_email=not args.no_email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
