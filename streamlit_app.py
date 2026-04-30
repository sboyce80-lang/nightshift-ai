#!/usr/bin/env python3
"""
Knight Shift — Streamlit App (Queue-Based)
=============================================
Upload construction PDFs → get automated painting estimates.
Jobs are queued and processed one at a time to stay within memory limits.

Run:
    streamlit run streamlit_app.py

Deploy (Streamlit Cloud):
    1. Push repo to GitHub
    2. Go to share.streamlit.io
    3. Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) as a secret
"""

import os
import sys
import gc
import json
import uuid
import threading
import time
import smtplib
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import pandas as pd
import streamlit as st

# ── Ensure imports from project root ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ── Bridge Streamlit secrets → environment variables ──
# Streamlit Cloud stores secrets in st.secrets, not os.environ.
# config.py and Takeoff_DIRECT.py read from os.environ, so we bridge here.
try:
    if hasattr(st, "secrets"):
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
                     "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD",
                     "EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT",
                     "INTERNAL_NOTIFY_EMAIL"):
            if key in st.secrets and key not in os.environ:
                os.environ[key] = str(st.secrets[key])
except Exception:
    pass

# ── Page config (must be first st call) ──
st.set_page_config(
    page_title="Knight Shift — Painting Takeoff",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Email / company config (loaded after secrets bridge) ──
from config import (
    EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
    EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
    PRICING_MODEL,
)
INTERNAL_NOTIFY_EMAIL = os.environ.get("INTERNAL_NOTIFY_EMAIL", "")

# ── Directories ──
UPLOAD_DIR = os.path.join(PROJECT_ROOT, "uploads")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
JOBS_DIR = os.path.join(PROJECT_ROOT, "jobs")
QUEUE_DIR = os.path.join(PROJECT_ROOT, "jobs", "queue")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(QUEUE_DIR, exist_ok=True)

# Lock file to track worker state
WORKER_LOCK = os.path.join(JOBS_DIR, ".worker_lock")


# ═══════════════════════════════════════════════════════════════════════════════
# JOB QUEUE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _read_job_meta(job_id):
    """Read job metadata from disk."""
    meta_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _write_job_meta(job_id, data):
    """Write job metadata to disk."""
    meta_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    with open(meta_path, "w") as f:
        json.dump(data, f, indent=2)


def _enqueue_job(job_id):
    """Add job to the queue (touch a file in queue dir with timestamp)."""
    queue_path = os.path.join(QUEUE_DIR, f"{job_id}")
    with open(queue_path, "w") as f:
        f.write(datetime.now().isoformat())


def _get_queue():
    """Get ordered list of queued job IDs (oldest first)."""
    queue_files = sorted(Path(QUEUE_DIR).glob("*"), key=lambda p: p.stat().st_mtime)
    return [p.name for p in queue_files]


def _dequeue_job(job_id):
    """Remove job from queue."""
    queue_path = os.path.join(QUEUE_DIR, f"{job_id}")
    if os.path.exists(queue_path):
        os.remove(queue_path)


def _get_queue_position(job_id):
    """Return 1-based position in queue, or 0 if not queued."""
    queue = _get_queue()
    if job_id in queue:
        return queue.index(job_id) + 1
    return 0


def _move_queue_position(job_id, direction):
    """Move a queued job up or down. direction: -1 = higher priority, +1 = lower."""
    queue = _get_queue()
    if job_id not in queue:
        return
    idx = queue.index(job_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(queue):
        return
    # Swap mtime to reorder — set target position's mtime, then adjust
    queue[idx], queue[new_idx] = queue[new_idx], queue[idx]
    # Rewrite queue files with ordered timestamps
    base_time = time.time() - len(queue)
    for i, jid in enumerate(queue):
        qpath = os.path.join(QUEUE_DIR, jid)
        if os.path.exists(qpath):
            os.utime(qpath, (base_time + i, base_time + i))


def _cancel_job(job_id):
    """Cancel a queued or running job."""
    # Remove from queue if queued
    _dequeue_job(job_id)
    # Update metadata
    meta = _read_job_meta(job_id)
    if meta:
        meta["status"] = "cancelled"
        meta["finished"] = datetime.now().isoformat()
        meta["error"] = "Cancelled by user"
        _write_job_meta(job_id, meta)
    # Signal worker to stop current job (write cancel flag)
    cancel_path = os.path.join(JOBS_DIR, f".cancel_{job_id}")
    with open(cancel_path, "w") as f:
        f.write("cancel")


def _is_cancelled(job_id):
    """Check if a job has been flagged for cancellation."""
    cancel_path = os.path.join(JOBS_DIR, f".cancel_{job_id}")
    return os.path.exists(cancel_path)


def _clear_cancel_flag(job_id):
    """Remove the cancel flag file."""
    cancel_path = os.path.join(JOBS_DIR, f".cancel_{job_id}")
    if os.path.exists(cancel_path):
        os.remove(cancel_path)


# Worker thread name — used to find it via threading.enumerate()
# since Streamlit reruns reset module-level variables.
_WORKER_THREAD_NAME = "nightshift-worker"

# PID-based startup flag — survives Streamlit reruns (same process)
# but correctly resets on actual reboot (new process).
_CLEANUP_FLAG_FILE = os.path.join(JOBS_DIR, ".cleanup_done_pid")


def _cleanup_stale_queue():
    """On startup, clean up queue entries from previous crashed sessions.
    If a job is 'queued' or 'running' but its PDFs no longer exist, mark it as error
    and remove from queue so it doesn't block new jobs.
    Only runs ONCE per process (PID-based flag file survives Streamlit reruns)."""
    # Check if cleanup already ran in THIS process
    my_pid = str(os.getpid())
    if os.path.exists(_CLEANUP_FLAG_FILE):
        try:
            with open(_CLEANUP_FLAG_FILE, "r") as f:
                stored_pid = f.read().strip()
            if stored_pid == my_pid:
                return  # Already cleaned up in this process
        except Exception:
            pass
    # Write our PID so subsequent reruns skip this
    with open(_CLEANUP_FLAG_FILE, "w") as f:
        f.write(my_pid)

    queue = _get_queue()
    for job_id in queue:
        meta = _read_job_meta(job_id)
        if not meta:
            # No metadata — orphaned queue entry, just remove it
            _dequeue_job(job_id)
            print(f"🧹 Removed orphaned queue entry: {job_id}")
            continue

        # Check if PDFs still exist
        pdf_paths = meta.get("pdf_paths", [])
        pdfs_exist = all(os.path.exists(p) for p in pdf_paths) if pdf_paths else False

        if not pdfs_exist:
            # PDFs were lost (reboot wiped uploads) — mark as error
            meta["status"] = "error"
            meta["error"] = "Upload files lost after app reboot. Please re-submit."
            meta["finished"] = datetime.now().isoformat()
            _write_job_meta(job_id, meta)
            _dequeue_job(job_id)
            _clear_progress(job_id)
            _clear_cancel_flag(job_id)
            print(f"🧹 Cleaned stale job (missing PDFs): {job_id}")
            continue

        # If job was 'running' when crash happened, reset to 'queued' so it retries
        # BUT only if the worker thread is NOT alive (actual crash recovery).
        # On Streamlit reruns, the worker IS alive — don't reset its active job!
        if meta.get("status") == "running" and _find_worker_thread() is None:
            meta["status"] = "queued"
            meta.pop("run_started", None)
            _write_job_meta(job_id, meta)
            print(f"🔄 Reset crashed-while-running job to queued: {job_id}")


def _find_worker_thread():
    """Find the worker thread by name via threading.enumerate().
    This survives Streamlit reruns because the thread is still alive
    in the process even though module-level variables get reset."""
    for t in threading.enumerate():
        if t.name == _WORKER_THREAD_NAME and t.is_alive():
            return t
    return None


def _is_worker_running():
    """Check if the background worker is alive by scanning live threads."""
    worker = _find_worker_thread()
    if worker is not None:
        return True
    # No live worker thread found — clean up any stale lock file
    if os.path.exists(WORKER_LOCK):
        os.remove(WORKER_LOCK)
    return False


def _get_progress(job_id):
    """Read the progress file for a running job."""
    progress_path = os.path.join(JOBS_DIR, f".progress_{job_id}.json")
    if not os.path.exists(progress_path):
        return None
    try:
        with open(progress_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _clear_progress(job_id):
    """Remove the progress file when job completes."""
    progress_path = os.path.join(JOBS_DIR, f".progress_{job_id}.json")
    if os.path.exists(progress_path):
        os.remove(progress_path)


def _get_memory_mb():
    """Get current process RSS in MB. Uses /proc on Linux (Streamlit Cloud), resource on macOS."""
    try:
        # Linux (Streamlit Cloud): read from /proc for accurate current RSS
        if os.path.exists("/proc/self/status"):
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024  # KB → MB
        # macOS fallback
        import resource
        rusage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rusage / (1024 * 1024)  # bytes → MB on macOS
    except Exception:
        return 0


# Memory limit: leave headroom for Streamlit itself (~300MB)
MEMORY_LIMIT_MB = int(os.environ.get("NIGHTSHIFT_MEMORY_LIMIT_MB", "700"))


def _send_result_email(meta):
    """Email completed proposal (PDF + JSON) to the contact on file."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("⚠️  SMTP not configured — skipping email delivery")
        return

    contact_name = meta.get("contact_name", "")
    contact_email = meta.get("contact_email", "")
    if not contact_email:
        print("⚠️  No contact email on job — skipping email delivery")
        return

    # Read the output JSON for summary data
    output_json_path = meta.get("output_json", "")
    costs = {}
    analysis = {}
    if output_json_path and os.path.exists(output_json_path):
        try:
            with open(output_json_path, "r") as f:
                data = json.load(f)
            costs = data.get("cost_estimate", {})
            analysis = data.get("analysis", {})
        except Exception:
            pass

    totals = analysis.get("aggregated_totals", {})
    project = analysis.get("project_info", {})

    items_text = ""
    for item in costs.get("line_items", []):
        if item.get("qty", 0) > 0:
            items_text += f"  - {item['item']}: ${item['total']:,.2f}\n"

    body = f"""Hi {contact_name},

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
{items_text}  TOTAL: ${costs.get('subtotal', 0):,.2f}

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
    msg["To"] = f"{contact_name} <{contact_email}>"
    msg["Subject"] = "Knight Shift - Your Painting Estimate is Ready"
    msg.attach(MIMEText(body, "plain"))

    # Attach PDF report
    pdf_path = meta.get("output_pdf", "")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(pdf_path),
            )
            msg.attach(att)

    # Attach JSON analysis
    if output_json_path and os.path.exists(output_json_path):
        with open(output_json_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="json")
            att.add_header(
                "Content-Disposition", "attachment",
                filename=os.path.basename(output_json_path),
            )
            msg.attach(att)

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"📧 Result email sent to {contact_email}")
    except Exception as exc:
        print(f"❌ Failed to send result email: {exc}")
        meta["email_error"] = f"Customer email failed: {exc}"


def _send_internal_notification(meta):
    """Email a copy of the completed estimate to the internal team."""
    if not INTERNAL_NOTIFY_EMAIL or not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("⚠️  Internal notification skipped — INTERNAL_NOTIFY_EMAIL or SMTP not configured")
        return

    job_id = meta.get("job_id", "unknown")
    contact_name = meta.get("contact_name", "N/A")
    contact_email = meta.get("contact_email", "N/A")
    status = meta.get("status", "unknown")

    # Build summary from output JSON
    output_json_path = meta.get("output_json", "")
    summary_lines = ""
    total_cost = "N/A"
    if output_json_path and os.path.exists(output_json_path):
        try:
            with open(output_json_path, "r") as f:
                data = json.load(f)
            costs = data.get("cost_estimate", {})
            analysis = data.get("analysis", {})
            totals = analysis.get("aggregated_totals", {})
            project = analysis.get("project_info", {})
            total_cost = f"${costs.get('subtotal', 0):,.2f}"
            summary_lines = (
                f"  Floors analyzed: {project.get('total_floors_analyzed', 'N/A')}\n"
                f"  Rooms found:     {project.get('total_rooms_found', 'N/A')}\n"
                f"  Wall sqft:       {totals.get('total_paintable_wall_sqft', 0):,.0f}\n"
                f"  Ceiling sqft:    {totals.get('total_paintable_ceiling_sqft', 0):,.0f}\n"
                f"  Doors:           {totals.get('total_doors_full_paint', 0):,.0f}\n"
            )
        except Exception:
            pass

    error_info = ""
    if status == "error":
        error_info = f"\nERROR: {meta.get('error', 'Unknown error')}\n"

    body = f"""Knight Shift — Job Complete

Job ID:   {job_id}
Status:   {status.upper()}
Contact:  {contact_name} ({contact_email})
Submitted: {meta.get('submitted', 'N/A')}
Finished:  {meta.get('finished', 'N/A')}
{error_info}
ESTIMATE TOTAL: {total_cost}

{summary_lines}
PDF and JSON reports are attached (if available).
"""

    msg = MIMEMultipart()
    msg["From"] = f"{COMPANY_NAME} <{EMAIL_ADDRESS}>"
    msg["To"] = INTERNAL_NOTIFY_EMAIL
    msg["Subject"] = f"[Knight Shift] {status.upper()}: {contact_name} — {total_cost}"
    msg.attach(MIMEText(body, "plain"))

    # Attach PDF report
    pdf_path = meta.get("output_pdf", "")
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header("Content-Disposition", "attachment", filename=os.path.basename(pdf_path))
            msg.attach(att)

    # Attach JSON analysis
    if output_json_path and os.path.exists(output_json_path):
        with open(output_json_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="json")
            att.add_header("Content-Disposition", "attachment", filename=os.path.basename(output_json_path))
            msg.attach(att)

    try:
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"📧 Internal notification sent to {INTERNAL_NOTIFY_EMAIL}")
    except Exception as exc:
        print(f"❌ Failed to send internal notification: {exc}")
        meta["internal_email_error"] = f"Internal email failed: {exc}"


