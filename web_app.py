#!/usr/bin/env python3
"""
Nightshift AI — Web Form for RFP Submission
=============================================
Flask application that provides a branded web form for submitting
construction document PDFs for automated painting estimates.

Usage:
    Development:  python web_app.py
    Production:   gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 300 wsgi:app
"""

import os
import sys
import uuid
import json
import shutil
import logging
import smtplib
from datetime import datetime
from threading import Thread
from queue import Queue

from flask import Flask, request, render_template, redirect, url_for, flash
from werkzeug.utils import secure_filename
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# ---------------------------------------------------------------------------
# Ensure local imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
    MAX_PDF_SIZE_MB, MAX_PDFS_PER_EMAIL,
    WEB_PORT, FLASK_SECRET_KEY,
)
from Takeoff_DIRECT import run_analysis

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

# Maximum total upload size (all files combined)
app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_SIZE_MB * MAX_PDFS_PER_EMAIL * 1024 * 1024

SUBMISSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submissions")
os.makedirs(SUBMISSIONS_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf"}

# ---------------------------------------------------------------------------
# Background Worker
# ---------------------------------------------------------------------------
_submission_queue = Queue()


def _worker():
    """Background worker that processes submissions one at a time."""
    while True:
        job = _submission_queue.get()
        if job is None:
            break
        try:
            _process_submission(**job)
        except Exception as exc:
            logger.error("Worker error for %s: %s",
                         job.get("submission_id", "?"), exc, exc_info=True)
        finally:
            _submission_queue.task_done()


def _start_worker():
    """Start the background worker thread (daemon so it exits with the app)."""
    t = Thread(target=_worker, daemon=True, name="submission-worker")
    t.start()
    logger.info("Background worker started")
    return t


# ---------------------------------------------------------------------------
# Submission Processing
# ---------------------------------------------------------------------------

def _update_status(submission_dir, status, error=None):
    """Update the status field in a submission's metadata.json."""
    meta_path = os.path.join(submission_dir, "metadata.json")
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta["status"] = status
        if error:
            meta["error"] = error
        meta["updated_at"] = datetime.now().isoformat()
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception as exc:
        logger.warning("Could not update status for %s: %s", submission_dir, exc)


def _process_submission(submission_id, submission_dir, pdf_paths, contact_info,
                        scope_notes):
    """Called by the background worker to run the analysis and email results."""
    logger.info("Processing submission %s (%d PDFs)", submission_id, len(pdf_paths))
    _update_status(submission_dir, "processing")

    try:
        result = run_analysis(
            pdf_paths,
            contact_name=contact_info["name"],
            contact_email=contact_info["email"],
            scope_notes=scope_notes,
        )

        # Copy output files to the submission's results directory
        results_dir = os.path.join(submission_dir, "results")
        os.makedirs(results_dir, exist_ok=True)

        for key in ("output_json_path", "output_pdf_path"):
            src = result.get(key)
            if src and os.path.exists(src):
                dst = os.path.join(results_dir, os.path.basename(src))
                shutil.copy2(src, dst)

        # Send email with results
        _send_result_email(contact_info, result)

        _update_status(submission_dir, "completed")
        logger.info("Submission %s completed — $%,.2f estimate",
                     submission_id,
                     result.get("cost_estimate", {}).get("subtotal", 0))

    except Exception as exc:
        logger.error("Submission %s failed: %s", submission_id, exc, exc_info=True)
        _update_status(submission_dir, "failed", error=str(exc))
        _send_error_email(contact_info, str(exc))


# ---------------------------------------------------------------------------
# Email Notifications (mirrors email_processor.py patterns)
# ---------------------------------------------------------------------------

def _send_result_email(contact_info, result):
    """Send analysis results to the submitter via SMTP."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping email notification")
        return

    costs = result.get("cost_estimate", {})
    analysis = result.get("analysis", {})
    totals = analysis.get("aggregated_totals", {})
    project = analysis.get("project_info", {})

    items_text = ""
    for item in costs.get("line_items", []):
        if item.get("qty", 0) > 0:
            items_text += f"  - {item['item']}: ${item['total']:,.2f}\n"

    body = f"""Hi {contact_info['name']},

Thank you for submitting your construction documents through Nightshift AI. Your painting estimate is ready.

PROJECT SUMMARY
  Floors analyzed: {project.get('total_floors_analyzed', 'N/A')}
  Rooms found:     {project.get('total_rooms_found', 'N/A')}

MEASUREMENTS EXTRACTED
  Paintable walls:    {totals.get('total_paintable_wall_sqft', 0):,.0f} sq ft
  Paintable ceilings: {totals.get('total_paintable_ceiling_sqft', 0):,.0f} sq ft
  Base trim:          {totals.get('total_base_trim_lf', 0):,.0f} linear feet
  Doors (full paint): {totals.get('total_doors_full_paint', 0):,.0f}
  Doors (HM panel):   {totals.get('total_doors_hm_panel', 0):,.0f}
  Windows (painted):  {totals.get('total_windows_painted_interior', 0):,.0f}
  Stair sections:     {totals.get('total_stair_sections', 0):,.0f}

COST ESTIMATE
{items_text}
  TOTAL: ${costs.get('subtotal', 0):,.2f}

IMPORTANT: This is a preliminary estimate generated automatically from your
drawings. A formal proposal will follow after review.

The detailed analysis is attached as a PDF report.

Best regards,
{COMPANY_NAME}
{COMPANY_PHONE}
{COMPANY_EMAIL}
"""

    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = f"{contact_info['name']} <{contact_info['email']}>"
    msg["Subject"] = "Nightshift AI - Your Painting Estimate is Ready"
    msg.attach(MIMEText(body, "plain"))

    # Attach PDF report
    pdf_path = result.get("output_pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(pdf_path),
            )
            msg.attach(att)

    # Attach JSON
    json_path = result.get("output_json_path")
    if json_path and os.path.exists(json_path):
        with open(json_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="json")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(json_path),
            )
            msg.attach(att)

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Result email sent to %s", contact_info["email"])
    except Exception as exc:
        logger.error("Failed to send result email: %s", exc)


def _send_error_email(contact_info, error_msg):
    """Send an error notification to the submitter."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping error email")
        return

    body = f"""Hi {contact_info['name']},

Thank you for submitting your construction documents through Nightshift AI.

Unfortunately, our system encountered an issue processing your documents:
  {error_msg}

This may happen when drawings are in an unsupported format or contain
elements our system can't yet interpret.

Please reply to this email or call {COMPANY_PHONE} for assistance.

Best regards,
{COMPANY_NAME}
"""

    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = f"{contact_info['name']} <{contact_info['email']}>"
    msg["Subject"] = "Nightshift AI - Issue Processing Your Documents"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Error email sent to %s", contact_info["email"])
    except Exception as exc:
        logger.error("Failed to send error email: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the RFP submission form."""
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    """Handle form submission: validate, save files, queue for processing."""

    # 1. Validate required fields
    name = request.form.get("name", "").strip()
    email_addr = request.form.get("email", "").strip()

    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("index"))
    if not email_addr or "@" not in email_addr:
        flash("A valid email address is required.", "error")
        return redirect(url_for("index"))

    # 2. Get uploaded files
    files = request.files.getlist("attachments")
    valid_files = [f for f in files if f.filename and f.filename.strip()]

    if not valid_files:
        flash("Please upload at least one PDF file.", "error")
        return redirect(url_for("index"))

    if len(valid_files) > MAX_PDFS_PER_EMAIL:
        flash(f"Maximum {MAX_PDFS_PER_EMAIL} files allowed.", "error")
        return redirect(url_for("index"))

    # 3. Create submission directory
    submission_id = str(uuid.uuid4())
    submission_dir = os.path.join(SUBMISSIONS_DIR, submission_id)
    uploads_dir = os.path.join(submission_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    # 4. Save and validate each file
    pdf_paths = []
    try:
        for f in valid_files:
            filename = secure_filename(f.filename)
            if not filename:
                filename = f"upload_{len(pdf_paths) + 1}.pdf"
            ext = os.path.splitext(filename)[1].lower()

            if ext not in ALLOWED_EXTENSIONS:
                flash(f"Only PDF files are accepted. Rejected: {f.filename}", "error")
                shutil.rmtree(submission_dir, ignore_errors=True)
                return redirect(url_for("index"))

            filepath = os.path.join(uploads_dir, filename)
            f.save(filepath)

            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb > MAX_PDF_SIZE_MB:
                flash(f"{filename} exceeds the {MAX_PDF_SIZE_MB} MB size limit.", "error")
                shutil.rmtree(submission_dir, ignore_errors=True)
                return redirect(url_for("index"))

            pdf_paths.append(filepath)

    except Exception as exc:
        logger.error("File save error: %s", exc)
        flash("An error occurred while uploading your files. Please try again.", "error")
        shutil.rmtree(submission_dir, ignore_errors=True)
        return redirect(url_for("index"))

    # 5. Collect metadata
    phone = request.form.get("phone", "").strip()
    business_name = request.form.get("business_name", "").strip()
    scope_notes = request.form.get("scope_notes", "").strip()
    deadline = request.form.get("deadline", "").strip()

    metadata = {
        "submission_id": submission_id,
        "name": name,
        "email": email_addr,
        "phone": phone,
        "business_name": business_name,
        "scope_notes": scope_notes,
        "deadline": deadline,
        "submitted_at": datetime.now().isoformat(),
        "status": "queued",
        "pdf_files": [os.path.basename(p) for p in pdf_paths],
    }

    with open(os.path.join(submission_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    # 6. Queue the job for background processing
    _submission_queue.put({
        "submission_id": submission_id,
        "submission_dir": submission_dir,
        "pdf_paths": sorted(pdf_paths),
        "contact_info": {
            "name": name,
            "email": email_addr,
            "phone": phone,
            "business_name": business_name,
        },
        "scope_notes": scope_notes,
    })

    logger.info("Submission %s queued — %d PDFs from %s <%s>",
                submission_id, len(pdf_paths), name, email_addr)

    # 7. Show confirmation page
    return render_template(
        "thank_you.html",
        name=name,
        email=email_addr,
        num_files=len(pdf_paths),
    )


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

# Start the background worker when the module loads
_start_worker()

if __name__ == "__main__":
    logger.info("Starting Nightshift AI web form on port %d", WEB_PORT)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=True)
