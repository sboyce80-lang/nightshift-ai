#!/usr/bin/env python3
"""
Knight Shift — Web Form for RFP Submission
=============================================
Flask application that provides a branded web form for submitting
construction document PDFs for automated painting estimates.

Flow:
    1. Browser POSTs form + PDFs to /submit.
    2. Flask validates input, stages files in a temp dir.
    3. Each PDF is streamed to Cloudflare R2 under
       submissions/<id>/uploads/<filename>.
    4. A row is created in the `submissions` table (and `files`).
    5. An RQ job is enqueued; Redis hands it to a worker.
    6. Worker downloads inputs from R2, runs the takeoff, uploads
       results back to R2, updates the DB, and emails the contact.

Source of truth: Postgres. R2 holds files only.

Usage:
    Development:  python web_app.py        (terminal 1)
                  python worker.py         (terminal 2)
                  redis-server             (terminal 3 if not already running)
    Production:   gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 120 wsgi:app
                  rq worker nightshift     # one or more worker processes
"""

import os
import sys
import uuid
import logging
import tempfile

from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError

# ---------------------------------------------------------------------------
# Ensure local imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    MAX_PDF_SIZE_MB, MAX_PDFS_PER_EMAIL,
    WEB_PORT, FLASK_SECRET_KEY,
    REDIS_URL, RQ_QUEUE_NAME, RQ_JOB_TIMEOUT, RQ_RESULT_TTL,
    CLERK_PUBLISHABLE_KEY, CLERK_SIGN_IN_URL,
)
import storage
from db import session_scope
from models import User, Submission, File
from auth import require_auth, current_user_id, clerk_frontend_api_host

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "web_app.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("nightshift.web")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY or os.urandom(24).hex()

app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL * 1024 * 1024

ALLOWED_EXTENSIONS = {".pdf"}

# ---------------------------------------------------------------------------
# Job Queue
# ---------------------------------------------------------------------------
_redis = Redis.from_url(REDIS_URL)
_queue = Queue(RQ_QUEUE_NAME, connection=_redis)


# ---------------------------------------------------------------------------
# Template context — Clerk frontend keys available in every render
# ---------------------------------------------------------------------------

