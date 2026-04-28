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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from redis import Redis
from rq import Queue, Worker

from config import REDIS_URL, RQ_QUEUE_NAME

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

    # Unique name per startup so a stale registration from a prior container
    # can never block a new one.
    worker_name = f"nightshift-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker = Worker([queue], connection=conn, name=worker_name)
    logger.info("Worker started (name=%s, queue=%s, redis=%s)",
                worker_name, RQ_QUEUE_NAME, REDIS_URL)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
