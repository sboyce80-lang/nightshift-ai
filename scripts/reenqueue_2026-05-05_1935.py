#!/usr/bin/env python3
"""One-shot: re-enqueue Elliott's Dollar Tree job. On failure, persist the
traceback into submissions.error of a debug-only marker row so we can read
it back via psql."""
import os, sys, traceback
# Worker container runs with its app dir on sys.path already; avoid __file__
# since this may be exec()'d via `python3 -c`.

DEBUG_MARKER = "__DEBUG_REENQUEUE_2026-05-05_1935__"

def write_debug(msg):
    """Persist a debug message to a known row (id starting with marker prefix).
    Uses a raw psycopg2 connection so it works even if SQLAlchemy fails."""
    try:
        import psycopg2
        url = os.environ["DATABASE_URL"]
        if url.startswith("postgresql+psycopg2://"):
            url = url.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(url)
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submissions (id, user_id, org_id, status, error, business_name) "
                "VALUES (%s, %s, %s, 'failed', %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET error = EXCLUDED.error, updated_at = NOW()",
                (DEBUG_MARKER[:36], 1, 1, msg[:1990], "DEBUG_MARKER"),
            )
        conn.close()
    except Exception:
        # last-ditch — write to stdout
        print("DEBUG_WRITE_FAILED:", msg[:1000])

try:
    print("STEP 1: imports")
    from redis import Redis
    from rq import Queue
    from rq.job import Job
    from sqlalchemy import create_engine, text
    from config import RQ_JOB_TIMEOUT, RQ_RESULT_TTL
    print("STEP 2: imports OK")

    sid = "e67c47a4-d092-452c-869e-df275bb00dc9"
    queue_name = "nightshift-fast"

    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url)
    redis = Redis.from_url(os.environ["REDIS_URL"])
    print("STEP 3: connections OK")

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT s.id, s.status, s.scope_notes, s.business_name, s.phone, "
            "u.name AS user_name, u.email AS user_email "
            "FROM submissions s JOIN users u ON u.id = s.user_id WHERE s.id = :sid"
        ), {"sid": sid}).mappings().first()
        print("STEP 4: row =", dict(row) if row else None)

        files = conn.execute(text(
            "SELECT r2_key FROM files WHERE submission_id = :sid AND kind = 'upload' ORDER BY id"
        ), {"sid": sid}).scalars().all()
        print("STEP 5: files =", list(files))

        conn.execute(text(
            "UPDATE submissions SET status = 'queued', error = NULL, updated_at = NOW() WHERE id = :sid"
        ), {"sid": sid})
        print("STEP 6: status flipped to queued")

        try:
            prior = Job.fetch(sid, connection=redis).get_status()
            print("STEP 7: prior RQ status =", prior)
        except Exception as e:
            print("STEP 7: prior fetch raised", type(e).__name__, e)
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
        print("STEP 8: redis cleaned")

        q = Queue(queue_name, connection=redis)
        q.enqueue(
            "jobs.process_submission",
            kwargs={
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
            },
            job_id=sid,
            job_timeout=RQ_JOB_TIMEOUT,
            result_ttl=RQ_RESULT_TTL,
            failure_ttl=RQ_RESULT_TTL,
        )
        print("STEP 9: enqueued")

    write_debug(f"OK enqueued {sid} on {queue_name}")
    print("DONE")

except Exception:
    tb = traceback.format_exc()
    print("EXCEPTION:\n", tb)
    write_debug("EXCEPTION:\n" + tb)
    sys.exit(1)
