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
    page_icon="🖌️",
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


_WORKER_THREAD = None  # Track actual thread object, not just a lock file

def _is_worker_running():
    """Check if the background worker is alive using the actual thread reference."""
    global _WORKER_THREAD
    # Primary check: is the thread object alive in THIS process?
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return True
    # Thread is dead or was never started in this process instance.
    # Clean up any stale lock file left by a previous instance/reboot.
    if os.path.exists(WORKER_LOCK):
        os.remove(WORKER_LOCK)
    _WORKER_THREAD = None
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

    # Set progress file path so Takeoff_DIRECT can write updates
    progress_path = os.path.join(JOBS_DIR, f".progress_{job_id}.json")
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = progress_path

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
    _clear_progress(job_id)
    # Clean up env var
    os.environ.pop("NIGHTSHIFT_PROGRESS_FILE", None)


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
    global _WORKER_THREAD
    if not _is_worker_running() and _get_queue():
        _WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True)
        _WORKER_THREAD.start()


# ═══════════════════════════════════════════════════════════════════════════════
# FUTURISTIC PAINTBRUSH LOGO (inline SVG)
# ═══════════════════════════════════════════════════════════════════════════════
LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80" width="56" height="56">
  <defs>
    <linearGradient id="brush_glow" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#00d4ff"/>
      <stop offset="100%" stop-color="#0066cc"/>
    </linearGradient>
    <linearGradient id="bristle_grad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#00d4ff" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="#0044aa" stop-opacity="0.6"/>
    </linearGradient>
    <filter id="glow">
      <feGaussianBlur stdDeviation="2" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
  </defs>
  <!-- Handle -->
  <rect x="52" y="4" width="8" height="34" rx="3" fill="url(#brush_glow)" transform="rotate(25 56 21)" filter="url(#glow)"/>
  <!-- Ferrule (metal band) -->
  <rect x="44" y="34" width="14" height="6" rx="1.5" fill="#c0d8f0" transform="rotate(25 51 37)" opacity="0.8"/>
  <!-- Bristles -->
  <path d="M28 52 L38 38 L52 42 L42 58 Q36 66 28 60 Z" fill="url(#bristle_grad)" filter="url(#glow)"/>
  <!-- Paint trail / swoosh -->
  <path d="M24 58 Q14 64 10 56 Q6 48 16 44 Q22 40 26 48 Z" fill="#00d4ff" opacity="0.5"/>
  <path d="M12 50 Q4 54 8 62 Q10 66 18 64" stroke="#00d4ff" stroke-width="1.5" fill="none" opacity="0.3"/>
  <!-- Glow dots -->
  <circle cx="14" cy="54" r="1.5" fill="#00d4ff" opacity="0.7"/>
  <circle cx="20" cy="62" r="1" fill="#00d4ff" opacity="0.5"/>
  <circle cx="8" cy="60" r="1" fill="#00d4ff" opacity="0.4"/>
</svg>
"""


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
    .main-header .header-text h1 span.accent { color: #00d4ff; }
    .main-header .header-text p {
        color: #7a9cc6;
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
        <h1><span class="accent">Nightshift</span> AI</h1>
        <p>Automated Construction Painting Estimates from Architectural PDFs</p>
    </div>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR — JOB SUBMISSION
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown(f'<div style="text-align:center;margin-bottom:0.5rem;">{LOGO_SVG}</div>', unsafe_allow_html=True)
    st.header("New Estimate")

    contact_name = st.text_input("Contact Name *", placeholder="e.g., John Smith")
    contact_email = st.text_input("Contact Email *", placeholder="e.g., john@rider.com")

    st.markdown("---")

    upload_mode = st.radio(
        "Upload Method",
        ["Individual PDFs", "ZIP Folder"],
        horizontal=True,
        help="Upload PDFs one at a time, or a ZIP containing a folder of PDFs.",
    )

    uploaded_files = []
    if upload_mode == "Individual PDFs":
        raw_uploads = st.file_uploader(
            "Upload Construction PDFs *",
            type=["pdf"],
            accept_multiple_files=True,
            help="Architectural drawings — floor plans, elevations, schedules. Max 200MB each.",
        )
        if raw_uploads:
            uploaded_files = raw_uploads
    else:
        zip_upload = st.file_uploader(
            "Upload ZIP folder containing PDFs *",
            type=["zip"],
            accept_multiple_files=False,
            help="A .zip file containing one or more PDF drawings. Subfolders are included.",
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
                    # Wrap extracted files as file-like objects with .name and .getbuffer()
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
                                # Top-level metrics
                                mc1, mc2, mc3 = st.columns(3)
                                mc1.metric("Total Estimate", f"${total:,.0f}")
                                mc2.metric("Interior", f"${interior_total:,.0f}")
                                mc3.metric("Exterior", f"${exterior_total:,.0f}")

                                mc4, mc5, mc6 = st.columns(3)
                                mc4.metric("Rooms Found", f"{rooms}")
                                mc5.metric("Wall Area", f"{wall_sf:,.0f} SF")
                                mc6.metric("Ceiling Area", f"{ceiling_sf:,.0f} SF")

                                # Interior line items
                                st.markdown("---")
                                st.markdown(f"**🏠 Interior — ${interior_total:,.0f}**")
                                if interior_items:
                                    int_data = []
                                    for li in interior_items:
                                        li_total = li.get("total", 0) or li.get("total_cost", 0) or 0
                                        if li_total > 0:
                                            int_data.append({
                                                "Line Item": li.get("item", "") or li.get("description", ""),
                                                "Qty": f"{li.get('qty', 0) or li.get('quantity', 0):,.0f}",
                                                "Cost": f"${li.get('cost', 0):,.0f}",
                                                "Total": f"${li_total:,.0f}",
                                            })
                                    if int_data:
                                        st.dataframe(int_data, use_container_width=True, hide_index=True)

                                # Exterior line items
                                if exterior_items:
                                    st.markdown(f"**🏗️ Exterior — ${exterior_total:,.0f}**")
                                    ext_data = []
                                    for li in exterior_items:
                                        li_total = li.get("total", 0) or li.get("total_cost", 0) or 0
                                        if li_total > 0:
                                            ext_data.append({
                                                "Line Item": li.get("item", "") or li.get("description", ""),
                                                "Qty": f"{li.get('qty', 0) or li.get('quantity', 0):,.0f}",
                                                "Cost": f"${li.get('cost', 0):,.0f}",
                                                "Total": f"${li_total:,.0f}",
                                            })
                                    if ext_data:
                                        st.dataframe(ext_data, use_container_width=True, hide_index=True)

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
    '<center><small class="footer-text">Nightshift AI &nbsp;&mdash;&nbsp; Automated Painting Estimates &nbsp;|&nbsp; '
    'Powered by Claude &nbsp;|&nbsp; Confidential</small></center>',
    unsafe_allow_html=True,
)
