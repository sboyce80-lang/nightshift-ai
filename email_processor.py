#!/usr/bin/env python3
"""
Knight Shift - Email-Based PDF Ingestion Service
==================================================
Monitors an Outlook / Office 365 mailbox for RFP emails with PDF attachments.
Processes PDFs through the Takeoff_DIRECT analysis pipeline.
Replies to the sender with the painting estimate PDF report attached.

Usage:
    python email_processor.py            # Run continuous polling loop
    python email_processor.py --once     # Process pending emails once and exit
    python email_processor.py --test     # Send a test email to verify SMTP works
"""

import imaplib
import smtplib
import email as email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import decode_header

import os
import sys
import json
import time
import signal
import logging
import argparse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure imports work from this directory
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    EMAIL_WATCH_FOLDER, EMAIL_POLL_INTERVAL,
    EMAIL_SUBJECT_FILTER,
    MAX_PDF_SIZE_MB, MAX_PDFS_PER_EMAIL,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
)
from Takeoff_DIRECT import run_analysis


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "email_processor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("nightshift.email")


# ---------------------------------------------------------------------------
# Processed-email tracker (simple JSON file — matches project pattern)
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
PROCESSED_DB = DATA_DIR / "processed_emails.json"


def _load_processed_ids():
    """Return set of already-processed Message-IDs."""
    if PROCESSED_DB.exists():
        with open(PROCESSED_DB, "r") as f:
            return set(json.load(f).get("processed_ids", []))
    return set()


def _save_processed_id(message_id):
    """Append a Message-ID to the tracker."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ids = _load_processed_ids()
    ids.add(message_id)
    with open(PROCESSED_DB, "w") as f:
        json.dump(
            {"processed_ids": list(ids), "last_updated": datetime.now().isoformat()},
            f,
            indent=2,
        )


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------
def connect_imap():
    """Connect and authenticate to the IMAP server."""
    logger.info("Connecting to %s:%s ...", EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT)
    conn = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT)
    conn.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
    logger.info("Authenticated as %s", EMAIL_ADDRESS)
    return conn


def fetch_unread_emails(conn):
    """
    Return list of (uid_bytes, email.message.Message) for unread emails
    that contain at least one PDF attachment.
    """
    conn.select(EMAIL_WATCH_FOLDER)
    criteria = "(UNSEEN)"
    if EMAIL_SUBJECT_FILTER:
        criteria = f'(UNSEEN SUBJECT "{EMAIL_SUBJECT_FILTER}")'
    status, data = conn.uid("search", None, criteria)
    if status != "OK" or not data[0]:
        return []
    uids = data[0].split()
    logger.info("Found %d unread email(s) matching criteria", len(uids))
    results = []
    for uid in uids:
        status, msg_data = conn.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            continue
        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)
        if _has_pdf_attachments(msg):
            results.append((uid, msg))
        else:
            logger.debug("Skipping (no PDFs): %s", _get_subject(msg))
    return results


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------
def _has_pdf_attachments(msg):
    for part in msg.walk():
        ct = part.get_content_type()
        fn = part.get_filename()
        if ct == "application/pdf" or (fn and fn.lower().endswith(".pdf")):
            return True
    return False


def _get_subject(msg):
    subject, enc = decode_header(msg["Subject"] or "")[0]
    if isinstance(subject, bytes):
        return subject.decode(enc or "utf-8")
    return subject or "(no subject)"


def _get_sender(msg):
    """Return (display_name, email_address)."""
    raw = msg.get("From", "")
    if "<" in raw and ">" in raw:
        name = raw.split("<")[0].strip().strip('"')
        addr = raw.split("<")[1].split(">")[0].strip()
    else:
        name = ""
        addr = raw.strip()
    return name or addr.split("@")[0], addr


def _get_email_body_text(msg):
    """Extract plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    return ""


