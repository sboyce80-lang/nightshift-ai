#!/usr/bin/env python3
"""
Knight Shift — Job Queue Workers
================================
Functions executed by RQ workers. This module must NOT import Flask, so an
`rq worker` process can load it without dragging in the web framework.

Source of truth for status is the `submissions` table in Postgres.
R2 holds the actual files (uploads + results).

Public entry point:
    process_submission(submission_id, pdf_keys, contact_info, scope_notes)
"""

import os
import sys
import logging
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# Ensure local imports work whether invoked as `rq worker` or directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
)
import storage
from db import session_scope
from models import Submission, File
from Takeoff_DIRECT import run_analysis

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("nightshift.jobs")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(os.path.join(LOG_DIR, "worker.log"))
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def update_status(submission_id, status, error=None, subtotal=None):
    """Patch the submission row's status (and optionally error/subtotal).

    Uses a raw UPDATE (not the ORM session.get + dirty-tracking path) so
    behavior is predictable even on the work-horse's first-ever DB call,
    and logs the affected rowcount so a silent no-op is visible in logs.
    """
    values = {
        "status": status,
        "updated_at": datetime.now(timezone.utc),
    }
    if error is not None:
        values["error"] = error[:2000]
    if subtotal is not None:
        values["subtotal"] = subtotal

    try:
        with session_scope() as session:
            stmt = (
                Submission.__table__.update()
                .where(Submission.id == submission_id)
                .values(**values)
            )
            result = session.execute(stmt)
            if result.rowcount == 0:
                logger.warning("update_status: no row affected for %s (status=%s)",
                               submission_id, status)
            else:
                logger.info("update_status: %s -> %s (rows=%d)",
                            submission_id, status, result.rowcount)
    except Exception as exc:
        logger.warning("Could not update status for %s -> %s: %s",
                       submission_id, status, exc, exc_info=True)


def _record_result_file(submission_id, filename, r2_key, size_bytes, content_type):
    """Idempotently record a result file in the `files` table."""
    try:
        with session_scope() as session:
            existing = session.query(File).filter(
                File.submission_id == submission_id,
                File.kind == "result",
                File.filename == filename,
            ).one_or_none()
            if existing:
                existing.r2_key = r2_key
                existing.size_bytes = size_bytes
                existing.content_type = content_type
            else:
                session.add(File(
                    submission_id=submission_id,
                    kind="result",
                    filename=filename,
                    r2_key=r2_key,
                    size_bytes=size_bytes,
                    content_type=content_type,
                ))
    except Exception as exc:
        logger.warning("Could not record result file %s for %s: %s",
                       filename, submission_id, exc)


# ---------------------------------------------------------------------------
# Main worker entry point — RQ calls this
# ---------------------------------------------------------------------------

def process_submission(submission_id, pdf_keys, contact_info, scope_notes,
                        rate_overrides=None):
    """Run the full takeoff pipeline for a submission.

    Args:
        submission_id: UUID, also used as the RQ job id and the R2 prefix.
        pdf_keys: list of R2 object keys (e.g. submissions/<id>/uploads/X.pdf).
        contact_info: dict with name, email, phone, business_name.
        scope_notes: free-form scope text.
        rate_overrides: optional dict of pricing overrides applied to
                        PRICING_MODEL via Takeoff_DIRECT._apply_rate_overrides.

    Workflow:
        1. Mark submission `processing` in the DB.
        2. Pull every input from R2 to a tempdir.
        3. Hand local paths to run_analysis().
        4. Upload output JSON + PDF back to R2 under .../results/.
        5. Record `files` rows for the results.
        6. Email the contact.
        7. Mark `completed` (with subtotal) in the DB.

    Failures: mark `failed` + email; re-raise so RQ records job as failed.
    """
    logger.info("Processing submission %s (%d PDFs)", submission_id, len(pdf_keys))
    update_status(submission_id, "processing")

    with tempfile.TemporaryDirectory(prefix=f"ns-job-{submission_id}-") as workdir:
        local_pdfs = []
        try:
            for key in pdf_keys:
                filename = key.rsplit("/", 1)[-1]
                local_path = os.path.join(workdir, filename)
                storage.download_file(key, local_path)
                local_pdfs.append(local_path)

            result = run_analysis(
                local_pdfs,
                contact_name=contact_info["name"],
                contact_email=contact_info["email"],
                scope_notes=scope_notes,
                rate_overrides=rate_overrides,
            )

            for key_name, content_type in (
                ("output_json_path", "application/json"),
                ("output_pdf_path", "application/pdf"),
            ):
                src = result.get(key_name)
                if src and os.path.exists(src):
                    filename = os.path.basename(src)
                    r2_key = storage.result_key(submission_id, filename)
                    size_bytes = os.path.getsize(src)
                    storage.upload_file(src, r2_key, content_type=content_type)
                    _record_result_file(submission_id, filename, r2_key,
                                        size_bytes, content_type)

            send_result_email(contact_info, result)

            subtotal = result.get("cost_estimate", {}).get("subtotal", 0) or 0
            update_status(submission_id, "completed", subtotal=subtotal)
            logger.info("Submission %s completed — $%s estimate",
                        submission_id, f"{subtotal:,.2f}")

            return {"submission_id": submission_id, "subtotal": subtotal}

        except Exception as exc:
            # If the user cancelled this job mid-flight, the DB row is already
            # "cancelled" — don't overwrite with "failed" or send an error email.
            with session_scope() as session:
                sub = session.get(Submission, submission_id)
                if sub is not None and sub.status == "cancelled":
                    logger.info("Submission %s cancelled mid-run; suppressing failure path",
                                submission_id)
                    raise

            logger.error("Submission %s failed: %s", submission_id, exc, exc_info=True)
            update_status(submission_id, "failed", error=str(exc))
            try:
                send_error_email(contact_info, str(exc))
            except Exception as email_exc:
                logger.error("Failed to send error email: %s", email_exc)
            raise


# ---------------------------------------------------------------------------
# Email Notifications
# ---------------------------------------------------------------------------

def send_result_email(contact_info, result):
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

Thank you for submitting your construction documents through Knight Shift. Your painting estimate is ready.

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
    msg["Subject"] = "Knight Shift - Your Painting Estimate is Ready"
    msg.attach(MIMEText(body, "plain"))

    pdf_path = result.get("output_pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(pdf_path),
            )
            msg.attach(att)

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


def send_error_email(contact_info, error_msg):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.warning("SMTP not configured — skipping error email")
        return

    body = f"""Hi {contact_info['name']},

Thank you for submitting your construction documents through Knight Shift.

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
    msg["Subject"] = "Knight Shift - Issue Processing Your Documents"
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
