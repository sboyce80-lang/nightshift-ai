#!/usr/bin/env python3
"""Re-enqueue stuck submissions onto an RQ queue.

Usage:
    python scripts/reenqueue.py SUBMISSION_ID [SUBMISSION_ID ...] [--queue NAME]

For each ID:
  1. Pull the original kwargs from the RQ job hash if still present (within
     failure_ttl). Otherwise reconstruct them from the DB — note that
     `rate_overrides` is not stored in the DB, so a reconstructed enqueue
     will pass `rate_overrides=None` and emit a warning.
  2. Reset the submission row to 'queued' + clear `error`.
  3. Wipe stale RQ state (job hash, executions hash, every registry entry).
  4. Re-enqueue with the recovered kwargs and original timeouts.

The queue is auto-detected from the RQ job's `origin` when possible;
otherwise it defaults to the heavy queue (or whatever --queue specifies).

Run inside the worker container so DATABASE_URL and REDIS_URL come from the
deployed env:

    render jobs create <heavy-worker-srv-id> --confirm \\
        --start-command "python scripts/reenqueue.py <ids>"

Or locally with both env vars exported.
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from redis import Redis
from rq import Queue
from rq.job import Job
from sqlalchemy import create_engine, text

from config import RQ_JOB_TIMEOUT, RQ_RESULT_TTL, RQ_QUEUE_HEAVY


def _reconstruct_kwargs_from_db(conn, sid):
    row = conn.execute(text("""
        SELECT s.scope_notes, s.business_name, s.phone,
               u.name AS user_name, u.email AS user_email
        FROM submissions s JOIN users u ON u.id = s.user_id
        WHERE s.id = :sid
    """), {"sid": sid}).mappings().first()
    if not row:
        return None
    files = conn.execute(text("""
        SELECT r2_key FROM files
        WHERE submission_id = :sid AND kind = 'upload'
        ORDER BY id
    """), {"sid": sid}).scalars().all()
    if not files:
        return None
    return {
        "submission_id": sid,
        "pdf_keys": list(files),
        "contact_info": {
            "name": row["user_name"] or "",
            "email": row["user_email"],
            "phone": row["phone"] or "",
            "business_name": row["business_name"] or "",
        },
        "scope_notes": row["scope_notes"] or "",
        "rate_overrides": None,
    }


def _wipe_stale_redis_state(redis, sid, queue_name):
    redis.delete(f"rq:job:{sid}")
    redis.delete(f"rq:executions:{sid}")
    for reg_key in (
        f"rq:failed:{queue_name}",
        f"rq:wip:{queue_name}",
        f"rq:scheduled:{queue_name}",
        f"rq:deferred:{queue_name}",
        f"rq:finished:{queue_name}",
        f"rq:canceled:{queue_name}",
    ):
        redis.zrem(reg_key, sid)
    redis.lrem(f"rq:queue:{queue_name}", 0, sid)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("ids", nargs="+", help="Submission IDs to re-enqueue")
    p.add_argument(
        "--queue", default=None,
        help=f"Queue name (default: auto-detect from RQ job, fallback {RQ_QUEUE_HEAVY})",
    )
    args = p.parse_args()

    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)

    engine = create_engine(db_url)
    redis = Redis.from_url(os.environ["REDIS_URL"])

    with engine.begin() as conn:
        for sid in args.ids:
            kwargs = None
            queue_name = args.queue
            prior_status = None
            try:
                job = Job.fetch(sid, connection=redis)
                kwargs = dict(job.kwargs or {})
                prior_status = job.get_status()
                if queue_name is None:
                    queue_name = job.origin or RQ_QUEUE_HEAVY
                print(f"  · {sid}  found RQ job (status={prior_status}, queue={queue_name})")
            except Exception:
                pass

            if kwargs is None:
                kwargs = _reconstruct_kwargs_from_db(conn, sid)
                if kwargs is None:
                    print(f"  ! {sid}  not found (no DB row or no upload files), skipping")
                    continue
                queue_name = queue_name or RQ_QUEUE_HEAVY
                print(f"  · {sid}  RQ job gone — reconstructed from DB "
                      f"(rate_overrides=None; re-apply manually if needed)")

            queue = Queue(queue_name, connection=redis)

            conn.execute(text("""
                UPDATE submissions
                SET status = 'queued', error = NULL, updated_at = NOW()
                WHERE id = :sid
            """), {"sid": sid})

            _wipe_stale_redis_state(redis, sid, queue_name)

            queue.enqueue(
                "jobs.process_submission",
                kwargs=kwargs,
                job_id=sid,
                job_timeout=RQ_JOB_TIMEOUT,
                result_ttl=RQ_RESULT_TTL,
                failure_ttl=RQ_RESULT_TTL,
            )
            ro = kwargs.get("rate_overrides")
            n = len(kwargs.get("pdf_keys") or [])
            print(f"  + {sid}  re-enqueued on {queue_name} ({n} PDFs, rate_overrides={ro})")

    print("\nDone.")


if __name__ == "__main__":
    main()
