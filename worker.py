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
    worker = Worker([queue], connection=conn, name=f"nightshift-{os.getpid()}")
    logger.info("Worker started (queue=%s, redis=%s, pid=%d)",
                RQ_QUEUE_NAME, REDIS_URL, os.getpid())
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