def _extract_scope_notes(msg):
    """
    Extract scope notes from the email body text.
    Looks for patterns like:
        Scope: Residential floors 2-4 only, skip basement
        Scope Notes: Interior only, no exterior
        Painting Scope: Common areas and corridors only
    Returns the scope notes string, or "" if not found.
    """
    import re
    body = _get_email_body_text(msg)
    if not body:
        return ""

    patterns = [
        r'(?i)scope\s*notes?\s*:\s*(.+?)(?:\n\s*\n|\Z)',
        r'(?i)painting\s+scope\s*:\s*(.+?)(?:\n\s*\n|\Z)',
        r'(?i)scope\s*:\s*(.+?)(?:\n\s*\n|\Z)',
    ]

    for pattern in patterns:
        match = re.search(pattern, body, re.DOTALL)
        if match:
            notes = match.group(1).strip()
            # Collapse multi-line into single line, trim length
            notes = re.sub(r'\s+', ' ', notes)
            return notes[:500]

    return ""


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------
def extract_pdf_attachments(msg, save_dir):
    """Save PDF attachments to *save_dir* and return list of file paths."""
    os.makedirs(save_dir, exist_ok=True)
    paths = []
    count = 0
    for part in msg.walk():
        ct = part.get_content_type()
        fn = part.get_filename()
        if not (ct == "application/pdf" or (fn and fn.lower().endswith(".pdf"))):
            continue
        if count >= MAX_PDFS_PER_EMAIL:
            logger.warning("Skipping remaining PDFs (limit %d reached)", MAX_PDFS_PER_EMAIL)
            break

        # Decode filename
        if fn:
            decoded, enc = decode_header(fn)[0]
            if isinstance(decoded, bytes):
                fn = decoded.decode(enc or "utf-8")
        else:
            fn = f"attachment_{count}.pdf"
        safe_fn = "".join(c for c in fn if c.isalnum() or c in "._- ").strip()
        filepath = os.path.join(save_dir, safe_fn)

        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        size_mb = len(payload) / (1024 * 1024)
        if size_mb > MAX_PDF_SIZE_MB:
            logger.warning("Skipping %s (%.1f MB > %d MB limit)", safe_fn, size_mb, MAX_PDF_SIZE_MB)
            continue

        with open(filepath, "wb") as f:
            f.write(payload)
        paths.append(filepath)
        count += 1
        logger.info("Extracted: %s (%.1f MB)", safe_fn, size_mb)
    return paths


# ---------------------------------------------------------------------------
# Process one email
# ---------------------------------------------------------------------------
def process_email(msg, uid, conn):
    """
    Extract PDFs ➜ run analysis ➜ return results dict (or error dict).
    Marks the email as read regardless of outcome.
    """
    message_id = msg.get("Message-ID", uid.decode() if isinstance(uid, bytes) else str(uid))
    subject = _get_subject(msg)
    sender_name, sender_email = _get_sender(msg)
    scope_notes = _extract_scope_notes(msg)

    logger.info("Processing: '%s' from %s <%s>", subject, sender_name, sender_email)
    if scope_notes:
        logger.info("Scope notes found: %s", scope_notes[:100])

    # Already handled?
    if message_id in _load_processed_ids():
        logger.info("Already processed %s — skipping", message_id)
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
    attachment_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "attachments", f"{timestamp}_{uid_str}"
    )

    try:
        # 1) Extract PDFs
        pdf_paths = extract_pdf_attachments(msg, attachment_dir)
        if not pdf_paths:
            logger.warning("No valid PDFs in '%s'", subject)
            return None
        logger.info("Extracted %d PDF(s) — starting analysis ...", len(pdf_paths))

        # 2) Run analysis
        result = run_analysis(pdf_paths, sender_name, sender_email,
                            scope_notes=scope_notes)

        # 3) Mark processed
        conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        _save_processed_id(message_id)

        cost_total = result.get("cost_estimate", {}).get("subtotal", 0)
        logger.info("Analysis complete: $%,.2f", cost_total)

        result["source_email_subject"] = subject
        return result

    except Exception as exc:
        logger.error("Failed to process '%s': %s", subject, exc, exc_info=True)
        # Still mark as read to avoid infinite retries
        conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        _save_processed_id(message_id)
        return {
            "error": str(exc),
            "source_email_subject": subject,
            "sender_name": sender_name,
            "sender_email": sender_email,
        }