def _process_single_job(job_id):
    """Process one job in a SUBPROCESS so its memory is fully reclaimed on exit.

    This is critical for Streamlit Cloud's 1GB free tier:
    - Takeoff_DIRECT + PyMuPDF tile rendering can use 500-800MB
    - Running in-process would crash the Streamlit app
    - A subprocess isolates all that memory; when it exits, the OS reclaims everything
    - Even if the subprocess is OOM-killed, the parent Streamlit process survives
    """
    import subprocess as _sp

    meta = _read_job_meta(job_id)
    if not meta:
        _dequeue_job(job_id)
        return

    # Check if already cancelled before starting
    if _is_cancelled(job_id):
        _dequeue_job(job_id)
        _clear_cancel_flag(job_id)
        return

    # Update status to running
    meta["status"] = "running"
    meta["run_started"] = datetime.now().isoformat()
    _write_job_meta(job_id, meta)

    # Set progress file path so Takeoff_DIRECT can write updates
    progress_path = os.path.join(JOBS_DIR, f".progress_{job_id}.json")

    # Build subprocess command
    # Put PDFs in a temp directory that Takeoff_DIRECT can read via --rfp_dir
    pdf_paths = meta.get("pdf_paths", [])
    if not pdf_paths:
        meta["status"] = "error"
        meta["error"] = "No PDF files provided"
        meta["finished"] = datetime.now().isoformat()
        _write_job_meta(job_id, meta)
        _dequeue_job(job_id)
        return

    # Use the upload directory as rfp_dir (all PDFs for this job are there)
    rfp_dir = os.path.dirname(pdf_paths[0])

    cmd = [
        sys.executable, "-u",
        os.path.join(PROJECT_ROOT, "Takeoff_DIRECT.py"),
        "--rfp_dir", rfp_dir,
        "--contact_name", meta.get("contact_name", ""),
        "--contact_email", meta.get("contact_email", ""),
    ]
    if meta.get("scope_notes"):
        cmd += ["--scope", meta["scope_notes"]]
    if meta.get("image_fallback", True):
        cmd.append("--image-fallback")
    else:
        cmd.append("--no-image-fallback")
    if meta.get("multi_pass", False):
        cmd.append("--multi-pass")

    # Pass pricing overrides as JSON string via --rate-overrides-json
    _all_overrides = {}
    for pm_key, rate_val in meta.get("rate_overrides", {}).items():
        _all_overrides[pm_key] = rate_val
    for pm_key, markup_val in meta.get("markup_overrides", {}).items():
        _all_overrides[f"markup_{pm_key}"] = markup_val
    if _all_overrides:
        cmd += ["--rate-overrides-json", json.dumps(_all_overrides)]

    # Pass API key and progress file via environment
    env = os.environ.copy()
    env["NIGHTSHIFT_PROGRESS_FILE"] = progress_path
    # Memory limit for subprocess.
    # Streamlit Cloud free tier: 1GB total. We give the subprocess as much as
    # possible (1.5GB virtual address space) because PyMuPDF needs headroom for
    # large DD-scale architectural pages. The OS reclaims everything when the
    # subprocess exits, so the parent Streamlit process stays safe.
    # Note: RLIMIT_AS caps virtual address space (not RSS), so 1.5GB is fine
    # even on a 1GB host — only resident pages count against physical RAM.
    _SUB_MEM_LIMIT_MB = 1536
    env["NIGHTSHIFT_MEM_LIMIT_MB"] = str(_SUB_MEM_LIMIT_MB)
    # Allow more tile pages for large-format PDFs
    env["NIGHTSHIFT_MAX_TILE_PAGES"] = "12"

    def _limit_memory():
        """Set memory limit on subprocess (Linux only)."""
        try:
            import resource
            limit_bytes = _SUB_MEM_LIMIT_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except Exception:
            pass  # macOS doesn't support RLIMIT_AS; skip

    try:
        print(f"🚀 Starting subprocess for job {job_id[:8]} (mem limit: {_SUB_MEM_LIMIT_MB}MB)...")
        proc = _sp.Popen(
            cmd, env=env, cwd=PROJECT_ROOT,
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
            text=True, bufsize=1,
            preexec_fn=_limit_memory,
        )

        # Stream output and check for cancellation
        output_lines = []
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                print(f"  [{job_id[:8]}] {line.rstrip()}")
                output_lines.append(line)

            # Check cancellation every line
            if _is_cancelled(job_id):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except _sp.TimeoutExpired:
                    proc.kill()
                meta["status"] = "cancelled"
                meta["finished"] = datetime.now().isoformat()
                meta["error"] = "Cancelled by user during processing"
                _clear_cancel_flag(job_id)
                _write_job_meta(job_id, meta)
                _dequeue_job(job_id)
                _clear_progress(job_id)
                return

        exit_code = proc.returncode
        print(f"📊 Subprocess for {job_id[:8]} exited with code {exit_code}")

        if exit_code == 0:
            # Find the output files (Takeoff_DIRECT writes to output/ dir)
            output_json = ""
            output_pdf = ""
            output_dir = os.path.join(PROJECT_ROOT, "output")
            if os.path.isdir(output_dir):
                # Find the most recent JSON and PDF in output/
                json_files = sorted(
                    [f for f in os.listdir(output_dir) if f.endswith(".json")
                     and f.startswith("construction_analysis")],
                    key=lambda f: os.path.getmtime(os.path.join(output_dir, f)),
                    reverse=True,
                )
                pdf_files = sorted(
                    [f for f in os.listdir(output_dir) if f.endswith(".pdf")
                     and f.startswith("construction_analysis")],
                    key=lambda f: os.path.getmtime(os.path.join(output_dir, f)),
                    reverse=True,
                )
                if json_files:
                    output_json = os.path.join(output_dir, json_files[0])
                if pdf_files:
                    output_pdf = os.path.join(output_dir, pdf_files[0])

            meta["status"] = "done"
            meta["finished"] = datetime.now().isoformat()
            meta["output_json"] = output_json
            meta["output_pdf"] = output_pdf

            # Email the completed proposal to the contact
            _send_result_email(meta)
            _send_internal_notification(meta)
        elif exit_code == -9 or exit_code == 137:
            # SIGKILL = OOM killer
            meta["status"] = "error"
            meta["error"] = (
                "Process killed (likely out of memory). "
                "This PDF may be too large for the free tier. "
                "Try splitting it into smaller files or upgrading the plan."
            )
            meta["finished"] = datetime.now().isoformat()
        else:
            # Extract error from last few lines of output
            last_lines = "".join(output_lines[-10:]) if output_lines else "No output"
            meta["status"] = "error"
            meta["error"] = f"Process exited with code {exit_code}. {last_lines[-500:]}"
            meta["finished"] = datetime.now().isoformat()

    except Exception as e:
        meta["status"] = "error"
        meta["error"] = f"Failed to start processing: {str(e)}"
        meta["finished"] = datetime.now().isoformat()

    _write_job_meta(job_id, meta)
    _dequeue_job(job_id)
    _clear_progress(job_id)

    # Send internal notification for errors (success notification is sent above)
    if meta.get("status") == "error":
        _send_internal_notification(meta)

    # Clean up uploaded PDFs (output is saved separately)
    for pdf_path in meta.get("pdf_paths", []):
        try:
            if os.path.exists(pdf_path) and UPLOAD_DIR in pdf_path:
                os.remove(pdf_path)
        except Exception:
            pass
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    try:
        if os.path.isdir(job_upload_dir) and not os.listdir(job_upload_dir):
            os.rmdir(job_upload_dir)
    except Exception:
        pass

    print(f"✅ Job {job_id[:8]} cleanup complete. Streamlit RSS: {_get_memory_mb():.0f}MB")


