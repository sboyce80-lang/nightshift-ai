#!/usr/bin/env python3
"""
Nightshift AI — Streamlit App (Queue-Based)
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
import json
import uuid
import threading
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Ensure imports from project root ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ── Bridge Streamlit secrets → environment variables ──
# Streamlit Cloud stores secrets in st.secrets, not os.environ.
# config.py and Takeoff_DIRECT.py read from os.environ, so we bridge here.
try:
    if hasattr(st, "secrets"):
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
            if key in st.secrets and key not in os.environ:
                os.environ[key] = st.secrets[key]
except Exception:
    pass

# ── Page config (must be first st call) ──
st.set_page_config(
    page_title="Nightshift AI — Painting Takeoff",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

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


def _is_worker_running():
    """Check if the background worker is alive."""
    if not os.path.exists(WORKER_LOCK):
        return False
    try:
        with open(WORKER_LOCK, "r") as f:
            data = json.load(f)
        # If lock is older than 30 minutes, consider it stale
        lock_time = datetime.fromisoformat(data.get("started", "2000-01-01"))
        age_minutes = (datetime.now() - lock_time).total_seconds() / 60
        if age_minutes > 30:
            os.remove(WORKER_LOCK)
            return False
        return data.get("alive", False)
    except Exception:
        return False


def _process_single_job(job_id):
    """Process one job — called by the worker thread."""
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

    try:
        from Takeoff_DIRECT import run_analysis
        result = run_analysis(
            pdf_paths=meta.get("pdf_paths", []),
            contact_name=meta.get("contact_name", ""),
            contact_email=meta.get("contact_email", ""),
            scope_notes=meta.get("scope_notes", ""),
            image_fallback=meta.get("image_fallback", True),
            multi_pass=meta.get("multi_pass", False),
        )

        # Check if cancelled during processing
        if _is_cancelled(job_id):
            meta["status"] = "cancelled"
            meta["finished"] = datetime.now().isoformat()
            meta["error"] = "Cancelled by user during processing"
            _clear_cancel_flag(job_id)
        else:
            meta["status"] = "done"
            meta["finished"] = datetime.now().isoformat()
            meta["output_json"] = result.get("output_json_path", "")
            meta["output_pdf"] = result.get("output_pdf_path", "")
    except Exception as e:
        if _is_cancelled(job_id):
            meta["status"] = "cancelled"
            meta["error"] = "Cancelled by user"
            _clear_cancel_flag(job_id)
        else:
            meta["status"] = "error"
            meta["error"] = str(e)
        meta["finished"] = datetime.now().isoformat()

    _write_job_meta(job_id, meta)
    _dequeue_job(job_id)


def _worker_loop():
    """Background worker — processes queue one job at a time."""
    # Write lock
    with open(WORKER_LOCK, "w") as f:
        json.dump({"alive": True, "started": datetime.now().isoformat()}, f)

    try:
        while True:
            queue = _get_queue()
            if not queue:
                # No more jobs — worker exits
                break

            job_id = queue[0]  # Process oldest first
            _process_single_job(job_id)

            # Brief pause between jobs
            time.sleep(2)
    finally:
        # Clean up lock
        if os.path.exists(WORKER_LOCK):
            os.remove(WORKER_LOCK)


def _ensure_worker():
    """Start the worker thread if not already running."""
    if not _is_worker_running() and _get_queue():
        thread = threading.Thread(target=_worker_loop, daemon=True)
        thread.start()


# ═══════════════════════════════════════════════════════════════════════════════
# STYLING
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
    .main-header {
        background: linear-gradient(135deg, #1a3a5c 0%, #2c6fbb 100%);
        padding: 2rem;
        border-radius: 12px;
        margin-bottom: 1.5rem;
        color: white;
    }
    .main-header h1 { color: white; margin: 0; font-size: 2rem; }
    .main-header p { color: #c8ddf0; margin: 0.25rem 0 0 0; font-size: 1rem; }
    .status-card {
        padding: 1rem 1.5rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        border-left: 4px solid;
    }
    .status-running { background: #fff8e6; border-color: #f0a500; }
    .status-queued { background: #e8f0fa; border-color: #2c6fbb; }
    .status-done { background: #e8f8e8; border-color: #1a7a3a; }
    .status-error { background: #fde8e8; border-color: #c0392b; }
    .metric-box {
        background: #f0f4f8;
        border-radius: 8px;
        padding: 1rem;
        text-align: center;
    }
    .metric-box h3 { margin: 0; color: #1a3a5c; font-size: 1.8rem; }
    .metric-box p { margin: 0; color: #666; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<div class="main-header">
    <h1>🎨 Nightshift AI</h1>
    <p>Automated Construction Painting Estimates from Architectural PDFs</p>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR — JOB SUBMISSION
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("📋 New Estimate")

    contact_name = st.text_input("Contact Name *", placeholder="e.g., John Smith")
    contact_email = st.text_input("Contact Email *", placeholder="e.g., john@rider.com")

    st.markdown("---")
    uploaded_files = st.file_uploader(
        "Upload Construction PDFs *",
        type=["pdf"],
        accept_multiple_files=True,
        help="Architectural drawings — floor plans, elevations, schedules. Max 200MB each.",
    )

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

    st.markdown("---")

    # Queue status in sidebar
    current_queue = _get_queue()
    if current_queue:
        st.warning(f"📊 Queue: {len(current_queue)} job(s) waiting")
    else:
        st.success("✅ Queue empty — jobs start immediately")

    # Validation
    can_submit = bool(contact_name and contact_email and uploaded_files)

    if st.button("🚀 Generate Estimate", type="primary", use_container_width=True, disabled=not can_submit):
        # ── Save uploaded PDFs ──
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        pdf_paths = []
        for f in uploaded_files:
            fpath = os.path.join(job_dir, f.name)
            with open(fpath, "wb") as out:
                out.write(f.getbuffer())
            pdf_paths.append(fpath)

        # ── Save job metadata (queued status) ──
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
        _write_job_meta(job_id, meta)

        # ── Add to queue ──
        _enqueue_job(job_id)

        # ── Start worker if needed ──
        _ensure_worker()

        queue_pos = _get_queue_position(job_id)
        if queue_pos <= 1:
            st.success(f"✅ Job **{job_id}** submitted! Processing now...")
        else:
            st.info(f"📋 Job **{job_id}** queued — position **#{queue_pos}**. It will start automatically when the current job finishes.")
        # Paintbrush celebration animation
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


# ═══════════════════════════════════════════════════════════════════════════════
# ENSURE WORKER IS RUNNING (on every page load)
# ═══════════════════════════════════════════════════════════════════════════════
_ensure_worker()


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
tab_active, tab_history = st.tabs(["📊 Active & Queued", "📁 Completed"])

with tab_active:
    active_jobs = {k: v for k, v in all_jobs.items() if v.get("status") in ("running", "queued")}

    if not active_jobs:
        st.info("No active jobs. Upload PDFs in the sidebar to start a new estimate.")
    else:
        for job_id, job in active_jobs.items():
            status = job.get("status", "unknown")
            queue_pos = _get_queue_position(job_id)
            pdf_names = [os.path.basename(p) for p in job.get("pdf_paths", [])]
            queue = _get_queue()

            if status == "running":
                st.markdown(f"""
                <div class="status-card status-running">
                    <strong>⏳ Processing:</strong> {job_id}<br/>
                    <small>Contact: {job.get('contact_name', '')} | Files: {', '.join(pdf_names)} | Started: {job.get('run_started', job.get('submitted', ''))}</small>
                </div>
                """, unsafe_allow_html=True)

                # Stop button for running jobs
                if st.button("🛑 Stop Job", key=f"stop_{job_id}", type="secondary"):
                    _cancel_job(job_id)
                    st.warning(f"Cancellation requested for **{job_id}**. It will stop after the current extraction step.")
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
                            total = costs.get("total_cost", 0)
                            agg = analysis_data.get("analysis", {}).get("aggregated_totals", {})
                            rooms = agg.get("total_rooms_found", 0)
                            wall_sf = agg.get("total_wall_sqft", 0)

                            with st.expander(f"💰 Estimate Summary — ${total:,.0f}", expanded=False):
                                mc1, mc2, mc3, mc4 = st.columns(4)
                                mc1.metric("Total Estimate", f"${total:,.0f}")
                                mc2.metric("Rooms Found", f"{rooms}")
                                mc3.metric("Wall Area", f"{wall_sf:,.0f} SF")
                                mc4.metric("Line Items", f"{len(line_items)}")

                                st.markdown("---")
                                if line_items:
                                    table_data = []
                                    for li in line_items:
                                        if li.get("total_cost", 0) > 0:
                                            table_data.append({
                                                "Line Item": li.get("description", li.get("item", "")),
                                                "Quantity": f"{li.get('quantity', 0):,.0f}",
                                                "Unit": li.get("unit", ""),
                                                "Rate": f"${li.get('unit_rate', 0):,.2f}",
                                                "Total": f"${li.get('total_cost', 0):,.2f}",
                                            })
                                    if table_data:
                                        st.dataframe(table_data, use_container_width=True, hide_index=True)

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
    "<center><small>Nightshift AI — Automated Painting Estimates &nbsp;|&nbsp; "
    "Powered by Claude &nbsp;|&nbsp; Confidential</small></center>",
    unsafe_allow_html=True,
)
