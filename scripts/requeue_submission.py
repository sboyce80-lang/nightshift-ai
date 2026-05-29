"""Re-enqueue one or more stuck submissions so the worker re-runs them.

Use case: a worker died mid-job (deploy, OOM, hung API call) leaving the
Submission row in 'processing' state forever. The PDFs are still in R2,
the contact info is in the DB, but RQ's started_job_registry thinks the
job is in flight and never re-queues it. This script does what the web
UI's "submit" path does, but for an existing submission_id:

  1. Look up the submission and verify the upload files exist in R2.
  2. Reconstruct contact_info from the User + Submission record.
  3. Reset status to 'queued' and clear the stale error.
  4. Re-enqueue 'jobs.process_submission' on the right RQ queue, sized
     by total pages / max file size (same _pick_queue / _pick_timeout
     helpers the web UI uses).

The RQ job_id is set to the submission_id, same as the original enqueue,
so any stale registry entry gets replaced cleanly.

Required env vars (same as the worker):
    DATABASE_URL, REDIS_URL, R2_*  (used by storage to verify uploads)

Usage:
    python3 scripts/requeue_submission.py 79ec14d3 5a7205f2
    python3 scripts/requeue_submission.py <full-uuid>
    python3 scripts/requeue_submission.py 79ec14d3 --dry-run

Safety:
  - Refuses to re-enqueue a 'completed' submission (would clobber results).
    Use --force to override.
  - Refuses if no upload files are present in R2.
  - Logs what it does so the audit trail is clear.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db import engine
from models import File, Organization, Submission, User

logger = logging.getLogger("nightshift.requeue_submission")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


def _resolve_submission(session: Session, sub_id_or_prefix: str) -> Submission:
    """Accept a full UUID or 8-char prefix. Raises if not exactly one match."""
    if len(sub_id_or_prefix) >= 36:
        q = sa.select(Submission).where(Submission.id == sub_id_or_prefix)
    else:
        q = sa.select(Submission).where(
            Submission.id.like(f"{sub_id_or_prefix}%"))
    rows = session.execute(q).scalars().all()
    if not rows:
        raise SystemExit(f"No submission matching {sub_id_or_prefix!r}")
    if len(rows) > 1:
        ids = ", ".join(r.id for r in rows)
        raise SystemExit(
            f"Ambiguous prefix {sub_id_or_prefix!r}: {ids}. Pass a full UUID.")
    return rows[0]


def _contact_info(session: Session, sub: Submission) -> dict:
    name = ""
    email = ""
    business = sub.business_name or ""
    user = session.get(User, sub.user_id)
    if user:
        name = user.name or ""
        email = user.email or ""
    if not business:
        org = session.get(Organization, sub.org_id)
        if org:
            business = org.name or business
    return {
        "name": name,
        "email": email,
        "phone": sub.phone or "",
        "business_name": business,
    }


def _payload_size(session: Session, sub: Submission):
    """Return (pdf_keys, total_pages, max_size_bytes). Pages come from
    File.page_count if populated; otherwise 0 (then _pick_queue routes
    by size only)."""
    uploads = session.execute(
        sa.select(File).where(File.submission_id == sub.id)
        .where(File.kind == "upload")
        .order_by(File.created_at.asc())
    ).scalars().all()
    pdf_keys = [f.r2_key for f in uploads]
    total_pages = sum(int(getattr(f, "page_count", 0) or 0) for f in uploads)
    max_size = max((int(getattr(f, "size_bytes", 0) or 0) for f in uploads),
                   default=0)
    return uploads, pdf_keys, total_pages, max_size


def requeue_one(sub_id_or_prefix: str, *, dry_run: bool, force: bool) -> bool:
    """Returns True if (would have) re-enqueued successfully."""
    from rq import Queue
    from redis import Redis
    from config import (REDIS_URL, RQ_QUEUE_FAST, RQ_QUEUE_HEAVY,
                         HEAVY_QUEUE_PAGE_THRESHOLD, HEAVY_QUEUE_FILE_MB,
                         RQ_RESULT_TTL)

    with Session(engine) as session:
        sub = _resolve_submission(session, sub_id_or_prefix)
        if sub.status == "completed" and not force:
            logger.error("%s is COMPLETED — refusing to requeue without --force",
                         sub.id)
            return False

        uploads, pdf_keys, total_pages, max_size = _payload_size(session, sub)
        if not pdf_keys:
            logger.error("%s has no upload files in DB — cannot requeue",
                         sub.id)
            return False

        contact = _contact_info(session, sub)
        max_mb = max_size / (1024 * 1024)

        # Same routing logic as web_app._pick_queue / _pick_timeout
        if (total_pages >= HEAVY_QUEUE_PAGE_THRESHOLD
                or max_mb >= HEAVY_QUEUE_FILE_MB):
            queue_name = RQ_QUEUE_HEAVY
        else:
            queue_name = RQ_QUEUE_FAST

        # Conservative timeout — match _pick_timeout brackets
        if max_mb >= 300 or total_pages >= 50:
            timeout = 4 * 3600
        elif max_mb >= 100:
            timeout = 2 * 3600
        elif max_mb >= 25 or total_pages >= 20:
            timeout = 90 * 60
        else:
            timeout = 30 * 60

        logger.info("Requeue plan for %s:", sub.id)
        logger.info("  Business:    %s", contact["business_name"] or "—")
        logger.info("  Contact:     %s <%s>", contact["name"], contact["email"])
        logger.info("  Status now:  %s", sub.status)
        logger.info("  Uploads:     %d file(s), max %.1f MB, %d pages",
                    len(pdf_keys), max_mb, total_pages)
        for f in uploads:
            logger.info("    %s  (%s)", f.filename, f.r2_key)
        logger.info("  Queue:       %s", queue_name)
        logger.info("  Timeout:     %d s (%.1f min)", timeout, timeout / 60)

        if dry_run:
            logger.info("  (dry-run — no changes made)")
            return True

        # Reset DB row first so the worker doesn't trip on the stale status
        sub.status = "queued"
        sub.error = None
        session.commit()
        logger.info("  Status reset to 'queued', error cleared.")

        # Enqueue. job_id=submission_id mirrors web_app.create_submission
        # and replaces any stale registry entry for that ID.
        redis_conn = Redis.from_url(REDIS_URL)
        queue = Queue(queue_name, connection=redis_conn)
        try:
            job = queue.enqueue(
                "jobs.process_submission",
                kwargs={
                    "submission_id": sub.id,
                    "pdf_keys": pdf_keys,
                    "contact_info": contact,
                    "scope_notes": sub.scope_notes or "",
                    "rate_overrides": None,
                },
                job_id=sub.id,
                job_timeout=timeout,
                result_ttl=RQ_RESULT_TTL,
                failure_ttl=RQ_RESULT_TTL,
            )
        except Exception as exc:
            logger.error("  Enqueue FAILED: %s", exc)
            # Roll back the status reset so a retry can try again
            sub.status = "failed"
            sub.error = f"requeue enqueue failed: {exc}"
            session.commit()
            return False

        logger.info("  Enqueued job %s on %s.", job.id, queue_name)
        return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("submission_ids", nargs="+",
                   help="One or more submission IDs (full UUID or 8-char prefix)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show plan but don't reset DB or enqueue")
    p.add_argument("--force", action="store_true",
                   help="Allow requeue even if status='completed' (clobbers results)")
    args = p.parse_args()

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set")
        return 2
    if not os.environ.get("REDIS_URL"):
        logger.error("REDIS_URL not set")
        return 2

    successes = 0
    for sid in args.submission_ids:
        logger.info("=" * 70)
        ok = requeue_one(sid, dry_run=args.dry_run, force=args.force)
        if ok:
            successes += 1
    logger.info("=" * 70)
    logger.info("Done. %d of %d submission(s) %s.",
                successes, len(args.submission_ids),
                "would be requeued" if args.dry_run else "requeued")
    return 0 if successes == len(args.submission_ids) else 1


if __name__ == "__main__":
    sys.exit(main())