@app.context_processor
def _inject_clerk_context():
    try:
        host = clerk_frontend_api_host() if CLERK_PUBLISHABLE_KEY else ""
    except Exception:
        host = ""
    return {
        "clerk_publishable_key": CLERK_PUBLISHABLE_KEY,
        "clerk_sign_in_url": CLERK_SIGN_IN_URL,
        "clerk_frontend_api_host": host,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Public landing — Clerk.js handles auth client-side and bootstraps the
    session cookie via the dev-mode handshake. The form is hidden by JS
    until Clerk confirms a signed-in user. Server-side auth is enforced on
    /submit and /jobs/<id>, which only fire after the cookie is in place.
    """
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
@require_auth
def submit():
    """Authenticated user uploads PDFs; we persist + enqueue."""

    # 1. Identity from session — no form fields for name/email any more.
    user_id = current_user_id()
    with session_scope() as session:
        user = session.get(User, user_id)
        name = user.name or ""
        email_addr = user.email

    # 2. Get uploaded files
    files = request.files.getlist("attachments")
    valid_files = [f for f in files if f.filename and f.filename.strip()]

    if not valid_files:
        flash("Please upload at least one PDF file.", "error")
        return redirect(url_for("index"))

    if len(valid_files) > MAX_PDFS_PER_EMAIL:
        flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed.", "error")
        return redirect(url_for("index"))

    submission_id = str(uuid.uuid4())

    # 3. Stage files locally, validate, push to R2.
    uploaded_files = []   # list of (filename, r2_key, size_bytes)

    with tempfile.TemporaryDirectory(prefix=f"ns-{submission_id}-") as staging:
        try:
            for idx, f in enumerate(valid_files):
                filename = secure_filename(f.filename) or f"upload_{idx + 1}.pdf"
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    flash(f"Only PDF files are accepted. Rejected: {f.filename}", "error")
                    return redirect(url_for("index"))

                local_path = os.path.join(staging, filename)
                f.save(local_path)

                size_bytes = os.path.getsize(local_path)
                if size_bytes > MAX_PDF_SIZE_MB * 1024 * 1024:
                    flash(f"{filename} exceeds the {MAX_PDF_SIZE_MB} MB size limit.", "error")
                    return redirect(url_for("index"))

                key = storage.upload_key(submission_id, filename)
                storage.upload_file(local_path, key, content_type="application/pdf")
                uploaded_files.append((filename, key, size_bytes))

        except storage.StorageNotConfigured as exc:
            logger.error("R2 not configured: %s", exc)
            flash("Storage is not configured. Please contact support.", "error")
            return redirect(url_for("index"))
        except Exception as exc:
            logger.error("Upload error for submission %s: %s", submission_id, exc, exc_info=True)
            flash("An error occurred while uploading your files. Please try again.", "error")
            try:
                storage.delete_prefix(storage.submission_prefix(submission_id))
            except Exception:
                pass
            return redirect(url_for("index"))

    # 4. Persist submission + files to the DB.
    phone = request.form.get("phone", "").strip()
    business_name = request.form.get("business_name", "").strip()
    scope_notes = request.form.get("scope_notes", "").strip()
    deadline = request.form.get("deadline", "").strip()

    try:
        with session_scope() as session:
            sub = Submission(
                id=submission_id,
                user_id=user_id,
                phone=phone or None,
                business_name=business_name or None,
                scope_notes=scope_notes or None,
                deadline=deadline or None,
                status="queued",
            )
            session.add(sub)
            for filename, r2_key, size_bytes in uploaded_files:
                session.add(File(
                    submission_id=submission_id,
                    kind="upload",
                    filename=filename,
                    r2_key=r2_key,
                    size_bytes=size_bytes,
                    content_type="application/pdf",
                ))
    except Exception as exc:
        logger.error("DB write failed for %s: %s", submission_id, exc, exc_info=True)
        flash("An error occurred while saving your submission. Please try again.", "error")
        try:
            storage.delete_prefix(storage.submission_prefix(submission_id))
        except Exception:
            pass
        return redirect(url_for("index"))

    # 5. Enqueue the job.
    pdf_keys = [k for (_, k, _) in uploaded_files]
    try:
        job = _queue.enqueue(
            "jobs.process_submission",
            kwargs={
                "submission_id": submission_id,
                "pdf_keys": pdf_keys,
                "contact_info": {
                    "name": name,
                    "email": email_addr,
                    "phone": phone,
                    "business_name": business_name,
                },
                "scope_notes": scope_notes,
            },
            job_id=submission_id,
            job_timeout=RQ_JOB_TIMEOUT,
            result_ttl=RQ_RESULT_TTL,
            failure_ttl=RQ_RESULT_TTL,
        )
    except Exception as exc:
        logger.error("Failed to enqueue submission %s: %s", submission_id, exc)
        flash("Our queue is unavailable right now. Please try again in a few minutes.", "error")
        # Mark the DB row failed; leave files in R2 for forensics.
        try:
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub:
                    sub.status = "failed"
                    sub.error = f"enqueue failed: {exc}"
        except Exception:
            pass
        return redirect(url_for("index"))

    logger.info("Submission %s enqueued — %d PDFs from %s <%s> (job %s)",
                submission_id, len(pdf_keys), name, email_addr, job.id)

    return render_template(
        "thank_you.html",
        name=name,
        email=email_addr,
        num_files=len(pdf_keys),
    )


@app.route("/jobs/<submission_id>", methods=["GET"])
@require_auth
def job_status(submission_id):
    """Return submission status + signed download URLs for any results.

    Authorization: only the submission's owner may view it. Other users get
    a 404 (not 403) so we don't leak existence of foreign IDs.
    """
    with session_scope() as session:
        sub = session.get(Submission, submission_id)
        if sub is None or sub.user_id != current_user_id():
            return jsonify({"error": "not found"}), 404

        result_files = [f for f in sub.files if f.kind == "result"]
        results = [{
            "filename": f.filename,
            "size": f.size_bytes,
            "url": storage.presigned_download_url(f.r2_key),
        } for f in result_files]

        payload = {
            "submission_id": sub.id,
            "status": sub.status,
            "error": sub.error,
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
            "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
            "subtotal": float(sub.subtotal) if sub.subtotal is not None else None,
            "results": results,
        }

    # RQ status is best-effort; the DB row is authoritative.
    rq_status = None
    rq_error = None
    try:
        job = Job.fetch(submission_id, connection=_redis)
        rq_status = job.get_status(refresh=True)
        if job.is_failed and job.exc_info:
            rq_error = job.exc_info.splitlines()[-1]
    except NoSuchJobError:
        rq_status = "expired"

    payload["rq_status"] = rq_status
    if not payload["error"] and rq_error:
        payload["error"] = rq_error
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def too_large(e):
    flash(f"Upload too large. Maximum total size is "
          f"{MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL} MB.", "error")
    return redirect(url_for("index"))


@app.errorhandler(500)
def server_error(e):
    logger.error("500 error: %s", e)
    flash("Something went wrong. Please try again.", "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting Knight Shift web form on port %d (queue=%s, redis=%s)",
                WEB_PORT, RQ_QUEUE_NAME, REDIS_URL)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=True)
