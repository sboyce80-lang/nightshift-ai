#!/usr/bin/env python3
"""
Nightshift AI — Streamlit App
==============================
Upload construction PDFs → get automated painting estimates.

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
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Ensure imports from project root ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

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
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

# ── Session state init ──
if "jobs" not in st.session_state:
    st.session_state.jobs = {}  # {job_id: {status, result, error, ...}}


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

        # ── Register job ──
        st.session_state.jobs[job_id] = {
            "status": "running",
            "contact_name": contact_name,
            "contact_email": contact_email,
            "pdf_names": [f.name for f in uploaded_files],
            "started": datetime.now().isoformat(),
            "result": None,
            "error": None,
            "log_lines": [],
        }

        # ── Save job metadata ──
        meta_path = os.path.join(JOBS_DIR, f"{job_id}.json")
        with open(meta_path, "w") as mf:
            json.dump({
                "job_id": job_id,
                "contact_name": contact_name,
                "contact_email": contact_email,
                "scope_notes": scope_notes,
                "pdf_paths": pdf_paths,
                "started": datetime.now().isoformat(),
                "status": "running",
            }, mf, indent=2)

        # ── Run analysis in background thread ──
        def _run_job(jid, paths, name, email, scope, img_fb, mp):
            try:
                from Takeoff_DIRECT import run_analysis
                result = run_analysis(
                    pdf_paths=paths,
                    contact_name=name,
                    contact_email=email,
                    scope_notes=scope,
                    image_fallback=img_fb,
                    multi_pass=mp,
                )
                st.session_state.jobs[jid]["status"] = "done"
                st.session_state.jobs[jid]["result"] = result
                st.session_state.jobs[jid]["finished"] = datetime.now().isoformat()

                # Update job file
                meta = os.path.join(JOBS_DIR, f"{jid}.json")
                with open(meta, "r") as f:
                    data = json.load(f)
                data["status"] = "done"
                data["finished"] = datetime.now().isoformat()
                data["output_json"] = result.get("output_json_path", "")
                data["output_pdf"] = result.get("output_pdf_path", "")
                with open(meta, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                st.session_state.jobs[jid]["status"] = "error"
                st.session_state.jobs[jid]["error"] = str(e)
                st.session_state.jobs[jid]["finished"] = datetime.now().isoformat()

        thread = threading.Thread(
            target=_run_job,
            args=(job_id, pdf_paths, contact_name, contact_email,
                  scope_notes, image_fallback, multi_pass),
            daemon=True,
        )
        thread.start()

        st.success(f"✅ Job **{job_id}** submitted! Refresh page to check status.")
        st.balloons()

    if not can_submit and uploaded_files:
        st.warning("Please fill in Contact Name and Email.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN AREA — JOB DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

# ── Load historical jobs from disk ──
def _load_jobs_from_disk():
    """Load completed job metadata from jobs/ directory."""
    disk_jobs = {}
    for jf in sorted(Path(JOBS_DIR).glob("*.json"), reverse=True):
        try:
            with open(jf) as f:
                data = json.load(f)
            jid = data.get("job_id", jf.stem)
            if jid not in st.session_state.jobs:
                disk_jobs[jid] = {
                    "status": data.get("status", "unknown"),
                    "contact_name": data.get("contact_name", ""),
                    "contact_email": data.get("contact_email", ""),
                    "pdf_names": [os.path.basename(p) for p in data.get("pdf_paths", [])],
                    "started": data.get("started", ""),
                    "finished": data.get("finished", ""),
                    "result": {
                        "output_json_path": data.get("output_json", ""),
                        "output_pdf_path": data.get("output_pdf", ""),
                    } if data.get("output_json") else None,
                    "error": data.get("error"),
                }
        except Exception:
            pass
    return disk_jobs

disk_jobs = _load_jobs_from_disk()
all_jobs = {**disk_jobs, **st.session_state.jobs}  # session takes priority

# ── Tabs ──
tab_active, tab_history = st.tabs(["📊 Active Jobs", "📁 Job History"])

with tab_active:
    running_jobs = {k: v for k, v in all_jobs.items() if v["status"] == "running"}

    if not running_jobs:
        st.info("No active jobs. Upload PDFs in the sidebar to start a new estimate.")
    else:
        for job_id, job in running_jobs.items():
            with st.container():
                st.markdown(f"""
                <div class="status-card status-running">
                    <strong>⏳ Running:</strong> {job_id}<br/>
                    <small>Contact: {job['contact_name']} | Files: {', '.join(job['pdf_names'])} | Started: {job['started']}</small>
                </div>
                """, unsafe_allow_html=True)

            # Auto-refresh for running jobs
            st.markdown(
                '<meta http-equiv="refresh" content="15">',
                unsafe_allow_html=True,
            )

with tab_history:
    completed_jobs = {k: v for k, v in all_jobs.items() if v["status"] in ("done", "error")}

    if not completed_jobs:
        st.info("No completed jobs yet.")
    else:
        for job_id, job in sorted(completed_jobs.items(), key=lambda x: x[0], reverse=True):
            is_done = job["status"] == "done"
            icon = "✅" if is_done else "❌"
            css_class = "status-done" if is_done else "status-error"

            with st.container():
                st.markdown(f"""
                <div class="status-card {css_class}">
                    <strong>{icon} {job_id}</strong><br/>
                    <small>Contact: {job['contact_name']} ({job['contact_email']}) | Files: {', '.join(job.get('pdf_names', []))}</small>
                </div>
                """, unsafe_allow_html=True)

                if is_done and job.get("result"):
                    result = job["result"]

                    col1, col2 = st.columns(2)

                    # ── Download buttons ──
                    pdf_path = result.get("output_pdf_path", "")
                    json_path = result.get("output_json_path", "")

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

                    # ── Show cost summary if JSON available ──
                    if json_path and os.path.exists(json_path):
                        try:
                            with open(json_path) as jf:
                                analysis_data = json.load(jf)

                            costs = analysis_data.get("cost_estimate", {})
                            line_items = costs.get("line_items", [])
                            total = costs.get("total_cost", 0)
                            rooms = analysis_data.get("analysis", {}).get("aggregated_totals", {}).get("total_rooms_found", 0)
                            wall_sf = analysis_data.get("analysis", {}).get("aggregated_totals", {}).get("total_wall_sqft", 0)

                            with st.expander(f"💰 Estimate Summary — ${total:,.0f}", expanded=False):
                                mc1, mc2, mc3, mc4 = st.columns(4)
                                mc1.metric("Total Estimate", f"${total:,.0f}")
                                mc2.metric("Rooms Found", f"{rooms}")
                                mc3.metric("Wall Area", f"{wall_sf:,.0f} SF")
                                mc4.metric("Line Items", f"{len(line_items)}")

                                st.markdown("---")
                                # Line item table
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

                                # RFI items
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
