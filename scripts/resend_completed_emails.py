#!/usr/bin/env python3
"""One-shot: re-send the customer result email for already-completed submissions.

Use this when a job completed normally but the email wasn't delivered (e.g.
SMTP env vars were missing on the worker). The takeoff is NOT re-run — we just
read the existing result JSON + PDF from R2 and email them.

Reads each submission's contact + result files from the prod DB, downloads
the result JSON/PDF from R2, rebuilds the same body that jobs.send_result_email
would have produced, and sends with an optional Cc list.

Usage (run on a Render worker via `render jobs create`, or locally with
EMAIL_*, DATABASE_URL, R2_* env vars set):

    python scripts/resend_completed_emails.py <submission_id> [<submission_id> ...]
    python scripts/resend_completed_emails.py --cc "ops@example.com" <id> [<id> ...]

If no IDs are passed, defaults to Elliott's three 2026-05-04 submissions.
"""
import argparse
import json
import os
import smtplib
import sys
import tempfile
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from config import (  # noqa: E402
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
)
import storage  # noqa: E402


DEFAULT_SUBMISSION_IDS = [
    # Elliott's 2026-05-04 jobs that completed but never emailed.
    "81fc79b7-cd50-42f8-8f21-2abec8c4b8d7",  # Washington Irving (asbestos, $0)
    "20a46123-81d8-4781-a4bc-7aa013ff7404",  # $8,168.25
    "cf0ba78e-ed21-47a8-9ab4-5ef2c974e125",  # DB_LGA4, $247,274.84
]
DEFAULT_CC = ["sboyce80@gmail.com"]


def _build_body(contact_name, result_json):
    """Mirror of jobs.send_result_email body — keep these in sync."""
    costs = result_json.get("cost_estimate", {})
    analysis = result_json.get("analysis", {})
    totals = analysis.get("aggregated_totals", {})
    project = analysis.get("project_info", {})

    items_text = ""
    for item in costs.get("line_items", []):
        if item.get("qty", 0) > 0:
            items_text += f"  - {item['item']}: ${item['total']:,.2f}\n"

    return f"""Hi {contact_name},

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


def _resend_one(engine, submission_id, cc_emails):
    """Returns True if email was sent, False otherwise (logs reason)."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT s.id, s.status, u.name AS contact_name, u.email AS contact_email
            FROM submissions s JOIN users u ON u.id = s.user_id
            WHERE s.id = :sid
        """), {"sid": submission_id}).mappings().first()
        if not row:
            print(f"  ! {submission_id}: not found in DB")
            return False
        if row["status"] != "completed":
            print(f"  ! {submission_id}: status={row['status']!r} (expected 'completed') — skipping")
            return False

        # Newest first: a re-run stacks a second result JSON/PDF on the
        # same submission, and the customer must get the latest one.
        files = conn.execute(text("""
            SELECT filename, r2_key, content_type
            FROM files
            WHERE submission_id = :sid AND kind = 'result'
            ORDER BY created_at DESC
        """), {"sid": submission_id}).mappings().all()

    json_file = next((f for f in files if f["filename"].endswith(".json")), None)
    pdf_file = next((f for f in files if f["filename"].endswith(".pdf")), None)
    if not json_file:
        print(f"  ! {submission_id}: no result JSON in files table — skipping")
        return False

    contact_name = row["contact_name"] or ""
    contact_email = row["contact_email"]
    print(f"  → {submission_id[:8]}: {contact_name} <{contact_email}>")

    with tempfile.TemporaryDirectory(prefix=f"resend-{submission_id}-") as workdir:
        json_path = os.path.join(workdir, json_file["filename"])
        storage.download_file(json_file["r2_key"], json_path)
        with open(json_path) as f:
            result_json = json.load(f)

        pdf_path = None
        if pdf_file:
            pdf_path = os.path.join(workdir, pdf_file["filename"])
            storage.download_file(pdf_file["r2_key"], pdf_path)

        body = _build_body(contact_name, result_json)

        msg = MIMEMultipart()
        msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
        msg["To"] = f"{contact_name} <{contact_email}>"
        if cc_emails:
            msg["Cc"] = ", ".join(cc_emails)
        msg["Subject"] = "Knight Shift - Your Painting Estimate is Ready"
        msg.attach(MIMEText(body, "plain"))

        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, "rb") as fp:
                att = MIMEApplication(fp.read(), _subtype="pdf")
                att.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(pdf_path))
                msg.attach(att)

        with open(json_path, "rb") as fp:
            att = MIMEApplication(fp.read(), _subtype="json")
            att.add_header("Content-Disposition", "attachment",
                           filename=os.path.basename(json_path))
            msg.attach(att)

        recipients = [contact_email] + list(cc_emails or [])
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg, to_addrs=recipients)

    cc_note = f" (cc {', '.join(cc_emails)})" if cc_emails else ""
    print(f"    ✅ sent to {contact_email}{cc_note}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("submission_ids", nargs="*",
                        help="One or more submission UUIDs. Defaults to "
                             "Elliott's 2026-05-04 jobs.")
    parser.add_argument("--cc", action="append", default=None,
                        help="Cc address. Repeat for multiple. "
                             f"Default: {','.join(DEFAULT_CC)}.")
    args = parser.parse_args()

    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("❌ EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set in env. Aborting.")
        sys.exit(1)

    sub_ids = args.submission_ids or DEFAULT_SUBMISSION_IDS
    cc_emails = args.cc if args.cc is not None else DEFAULT_CC

    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url)

    print(f"Resending result emails for {len(sub_ids)} submission(s)")
    print(f"  From: {EMAIL_ADDRESS}")
    print(f"  Cc:   {', '.join(cc_emails) if cc_emails else '(none)'}")
    print()

    sent = 0
    for sid in sub_ids:
        try:
            if _resend_one(engine, sid, cc_emails):
                sent += 1
        except Exception as exc:
            print(f"  ✗ {sid}: {type(exc).__name__}: {exc}")

    print()
    print(f"Done — {sent}/{len(sub_ids)} email(s) sent.")
    sys.exit(0 if sent == len(sub_ids) else 1)


if __name__ == "__main__":
    main()
