"""Sweep zombie submissions stuck in queued/processing/running state.

The worker can die mid-job — Render container restart on deploy, OOM
kill on a heavy PDF, or a hung Claude API call without timeout. When
that happens the Submission row stays at status="processing" forever
with no result, no error, no notification. Steve hits the symptom
hours later asking "is my job stuck?" (see Submission 45c7eca6 on
2026-05-29: sat in 'processing' for 5h 29m before anyone noticed).

This script sweeps the table once: any submission in queued /
processing / running whose updated_at is older than --threshold-min
(default 30) is marked 'failed' with an explanatory error, and the
contact + admin get a notification email so the customer knows to
resubmit instead of staring at a silent dashboard.

Designed to run on a Render cron schedule (every 5–10 min). Idempotent
(a swept job stays 'failed', won't be re-emailed). Safe to dry-run.

Required env vars:
    DATABASE_URL          # always
    RESEND_API_KEY        # optional but recommended; without it the
    RESEND_FROM_EMAIL     # script still marks jobs failed but won't
    ADMIN_EMAILS          # send notifications.

Usage:
    python3 scripts/sweep_stuck_jobs.py                  # default 30 min
    python3 scripts/sweep_stuck_jobs.py --threshold-min 60
    python3 scripts/sweep_stuck_jobs.py --dry-run
    python3 scripts/sweep_stuck_jobs.py --no-email
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

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db import engine
from models import File, Organization, Submission, User

logger = logging.getLogger("nightshift.sweep_stuck_jobs")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# States that indicate the worker has (or had) a job in flight.
# When updated_at on one of these is older than threshold, the worker
# almost certainly died mid-job.
ACTIVE_STATES = ("queued", "processing", "running")

DEFAULT_ERROR_MSG = (
    "Worker process died before finishing the job (likely killed by a "
    "deploy, out-of-memory event, or a hung API call). The submission "
    "was automatically marked failed by the stuck-job watchdog so it "
    "could be retried. Please resubmit through the web app."
)


def _resolve_contact(session: Session, sub: Submission) -> dict:
    """Build the contact_info dict (same shape as web_app passes to
    process_submission) for a submission. Falls back gracefully when
    user/org records can't be loaded."""
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


def _send_stuck_email(contact_info: dict, sub: Submission,
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


def sweep(threshold_min: int = 30, dry_run: bool = False,
          send_email: bool = True) -> int:
    """Mark all stuck-active submissions as failed. Returns count swept."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    swept = 0

    with Session(engine) as session:
        q = (sa.select(Submission)
             .where(Submission.status.in_(ACTIVE_STATES))
             .where(Submission.updated_at < cutoff)
             .order_by(Submission.updated_at.asc()))
        stuck = session.execute(q).scalars().all()

        if not stuck:
            logger.info("No stuck submissions older than %d min.",
                        threshold_min)
            return 0

        logger.info("Found %d stuck submission(s) older than %d min:",
                    len(stuck), threshold_min)
        for sub in stuck:
            age = (datetime.now(timezone.utc) -
                   sub.updated_at.replace(tzinfo=timezone.utc)
                   if sub.updated_at.tzinfo is None
                   else datetime.now(timezone.utc) - sub.updated_at)
            stuck_min = int(age.total_seconds() / 60)
            logger.info("  • %s  status=%s  updated %d min ago",
                        sub.id, sub.status, stuck_min)

            if dry_run:
                continue

            contact = _resolve_contact(session, sub)
            sub.status = "failed"
            sub.error = (
                f"[Watchdog 2026-05-29] {DEFAULT_ERROR_MSG} "
                f"(was '{ACTIVE_STATES}' state for {stuck_min} min before sweep)"
            )
            session.commit()
            swept += 1

            if send_email:
                ok = _send_stuck_email(contact, sub, stuck_min)
                logger.info("    notification: %s",
                            "sent" if ok else "skipped/failed")

    logger.info("Swept %d submission(s).", swept)
    return swept


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--threshold-min", type=int, default=30,
                   help="Mark failed if updated_at older than this many "
                        "minutes (default 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what WOULD be swept but don't change DB")
    p.add_argument("--no-email", action="store_true",
                   help="Skip sending notification emails")
    args = p.parse_args()

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set")
        return 2

    sweep(threshold_min=args.threshold_min,
          dry_run=args.dry_run,
          send_email=not args.no_email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