def _cleanup_old_jobs(keep=20):
    """Remove old completed job metadata and output files, keeping the most recent N."""
    try:
        job_files = sorted(
            Path(JOBS_DIR).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Skip non-meta files (progress files etc.)
        meta_files = [f for f in job_files if not f.name.startswith(".")]
        for old_file in meta_files[keep:]:
            try:
                meta = json.loads(old_file.read_text())
                # Only clean up finished jobs
                if meta.get("status") not in ("done", "error", "cancelled"):
                    continue
                # Remove output files
                for key in ("output_json", "output_pdf"):
                    fpath = meta.get(key, "")
                    if fpath and os.path.exists(fpath):
                        os.remove(fpath)
                # Remove meta file
                old_file.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _worker_loop():
    """Background worker — processes queue one job at a time."""
    # Write lock
    with open(WORKER_LOCK, "w") as f:
        json.dump({"alive": True, "started": datetime.now().isoformat()}, f)

    try:
        while True:
            queue = _get_queue()
            if not queue:
                print("📭 Worker: queue empty, exiting")
                break

            job_id = queue[0]  # Process oldest first
            print(f"📋 Worker: processing job {job_id[:8]}...")
            try:
                _process_single_job(job_id)
            except Exception as e:
                print(f"❌ Worker: job {job_id[:8]} failed with exception: {e}")
                import traceback
                traceback.print_exc()
                # Mark job as error so it doesn't block the queue
                meta = _read_job_meta(job_id)
                if meta and meta.get("status") not in ("done", "cancelled", "error"):
                    meta["status"] = "error"
                    meta["error"] = f"Worker exception: {str(e)}"
                    meta["finished"] = datetime.now().isoformat()
                    _write_job_meta(job_id, meta)
                _dequeue_job(job_id)
                _clear_progress(job_id)

            # Clean up old jobs periodically to free disk
            _cleanup_old_jobs(keep=20)

            # Brief pause between jobs
            time.sleep(2)
    except Exception as e:
        print(f"❌ Worker loop crashed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Clean up lock
        if os.path.exists(WORKER_LOCK):
            os.remove(WORKER_LOCK)
        print("🔒 Worker: lock cleaned up, thread exiting")


def _ensure_worker():
    """Start the worker thread if not already running.
    Uses threading.enumerate() to detect existing worker — survives Streamlit reruns."""
    queue = _get_queue()
    if not queue:
        return
    existing = _find_worker_thread()
    if existing is not None:
        print(f"✅ Worker already running: {existing.name} alive={existing.is_alive()}")
        return
    print(f"🚀 Starting worker thread (queue has {len(queue)} job(s))")
    t = threading.Thread(target=_worker_loop, daemon=True, name=_WORKER_THREAD_NAME)
    t.start()
    # Give it a moment to actually start
    time.sleep(0.5)
    print(f"   Worker thread started: alive={t.is_alive()}")


# ═══════════════════════════════════════════════════════════════════════════════
# FUTURISTIC PAINTBRUSH LOGO (inline SVG)
# ═══════════════════════════════════════════════════════════════════════════════
# Load the Knight Shift helmet logo as base64 for inline HTML embedding
def _load_logo_b64():
    """Load the helmet logo PNG and return as a base64 data URI img tag."""
    import base64
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo_helm.png")
    try:
        with open(logo_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f'<img src="data:image/png;base64,{b64}" width="56" height="72" alt="Knight Shift">'
    except FileNotFoundError:
        return '<div style="width:56px;height:72px;"></div>'

LOGO_SVG = _load_logo_b64()


# ═══════════════════════════════════════════════════════════════════════════════
# STYLING — Dark Blue Theme
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
    /* ── Global dark overrides ── */
    .stApp { background-color: #0a1628; }
    section[data-testid="stSidebar"] { background-color: #0d1f3c; }
    section[data-testid="stSidebar"] .stMarkdown,
    section[data-testid="stSidebar"] label { color: #c8ddf0; }

    /* ── Header banner ── */
    .main-header {
        background: linear-gradient(135deg, #0d1f3c 0%, #142d54 50%, #0a1628 100%);
        border: 1px solid rgba(0,212,255,0.15);
        padding: 1.8rem 2rem;
        border-radius: 14px;
        margin-bottom: 1.5rem;
        display: flex;
        align-items: center;
        gap: 1.2rem;
        box-shadow: 0 0 30px rgba(0,212,255,0.08), inset 0 1px 0 rgba(255,255,255,0.04);
    }
    .main-header .logo-wrap { flex-shrink: 0; }
    .main-header .header-text h1 {
        color: #e0e8f0;
        margin: 0;
        font-size: 2rem;
        font-weight: 700;
        letter-spacing: -0.02em;
    }
    .main-header .header-text h1 span.accent { color: #1a8fd4; }
    .main-header .header-text p {
        color: #2ecc71;
        margin: 0.3rem 0 0 0;
        font-size: 0.95rem;
        letter-spacing: 0.02em;
    }

    /* ── Status cards — dark variants ── */
    .status-card {
        padding: 1rem 1.5rem;
        border-radius: 10px;
        margin: 0.5rem 0;
        border-left: 4px solid;
        color: #e0e8f0;
    }
    .status-card small { color: #8aa4c4; }
    .status-running { background: rgba(240,165,0,0.08); border-color: #f0a500; }
    .status-queued  { background: rgba(0,212,255,0.06); border-color: #00d4ff; }
    .status-done    { background: rgba(0,200,100,0.08); border-color: #00c864; }
    .status-error   { background: rgba(220,60,60,0.08); border-color: #dc3c3c; }

    /* ── Metric cards ── */
    [data-testid="stMetric"] {
        background: rgba(0,212,255,0.04);
        border: 1px solid rgba(0,212,255,0.1);
        border-radius: 10px;
        padding: 0.8rem 1rem;
    }
    [data-testid="stMetricLabel"] { color: #7a9cc6 !important; }
    [data-testid="stMetricValue"] { color: #e0e8f0 !important; }

    /* ── Tabs ── */
    button[data-baseweb="tab"] { color: #7a9cc6; }
    button[data-baseweb="tab"][aria-selected="true"] { color: #00d4ff; }

    /* ── Dataframes ── */
    .stDataFrame { border: 1px solid rgba(0,212,255,0.1); border-radius: 8px; }

    /* ── Expander ── */
    details summary { color: #e0e8f0 !important; }

    /* ── Download buttons ── */
    .stDownloadButton button {
        background: rgba(0,212,255,0.1) !important;
        border: 1px solid rgba(0,212,255,0.25) !important;
        color: #00d4ff !important;
    }
    .stDownloadButton button:hover {
        background: rgba(0,212,255,0.2) !important;
        border-color: #00d4ff !important;
    }

    /* ── Footer ── */
    .footer-text { color: #4a6a8a; }
    .footer-text a { color: #00d4ff; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<div class="main-header">
    <div class="logo-wrap">{LOGO_SVG}</div>
    <div class="header-text">
        <h1><span class="accent">Knight</span> Shift</h1>
        <p>Forged by Willpower</p>
    </div>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR — Branding only
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown(f'<div style="text-align:center;margin-bottom:0.5rem;">{LOGO_SVG}</div>', unsafe_allow_html=True)
    st.markdown("#### Knight Shift")
    st.caption("Forged by Willpower")
    st.markdown("---")
    current_queue = _get_queue()
    if current_queue:
        st.warning(f"📊 Queue: {len(current_queue)} job(s) waiting")
    else:
        st.success("✅ Queue empty — jobs start immediately")


# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP + ENSURE WORKER IS RUNNING (on every page load)
# ═══════════════════════════════════════════════════════════════════════════════
_cleanup_stale_queue()   # Clear orphaned jobs from previous crashes (runs once)
_ensure_worker()         # Start worker thread if queue has jobs


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN AREA — JOB DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def _load_all_jobs():
    """Load all job metadata from disk."""
    jobs = {}
    for jf in sorted(Path(JOBS_DIR).glob("*.json"), reverse=True):
        try:
            with open(jf) as f:
                data = json.load(f)
            jid = data.get("job_id", jf.stem)
            jobs[jid] = data
        except Exception:
            pass
    return jobs

all_jobs = _load_all_jobs()

# ── Tabs ──
tab_new, tab_active, tab_history = st.tabs(["🖌️ New Estimate", "📊 Active & Queued", "📁 Completed"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: NEW ESTIMATE
# ═══════════════════════════════════════════════════════════════════════════════
with tab_new:
    st.header("New Estimate")

    col_left, col_right = st.columns([1, 1])

    with col_left:
        contact_name = st.text_input("Contact Name *", placeholder="e.g., John Smith")
        contact_email = st.text_input("Contact Email *", placeholder="e.g., john@rider.com")

        st.markdown("---")

        upload_mode = st.radio(
            "Upload Method",
            ["Individual PDFs", "ZIP Folder"],
            horizontal=True,
            help="Upload PDFs one at a time, or a ZIP containing a folder of PDFs.",
        )

        # Upload key counter — incremented after submission to clear the file uploader
        if "upload_key" not in st.session_state:
            st.session_state.upload_key = 0

        uploaded_files = []
        if upload_mode == "Individual PDFs":
            raw_uploads = st.file_uploader(
                "Upload Construction PDFs *",
                type=["pdf"],
                accept_multiple_files=True,
                help="Architectural drawings — floor plans, elevations, schedules. Max 200MB each.",
                key=f"pdf_uploader_{st.session_state.upload_key}",
            )
            if raw_uploads:
                uploaded_files = raw_uploads
        else:
            zip_upload = st.file_uploader(
                "Upload ZIP folder containing PDFs *",
                type=["zip"],
                accept_multiple_files=False,
                help="A .zip file containing one or more PDF drawings. Subfolders are included.",
                key=f"zip_uploader_{st.session_state.upload_key}",
            )
            if zip_upload:
                import zipfile
                import io
                try:
                    zf = zipfile.ZipFile(io.BytesIO(zip_upload.getvalue()))
                    pdf_names = [n for n in zf.namelist()
                                 if n.lower().endswith(".pdf") and not n.startswith("__MACOSX")]
                    if pdf_names:
                        st.info(f"Found **{len(pdf_names)}** PDF(s) in ZIP: {', '.join(os.path.basename(n) for n in pdf_names[:5])}{'...' if len(pdf_names) > 5 else ''}")
                        class _ZipPDF:
                            def __init__(self, name, data):
                                self.name = os.path.basename(name)
                                self._data = data
                            def getbuffer(self):
                                return self._data
                        for pname in pdf_names:
                            uploaded_files.append(_ZipPDF(pname, zf.read(pname)))
                    else:
                        st.warning("No PDF files found in the ZIP archive.")
                except Exception as e:
                    st.error(f"Could not read ZIP file: {e}")

        st.markdown("---")
        with st.expander("⚙️ Advanced Options", expanded=False):
            scope_notes = st.text_area(
                "Scope Notes",
                placeholder="e.g., Residential floors 2-4 only, skip basement",
                help="Optional free-form notes to guide the extraction.",
            )
            image_fallback = st.checkbox(
                "Image Fallback",
                value=True,
                help="Render large pages as images when native PDF extraction fails.",
            )
            multi_pass = st.checkbox(
                "Multi-Pass Extraction",
                value=False,
                help="Run floor plans twice, keep best extraction. Slower but more accurate.",
            )

    with col_right:
        st.markdown("#### Pricing — Rates & Markups")
        st.caption("These are the system default rates. "
                   "Adjust any values below — changes apply to this job only.")

        _pricing_display = [
            ("gyp_walls",          "Gyp. Walls",          "sqft"),
            ("gyp_ceilings",       "Gyp. Ceilings",       "sqft"),
            ("base_trim",          "Base Trim",            "LF"),
            ("doors_full_paint",   "Doors (Full Paint)",   "ea"),
            ("doors_hm_panel",     "Doors (HM Panel)",     "ea"),
            ("doors_frame_only",   "Doors (Frame Only)",   "ea"),
            ("windows",            "Windows",              "ea"),
            ("stairs",             "Stairs",               "ea"),
            ("cmu_walls_full",     "CMU Walls (Full)",     "sqft"),
            ("dryfall_ceiling",    "Dryfall Ceiling",      "sqft"),
            ("concrete_sealer",    "Concrete Sealer",      "sqft"),
            ("painted_columns",    "Painted Columns",      "ea"),
            ("wallcovering_install","Wallcovering Install", "sqft"),
            ("stained_wood",       "Stained Wood",         "sqft"),
            ("exterior_cornice",   "Ext. Cornice",         "LF"),
            ("exterior_window_trim","Ext. Window Trim",    "LF"),
            ("exterior_painting",  "Ext. Painting",        "sqft"),
            ("exterior_hardie_siding","Ext. Hardie Siding","sqft"),
            ("exterior_lift_rental","Ext. Lift Rental",    "ea"),
        ]

        _pricing_rows = []
        for pm_key, label, unit in _pricing_display:
            if pm_key in PRICING_MODEL:
                cfg = PRICING_MODEL[pm_key]
                default_rate = cfg["tiers"][-1]["rate"] if cfg["tiers"] else 0
                default_markup = cfg["markup"]
                _pricing_rows.append({
                    "_key": pm_key,
                    "Item": label,
                    "Unit": f"/{unit}",
                    "Rate ($)": default_rate,
                    "Markup (%)": round(default_markup * 100, 1),
                })

        pricing_df = pd.DataFrame(_pricing_rows)
        edited_pricing = st.data_editor(
            pricing_df[["Item", "Unit", "Rate ($)", "Markup (%)"]],
            column_config={
                "Item": st.column_config.TextColumn("Item", disabled=True, width="medium"),
                "Unit": st.column_config.TextColumn("Unit", disabled=True, width="small"),
                "Rate ($)": st.column_config.NumberColumn("Rate ($)", format="$%.2f", min_value=0.0, step=0.01),
                "Markup (%)": st.column_config.NumberColumn("Markup (%)", format="%.1f", min_value=0.0, max_value=100.0, step=0.5),
            },
            use_container_width=True,
            hide_index=True,
            key="pricing_editor",
            num_rows="fixed",
        )

        _rate_overrides = {}
        _markup_overrides = {}
        if edited_pricing is not None:
            for i, row in enumerate(_pricing_rows):
                pm_key = row["_key"]
                orig_rate = row["Rate ($)"]
                orig_markup = row["Markup (%)"]
                new_rate = edited_pricing.iloc[i]["Rate ($)"]
                new_markup = edited_pricing.iloc[i]["Markup (%)"]
                if abs(new_rate - orig_rate) > 0.001:
                    _rate_overrides[pm_key] = new_rate
                if abs(new_markup - orig_markup) > 0.01:
                    _markup_overrides[pm_key] = new_markup / 100.0

        if _rate_overrides or _markup_overrides:
            changes = len(_rate_overrides) + len(_markup_overrides)
            st.success(f"{changes} pricing adjustment(s) will be applied to this job.")

    # ── Submit button (full width below columns) ──
    st.markdown("---")
    can_submit = bool(contact_name and contact_email and uploaded_files)

    if st.button("🚀 Generate Estimate", type="primary", use_container_width=True, disabled=not can_submit):
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        pdf_paths = []
        for f in uploaded_files:
            fpath = os.path.join(job_dir, f.name)
            with open(fpath, "wb") as out:
                out.write(f.getbuffer())
            pdf_paths.append(fpath)

        meta = {
            "job_id": job_id,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "scope_notes": scope_notes,
            "pdf_paths": pdf_paths,
            "image_fallback": image_fallback,
            "multi_pass": multi_pass,
            "submitted": datetime.now().isoformat(),
            "status": "queued",
        }
        if _rate_overrides:
            meta["rate_overrides"] = _rate_overrides
        if _markup_overrides:
            meta["markup_overrides"] = _markup_overrides
        _write_job_meta(job_id, meta)

        _enqueue_job(job_id)
        _ensure_worker()

        st.session_state.upload_key += 1

        queue_pos = _get_queue_position(job_id)
        if queue_pos <= 1:
            st.success(f"✅ Job **{job_id}** submitted! Processing now...")
        else:
            st.info(f"📋 Job **{job_id}** queued — position **#{queue_pos}**. It will start automatically when the current job finishes.")
        st.markdown("""
        <style>
            @keyframes flyUp {
                0% { transform: translateY(100vh) rotate(0deg); opacity: 1; }
                80% { opacity: 1; }
                100% { transform: translateY(-20vh) rotate(360deg); opacity: 0; }
            }
            .paintbrush-container {
                position: fixed;
                top: 0; left: 0;
                width: 100vw; height: 100vh;
                pointer-events: none;
                z-index: 999999;
                overflow: hidden;
            }
            .paintbrush {
                position: absolute;
                bottom: -60px;
                font-size: 2.5rem;
                animation: flyUp 2.5s ease-out forwards;
            }
        </style>
        <div class="paintbrush-container">
            <span class="paintbrush" style="left:5%; animation-delay:0s;">🖌️</span>
            <span class="paintbrush" style="left:12%; animation-delay:0.15s;">🎨</span>
            <span class="paintbrush" style="left:20%; animation-delay:0.3s;">🖌️</span>
            <span class="paintbrush" style="left:28%; animation-delay:0.1s;">🖌️</span>
            <span class="paintbrush" style="left:35%; animation-delay:0.45s;">🎨</span>
            <span class="paintbrush" style="left:42%; animation-delay:0.2s;">🖌️</span>
            <span class="paintbrush" style="left:50%; animation-delay:0.35s;">🖌️</span>
            <span class="paintbrush" style="left:58%; animation-delay:0.05s;">🎨</span>
            <span class="paintbrush" style="left:65%; animation-delay:0.4s;">🖌️</span>
            <span class="paintbrush" style="left:72%; animation-delay:0.25s;">🖌️</span>
            <span class="paintbrush" style="left:80%; animation-delay:0.5s;">🎨</span>
            <span class="paintbrush" style="left:88%; animation-delay:0.15s;">🖌️</span>
            <span class="paintbrush" style="left:95%; animation-delay:0.35s;">🖌️</span>
        </div>
        """, unsafe_allow_html=True)

    if not can_submit and uploaded_files:
        st.warning("Please fill in Contact Name and Email.")

with tab_active:
    active_jobs = {k: v for k, v in all_jobs.items() if v.get("status") in ("running", "queued")}

    if not active_jobs:
        st.info("No active jobs. Submit PDFs in the **New Estimate** tab to start.")
    else:
        for job_id, job in active_jobs.items():
            status = job.get("status", "unknown")
            queue_pos = _get_queue_position(job_id)
            pdf_names = [os.path.basename(p) for p in job.get("pdf_paths", [])]
            queue = _get_queue()

            if status == "running":
                progress = _get_progress(job_id)

                st.markdown(f"""
                <div class="status-card status-running">
                    <strong>⏳ Processing:</strong> {job_id}<br/>
                    <small>Contact: {job.get('contact_name', '')} | Files: {', '.join(pdf_names)} | Started: {job.get('run_started', job.get('submitted', ''))}</small>
                </div>
                """, unsafe_allow_html=True)

                # Progress bar and step label
                if progress:
                    pct = progress.get("pct", 0) / 100.0
                    step_label = progress.get("label", "Processing...")
                    step_detail = progress.get("detail", "")
                    step_num = progress.get("step", 0)
                    total_steps = progress.get("total_steps", 8)
                    st.progress(pct, text=f"**Step {step_num}/{total_steps}: {step_label}** — {step_detail}")
                else:
                    st.progress(0.0, text="**Starting...** Waiting for engine to initialize")

                # Stop + refresh buttons
                btn_col1, btn_col2, _ = st.columns([1, 1, 4])
                with btn_col1:
                    if st.button("🛑 Stop Job", key=f"stop_{job_id}", type="secondary"):
                        _cancel_job(job_id)
                        st.warning(f"Cancellation requested for **{job_id}**.")
                        st.rerun()
                with btn_col2:
                    if st.button("🔄 Refresh", key=f"refresh_running_{job_id}"):
                        st.rerun()

            elif status == "queued":
                st.markdown(f"""
                <div class="status-card status-queued">
                    <strong>📋 Queued (#{queue_pos}):</strong> {job_id}<br/>
                    <small>Contact: {job.get('contact_name', '')} | Files: {', '.join(pdf_names)} | Submitted: {job.get('submitted', '')}</small>
                </div>
                """, unsafe_allow_html=True)

                # Priority & cancel controls for queued jobs
                btn_cols = st.columns([1, 1, 1, 3])
                with btn_cols[0]:
                    can_move_up = queue_pos > 1 and job_id in queue
                    if st.button("⬆️ Priority", key=f"up_{job_id}", disabled=not can_move_up):
                        _move_queue_position(job_id, -1)
                        st.rerun()
                with btn_cols[1]:
                    can_move_down = queue_pos < len(queue) and job_id in queue
                    if st.button("⬇️ Lower", key=f"down_{job_id}", disabled=not can_move_down):
                        _move_queue_position(job_id, 1)
                        st.rerun()
                with btn_cols[2]:
                    if st.button("❌ Cancel", key=f"cancel_{job_id}"):
                        _cancel_job(job_id)
                        st.rerun()

        if not any(v.get("status") == "running" for v in active_jobs.values()):
            st.markdown("")
            if st.button("🔄 Refresh Status", key="refresh_active"):
                st.rerun()

with tab_history:
    completed_jobs = {k: v for k, v in all_jobs.items() if v.get("status") in ("done", "error", "cancelled")}

    if not completed_jobs:
        st.info("No completed jobs yet.")
    else:
        for job_id, job in completed_jobs.items():
            is_done = job.get("status") == "done"
            is_cancelled = job.get("status") == "cancelled"
            icon = "✅" if is_done else ("🚫" if is_cancelled else "❌")
            css_class = "status-done" if is_done else "status-error"
            pdf_names = [os.path.basename(p) for p in job.get("pdf_paths", [])]

            with st.container():
                st.markdown(f"""
                <div class="status-card {css_class}">
                    <strong>{icon} {job_id}</strong><br/>
                    <small>Contact: {job.get('contact_name', '')} ({job.get('contact_email', '')}) | Files: {', '.join(pdf_names)}</small>
                </div>
                """, unsafe_allow_html=True)

                if is_done:
                    col1, col2 = st.columns(2)

                    pdf_path = job.get("output_pdf", "")
                    json_path = job.get("output_json", "")

                    if pdf_path and os.path.exists(pdf_path):
                        with open(pdf_path, "rb") as pf:
                            col1.download_button(
                                "📄 Download Proposal PDF",
                                data=pf.read(),
                                file_name=os.path.basename(pdf_path),
                                mime="application/pdf",
                                key=f"pdf_{job_id}",
                            )

                    if json_path and os.path.exists(json_path):
                        with open(json_path, "rb") as jf:
                            col2.download_button(
                                "📊 Download JSON Data",
                                data=jf.read(),
                                file_name=os.path.basename(json_path),
                                mime="application/json",
                                key=f"json_{job_id}",
                            )

                    # ── Cost summary ──
                    if json_path and os.path.exists(json_path):
                        try:
                            with open(json_path) as jf:
                                analysis_data = json.load(jf)

                            costs = analysis_data.get("cost_estimate", {})
                            line_items = costs.get("line_items", [])
                            total = costs.get("subtotal", 0) or costs.get("total_cost", 0) or 0
                            analysis = analysis_data.get("analysis", {})
                            agg = analysis.get("aggregated_totals", {})
                            pi = analysis.get("project_info", {})
                            rooms = pi.get("total_rooms_found", 0) or 0
                            wall_sf = agg.get("total_paintable_wall_sqft", 0) or 0
                            ceiling_sf = agg.get("total_paintable_ceiling_sqft", 0) or 0

                            # ── Compute interior vs exterior breakdown ──
                            interior_total = 0
                            exterior_total = 0
                            interior_items = []
                            exterior_items = []
                            for li in line_items:
                                li_total = li.get("total", 0) or li.get("total_cost", 0) or 0
                                li_name = li.get("item", "") or li.get("description", "")
                                is_ext = any(kw in li_name.lower() for kw in (
                                    "ext.", "exterior", "hardie", "azek", "lintel",
                                    "corner board", "soffit", "cornice", "lift rental"))
                                if is_ext:
                                    exterior_total += li_total
                                    exterior_items.append(li)
                                else:
                                    interior_total += li_total
                                    interior_items.append(li)

                            with st.expander(f"💰 Estimate Summary — ${total:,.0f}", expanded=True):
                                # ── Build editable line item tables ──
                                def _build_editable_df(items):
                                    """Build a DataFrame with original + adjustable columns."""
                                    rows = []
                                    for li in items:
                                        li_total = li.get("total", 0) or li.get("total_cost", 0) or 0
                                        if li_total <= 0:
                                            continue
                                        qty = float(li.get("qty", 0) or li.get("quantity", 0) or 0)
                                        cost = float(li.get("cost", 0) or 0)
                                        markup = float(li.get("markup", 0) or 0)
                                        rate = round(cost / qty, 2) if qty > 0 else 0.0
                                        markup_pct = round(markup / cost, 4) if cost > 0 else 0.06
                                        rows.append({
                                            "Line Item": li.get("item", "") or li.get("description", ""),
                                            "Orig Qty": qty,
                                            "Orig Rate": rate,
                                            "Adjusted Qty": qty,
                                            "Adjusted Rate": rate,
                                            "_markup_pct": markup_pct,
                                            "Orig Total": float(li_total),
                                        })
                                    return pd.DataFrame(rows) if rows else None

                                int_df = _build_editable_df(interior_items)
                                ext_df = _build_editable_df(exterior_items)

                                # Reserve a container at the top for summary metrics
                                # (populated after editors compute adjusted totals)
                                summary_container = st.container()

                                st.markdown("---")
                                st.info("Edit the **Adjusted Qty** and **Adjusted Rate** columns to see updated totals. Original values are locked for reference.")

                                # ── Interior line items (editable) ──
                                st.markdown(f"**🏠 Interior — Original: ${interior_total:,.0f}**")
                                adj_int_total = interior_total
                                if int_df is not None and not int_df.empty:
                                    edited_int = st.data_editor(
                                        int_df[["Line Item", "Orig Qty", "Orig Rate", "Adjusted Qty", "Adjusted Rate"]],
                                        column_config={
                                            "Line Item": st.column_config.TextColumn("Line Item", disabled=True, width="large"),
                                            "Orig Qty": st.column_config.NumberColumn("Orig Qty", disabled=True, format="%.0f"),
                                            "Orig Rate": st.column_config.NumberColumn("Orig Rate ($)", disabled=True, format="$%.2f"),
                                            "Adjusted Qty": st.column_config.NumberColumn("Adjusted Qty", format="%.0f", min_value=0),
                                            "Adjusted Rate": st.column_config.NumberColumn("Adjusted Rate ($)", format="$%.2f", min_value=0.0, step=0.01),
                                        },
                                        use_container_width=True,
                                        hide_index=True,
                                        key=f"int_edit_{job_id}",
                                        num_rows="fixed",
                                    )
                                    # Recalculate from edited values
                                    if edited_int is not None:
                                        _adj_cost = edited_int["Adjusted Qty"] * edited_int["Adjusted Rate"]
                                        _adj_markup = _adj_cost * int_df["_markup_pct"]
                                        _adj_line_totals = (_adj_cost + _adj_markup).round(2)
                                        adj_int_total = _adj_line_totals.sum()

                                        # Show per-line adjusted totals
                                        summary_int = pd.DataFrame({
                                            "Line Item": edited_int["Line Item"],
                                            "Adjusted Total": _adj_line_totals,
                                            "Orig Total": int_df["Orig Total"],
                                            "Change": _adj_line_totals - int_df["Orig Total"],
                                        })
                                        has_changes = (summary_int["Change"].abs() > 0.01).any()
                                        if has_changes:
                                            changed = summary_int[summary_int["Change"].abs() > 0.01]
                                            st.dataframe(
                                                changed[["Line Item", "Orig Total", "Adjusted Total", "Change"]],
                                                column_config={
                                                    "Line Item": st.column_config.TextColumn("Line Item", width="large"),
                                                    "Orig Total": st.column_config.NumberColumn("Orig Total", format="$%.0f"),
                                                    "Adjusted Total": st.column_config.NumberColumn("Adjusted Total", format="$%.0f"),
                                                    "Change": st.column_config.NumberColumn("Change", format="$%+.0f"),
                                                },
                                                use_container_width=True,
                                                hide_index=True,
                                            )
                                    st.markdown(f"**Adjusted Interior Total: ${adj_int_total:,.0f}**")

                                # ── Exterior line items (editable) ──
                                adj_ext_total = exterior_total
                                if ext_df is not None and not ext_df.empty:
                                    st.markdown("---")
                                    st.markdown(f"**🏗️ Exterior — Original: ${exterior_total:,.0f}**")
                                    edited_ext = st.data_editor(
                                        ext_df[["Line Item", "Orig Qty", "Orig Rate", "Adjusted Qty", "Adjusted Rate"]],
                                        column_config={
                                            "Line Item": st.column_config.TextColumn("Line Item", disabled=True, width="large"),
                                            "Orig Qty": st.column_config.NumberColumn("Orig Qty", disabled=True, format="%.0f"),
                                            "Orig Rate": st.column_config.NumberColumn("Orig Rate ($)", disabled=True, format="$%.2f"),
                                            "Adjusted Qty": st.column_config.NumberColumn("Adjusted Qty", format="%.0f", min_value=0),
                                            "Adjusted Rate": st.column_config.NumberColumn("Adjusted Rate ($)", format="$%.2f", min_value=0.0, step=0.01),
                                        },
                                        use_container_width=True,
                                        hide_index=True,
                                        key=f"ext_edit_{job_id}",
                                        num_rows="fixed",
                                    )
                                    if edited_ext is not None:
                                        _adj_cost = edited_ext["Adjusted Qty"] * edited_ext["Adjusted Rate"]
                                        _adj_markup = _adj_cost * ext_df["_markup_pct"]
                                        _adj_line_totals = (_adj_cost + _adj_markup).round(2)
                                        adj_ext_total = _adj_line_totals.sum()

                                        summary_ext = pd.DataFrame({
                                            "Line Item": edited_ext["Line Item"],
                                            "Adjusted Total": _adj_line_totals,
                                            "Orig Total": ext_df["Orig Total"],
                                            "Change": _adj_line_totals - ext_df["Orig Total"],
                                        })
                                        has_changes = (summary_ext["Change"].abs() > 0.01).any()
                                        if has_changes:
                                            changed = summary_ext[summary_ext["Change"].abs() > 0.01]
                                            st.dataframe(
                                                changed[["Line Item", "Orig Total", "Adjusted Total", "Change"]],
                                                column_config={
                                                    "Line Item": st.column_config.TextColumn("Line Item", width="large"),
                                                    "Orig Total": st.column_config.NumberColumn("Orig Total", format="$%.0f"),
                                                    "Adjusted Total": st.column_config.NumberColumn("Adjusted Total", format="$%.0f"),
                                                    "Change": st.column_config.NumberColumn("Change", format="$%+.0f"),
                                                },
                                                use_container_width=True,
                                                hide_index=True,
                                            )
                                    st.markdown(f"**Adjusted Exterior Total: ${adj_ext_total:,.0f}**")

                                # ── Populate the summary container at the top ──
                                adj_grand = adj_int_total + adj_ext_total
                                delta = adj_grand - total
                                has_adjustments = abs(delta) > 0.01
                                int_delta = adj_int_total - interior_total
                                ext_delta = adj_ext_total - exterior_total

                                with summary_container:
                                    # Row 1: Original values
                                    mc1, mc2, mc3 = st.columns(3)
                                    mc1.metric("Original Estimate", f"${total:,.0f}")
                                    mc2.metric("Interior (Orig)", f"${interior_total:,.0f}")
                                    mc3.metric("Exterior (Orig)", f"${exterior_total:,.0f}")

                                    # Row 2: Adjusted values (with deltas)
                                    ac1, ac2, ac3 = st.columns(3)
                                    ac1.metric("Adjusted Estimate", f"${adj_grand:,.0f}",
                                               delta=f"${delta:+,.0f}" if has_adjustments else None,
                                               delta_color="normal")
                                    ac2.metric("Interior (Adj)", f"${adj_int_total:,.0f}",
                                               delta=f"${int_delta:+,.0f}" if abs(int_delta) > 0.01 else None,
                                               delta_color="normal")
                                    ac3.metric("Exterior (Adj)", f"${adj_ext_total:,.0f}",
                                               delta=f"${ext_delta:+,.0f}" if abs(ext_delta) > 0.01 else None,
                                               delta_color="normal")

                                    # Row 3: Project info
                                    mc4, mc5, mc6 = st.columns(3)
                                    mc4.metric("Rooms Found", f"{rooms}")
                                    mc5.metric("Wall Area", f"{wall_sf:,.0f} SF")
                                    mc6.metric("Ceiling Area", f"{ceiling_sf:,.0f} SF")

                                    if has_adjustments:
                                        pct = (delta / total * 100) if total else 0
                                        st.markdown(f"**Overall Change: {pct:+.1f}%**")

                                rfi = analysis_data.get("rfi_items", [])
                                if rfi:
                                    st.markdown(f"**📋 RFI Items ({len(rfi)})**")
                                    for item in rfi:
                                        st.markdown(f"- {item.get('question', item) if isinstance(item, dict) else item}")
                        except Exception:
                            pass

                elif job.get("error"):
                    st.error(f"Error: {job['error']}")

                st.markdown("---")


# ═══════════════════════════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown(
    '<center><small class="footer-text">Knight Shift &nbsp;&mdash;&nbsp; Forged by Willpower &nbsp;|&nbsp; '
    'Powered by Claude &nbsp;|&nbsp; Confidential</small></center>',
    unsafe_allow_html=True,
)
