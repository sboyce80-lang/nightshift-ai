#!/usr/bin/env python3
"""
Knight Shift — RQ Worker Entry Point
====================================
Run a single RQ worker process that consumes from the nightshift queue.

Usage:
    python worker.py              # one worker, foreground
    rq worker nightshift          # equivalent, using rq's own CLI

For production on Render, point the Background Worker service at this script.
"""

import os
import sys
import uuid
import logging

# Force line-buffered stdout so print() statements from Takeoff_DIRECT.py
# (the takeoff engine the worker invokes) reach Render's log stream
# immediately rather than being held in a 4KB block buffer that only
# flushes on process exit. Equivalent to running the worker with
# `python -u`, but a code-side change auto-deploys with each commit
# whereas startCommand changes need a Render Blueprint sync.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python <3.7 — should not happen, render.yaml pins 3.11

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from redis import Redis
from rq import Queue, Worker

from config import REDIS_URL, RQ_QUEUE_NAME
from db import session_scope
from models import Submission

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "worker.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("nightshift.worker")


class RequeueOnShutdownWorker(Worker):
    """Worker that re-enqueues in-flight jobs on warm shutdown.

    Render redeploys send SIGTERM with a ~30s grace before SIGKILL. RQ's
    default warm shutdown waits for the work-horse to finish — but our
    DD-scale takeoffs run 20-30 min, so both parent and work-horse get
    SIGKILLed, the DB row is stranded at 'processing', and the next
    visible signal is RQ's StartedJobRegistry sweep ~13 min later
    (AbandonedJobError → FailedJobRegistry).

    To recover within seconds instead:
      1. request_stop kills the work-horse immediately so the parent's
         monitor_work_horse loop exits and handle_job_failure runs while
         the container is still alive.
      2. handle_job_failure detects the warm-shutdown flag and resets the
         submission row to 'queued' + re-enqueues the RQ job, instead of
         marking it failed and emailing the user.

    Tradeoff: a fast job that could have finished inside the 30s grace gets
    killed and re-run. For the heavy queue this is fine (no job ever
    finishes that fast). The fast queue uses the same class for symmetry,
    and small jobs that survive normal completion never hit this path.
    """

    def request_stop(self, signum, frame):
        super().request_stop(signum, frame)
        if getattr(self, "_horse_pid", 0):
            self.log.warning(
                "Killing work-horse %d so cleanup runs before container dies",
                self._horse_pid,
            )
            try:
                self.kill_horse()
            except Exception as exc:
                self.log.error("kill_horse failed: %s", exc, exc_info=True)

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):
        is_warm_shutdown = (
            getattr(self, "_stop_requested", False)
            and getattr(self, "_stopped_job_id", None) != job.id
        )
        if is_warm_shutdown:
            try:
                if _requeue_on_shutdown(self.connection, queue, job):
                    self.log.warning(
                        "Job %s killed by warm shutdown — re-queued (DB reset to 'queued')",
                        job.id,
                    )
                    return
            except Exception as exc:
                self.log.error(
                    "Re-queue on shutdown failed for %s, falling through to default failure path: %s",
                    job.id, exc, exc_info=True,
                )
        return super().handle_job_failure(job, queue, started_job_registry, exc_string)


def _requeue_on_shutdown(redis_conn, queue, job):
    """Reset DB row to 'queued' and re-enqueue the job with its original kwargs.

    Returns True on successful requeue, False if the submission is in a
    terminal state (e.g. user-cancelled) where requeue would be wrong.
    """
    sid = job.id
    kwargs = dict(job.kwargs or {})
    timeout = job.timeout
    result_ttl = job.result_ttl
    failure_ttl = job.failure_ttl

    # Skip requeue if user cancelled mid-flight — 'cancelled' is terminal.
    with session_scope() as session:
        sub = session.get(Submission, sid)
        if sub is None:
            return False
        if sub.status == "cancelled":
            logger.info("Skipping requeue: %s already cancelled by user", sid)
            return False
        sub.status = "queued"
        sub.error = None

    # Wipe every key/registry entry pinning this job id so the fresh enqueue
    # doesn't trip on stale RQ state (StartedJobRegistry, executions hash,
    # etc). Same logic as scripts/reenqueue.py.
    qname = queue.name
    redis_conn.delete(f"rq:job:{sid}")
    redis_conn.delete(f"rq:executions:{sid}")
    for reg_key in (
        f"rq:failed:{qname}",
        f"rq:wip:{qname}",
        f"rq:scheduled:{qname}",
        f"rq:deferred:{qname}",
        f"rq:finished:{qname}",
        f"rq:canceled:{qname}",
    ):
        redis_conn.zrem(reg_key, sid)
    redis_conn.lrem(f"rq:queue:{qname}", 0, sid)

    queue.enqueue(
        "jobs.process_submission",
        kwargs=kwargs,
        job_id=sid,
        job_timeout=timeout,
        result_ttl=result_ttl,
        failure_ttl=failure_ttl,
        at_front=True,
    )
    return True


def main():
    conn = Redis.from_url(REDIS_URL)
    queue = Queue(RQ_QUEUE_NAME, connection=conn)

    # Sweep stale registrations from prior containers whose death wasn't
    # recorded (Render redeploys can collide on PID-based names). RQ marks
    # workers dead if they haven't heartbeated within their TTL.
    try:
        for stale in Worker.all(connection=conn):
            if not stale.is_alive():
                stale.register_death()
                logger.info("Cleaned stale worker: %s", stale.name)
    except Exception as exc:
        logger.warning("Stale-worker sweep failed (non-fatal): %s", exc)

    # Reconcile DB rows whose RQ jobs were killed by SIGKILL (OOM, RQ
    # job_timeout, hard Render eviction) and so escaped both
    # process_submission's try/except AND RequeueOnShutdownWorker's
    # warm-shutdown handler. Without this, those rows sit at
    # 'queued'/'processing' forever.
    try:
        from jobs import reconcile_abandoned_submissions
        reconcile_abandoned_submissions(conn, [RQ_QUEUE_NAME])
    except Exception as exc:
        logger.warning("Abandoned-job reconciliation failed (non-fatal): %s",
                       exc, exc_info=True)

    # Unique name per startup so a stale registration from a prior container
    # can never block a new one.
    worker_name = f"nightshift-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker = RequeueOnShutdownWorker([queue], connection=conn, name=worker_name)
    logger.info("Worker started (name=%s, queue=%s, redis=%s)",
                worker_name, RQ_QUEUE_NAME, REDIS_URL)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