# ---------------------------------------------------------------------------
# Reply composition
# ---------------------------------------------------------------------------
def compose_reply(original_msg, result):
    """Build a MIMEMultipart reply with the PDF report attached."""
    sender_name, sender_email = _get_sender(original_msg)
    subject = _get_subject(original_msg)

    reply = MIMEMultipart()
    reply["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    reply["To"] = f"{sender_name} <{sender_email}>"
    reply["Subject"] = f"Re: {subject} - Painting Estimate Ready"
    reply["In-Reply-To"] = original_msg.get("Message-ID", "")
    reply["References"] = original_msg.get("Message-ID", "")

    if "error" in result:
        body = _error_body(sender_name, result["error"])
    else:
        body = _success_body(sender_name, result)

    # Attach the PDF report
    pdf_path = result.get("output_pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment", filename=os.path.basename(pdf_path)
            )
            reply.attach(att)

    # Also attach the JSON
    json_path = result.get("output_json_path")
    if json_path and os.path.exists(json_path):
        with open(json_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="json")
            att.add_header(
                "Content-Disposition", "attachment", filename=os.path.basename(json_path)
            )
            reply.attach(att)

    reply.attach(MIMEText(body, "plain"))
    return reply


def _success_body(name, result):
    costs = result.get("cost_estimate", {})
    analysis = result.get("analysis", {})
    totals = analysis.get("aggregated_totals", {})
    project = analysis.get("project_info", {})
    will_synth = result.get("will_synthesis", {}) or {}

    items_text = ""
    for item in costs.get("line_items", []):
        if item.get("qty", 0) > 0:
            items_text += f"  - {item['item']}: ${item['total']:,.2f}\n"

    scope_text = ""
    scope_summary = analysis.get("scope_summary", {})
    scope_notes_val = result.get("scope_notes") or ""
    if scope_notes_val:
        scope_text = f"""
SCOPE APPLIED
  Scope notes:     {scope_notes_val}
  Rooms in scope:  {scope_summary.get('rooms_in_scope', 'all')}
  Rooms excluded:  {scope_summary.get('rooms_excluded', 0)}
"""

    # ── Will's executive recap goes at the top of the reply ──
    # All four blocks gracefully degrade to empty strings if Will synthesis
    # is unavailable (API failure, missing key, etc.), so the email format
    # falls back to the original behavior with no crash.
    will_header = ""
    will_scope = ""
    will_confidence = ""
    will_adjustments = ""
    if will_synth:
        recap = will_synth.get("estimator_recap", "").strip()
        if recap:
            will_header = f"""
ESTIMATOR'S RECAP
{recap}
"""

        gc_scope = will_synth.get("gc_scope_of_work", "").strip()
        if gc_scope:
            will_scope = f"""
SCOPE OF WORK
{gc_scope}
"""

        confidence = will_synth.get("confidence", {})
        if confidence:
            level = confidence.get("level_pct", 0)
            recommendation = confidence.get("bid_recommendation", "")
            top_risks = confidence.get("top_risks", [])
            risks_text = "\n".join(f"  - {r}" for r in top_risks[:3])
            will_confidence = f"""
ESTIMATOR'S CONFIDENCE: {level}%
Recommendation: {recommendation}
Top risks:
{risks_text}
"""

        # Surface Will's adjustments so the GC sees what changed
        adjustments_log = result.get("adjustments_applied", []) or []
        will_edits = [a for a in adjustments_log if isinstance(a, str) and a.startswith("Will adjusted")]
        if will_edits:
            edits_text = "\n".join(f"  - {e}" for e in will_edits[:5])
            will_adjustments = f"""
ESTIMATOR ADJUSTMENTS APPLIED
{edits_text}
"""

    return f"""Hi {name},

Thank you for sending over the construction documents.  Our system has completed a preliminary painting estimate based on the architectural drawings.
{will_header}{will_scope}
PROJECT SUMMARY
  Floors analyzed:  {project.get('total_floors_analyzed', 'N/A')}
  Rooms found:      {project.get('total_rooms_found', 'N/A')}
{scope_text}
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
{will_adjustments}{will_confidence}
IMPORTANT: This is a preliminary estimate generated automatically from your drawings. A formal proposal will follow after we review the documents and discuss the project scope in detail.

The detailed analysis is attached as a PDF and JSON file.

Factors that may adjust the final price:
  - Surface conditions and prep work required
  - Paint specifications and color selections
  - Access conditions on site
  - Areas not clearly shown in the drawings

I would be happy to schedule a walkthrough to finalize. Please reply to this email or call us at {COMPANY_PHONE}.

Best regards,
{COMPANY_NAME}
{COMPANY_PHONE}
{COMPANY_EMAIL}
"""


def _error_body(name, error_msg):
    return f"""Hi {name},

Thank you for sending over your construction documents.  Unfortunately, our
system was unable to complete the analysis automatically.

Reason: {error_msg}

This typically happens when:
  - The PDF contains only title sheets, details, or specifications (no floor plans)
  - The PDF is a scanned image rather than a vector drawing
  - The file was corrupted or password-protected

Please try re-sending the floor plan sheets specifically, or reply to this
email and we will review the documents manually.

Best regards,
{COMPANY_NAME}
{COMPANY_PHONE}
{COMPANY_EMAIL}
"""


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------
def send_reply(reply_msg):
    """Send a composed reply via SMTP."""
    logger.info("Sending reply to %s: %s", reply_msg["To"], reply_msg["Subject"])
    with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(reply_msg)
    logger.info("Reply sent successfully")


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received — finishing current cycle ...")
    _shutdown = True


def poll_loop():
    """Main loop: check inbox → process → reply → sleep → repeat."""
    global _shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("=" * 60)
    logger.info("NIGHTSHIFT AI — Email Processor Starting")
    logger.info("Monitoring: %s / %s", EMAIL_ADDRESS, EMAIL_WATCH_FOLDER)
    logger.info("Poll interval: %ds", EMAIL_POLL_INTERVAL)
    if EMAIL_SUBJECT_FILTER:
        logger.info("Subject filter: '%s'", EMAIL_SUBJECT_FILTER)
    logger.info("=" * 60)

    while not _shutdown:
        conn = None
        try:
            conn = connect_imap()
            emails = fetch_unread_emails(conn)
            if not emails:
                logger.debug("No new RFP emails")

            for uid, msg in emails:
                if _shutdown:
                    break
                result = process_email(msg, uid, conn)
                if result is not None:
                    reply = compose_reply(msg, result)
                    send_reply(reply)

        except imaplib.IMAP4.abort as exc:
            logger.error("IMAP connection aborted: %s", exc)
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP error: %s", exc)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

        if not _shutdown:
            for _ in range(EMAIL_POLL_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    logger.info("Email processor stopped")


# ---------------------------------------------------------------------------
# One-shot & test modes
# ---------------------------------------------------------------------------
def process_once():
    """Check for pending emails, process them, then exit."""
    logger.info("Running one-shot mode ...")
    conn = None
    try:
        conn = connect_imap()
        emails = fetch_unread_emails(conn)
        if not emails:
            logger.info("No new RFP emails found")
            return
        for uid, msg in emails:
            result = process_email(msg, uid, conn)
            if result is not None:
                reply = compose_reply(msg, result)
                send_reply(reply)
        logger.info("Processed %d email(s)", len(emails))
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


def send_test_email():
    """Send a test email to yourself to verify SMTP credentials."""
    logger.info("Sending test email to verify SMTP configuration ...")
    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = EMAIL_ADDRESS
    msg["Subject"] = "Knight Shift - SMTP Test"
    body = f"""This is a test email from the Knight Shift Email Processor.

If you are reading this, your SMTP configuration is working correctly.

Configuration:
  IMAP: {EMAIL_IMAP_SERVER}:{EMAIL_IMAP_PORT}
  SMTP: {EMAIL_SMTP_SERVER}:{EMAIL_SMTP_PORT}
  Folder: {EMAIL_WATCH_FOLDER}
  Poll interval: {EMAIL_POLL_INTERVAL}s
  Subject filter: {EMAIL_SUBJECT_FILTER or '(none)'}

Timestamp: {datetime.now().isoformat()}
"""
    msg.attach(MIMEText(body, "plain"))
    send_reply(msg)
    logger.info("Test email sent — check your inbox!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Knight Shift — Email PDF Ingestion Service"
    )
    parser.add_argument("--once", action="store_true", help="Process once and exit")
    parser.add_argument("--test", action="store_true", help="Send test email to verify SMTP")
    args = parser.parse_args()

    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        logger.error(
            "EMAIL_ADDRESS and EMAIL_APP_PASSWORD must be set.\n"
            "Create a .env file from .env.example and fill in your credentials."
        )
        sys.exit(1)

    if args.test:
        send_test_email()
    elif args.once:
        process_once()
    else:
        poll_loop()


if __name__ == "__main__":
    main()
