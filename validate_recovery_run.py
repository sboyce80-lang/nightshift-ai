#!/usr/bin/env python3
"""Live validation runner for the confidence/room-recovery branch.

Runs the SAME engine path prod uses (run_analysis, multi_pass=True) with the
new merge/footprint flags ON, so we can confirm:
  - Tier-1 Fishkill does not regress (compare to regression_test references)
  - Wingstop Eastern recovers rooms (was 52->12 collapse) without overshoot

Flags are set process-wide here so the run exercises the opt-in behavior.
Outputs land next to this script as <label>.json / <label>.pdf.
"""
import os, sys, json, shutil, traceback
from datetime import datetime, timezone

# Exercise the opt-in recovery behavior end-to-end.
os.environ["NIGHTSHIFT_MERGE_PREFER_COMPLETE"] = "1"
os.environ["NIGHTSHIFT_MERGE_UNION"] = "1"
os.environ["NIGHTSHIFT_FOOTPRINT_RFI"] = "1"
# Stage 4 (confidence decouple) is already default-on.

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from Takeoff_DIRECT import run_analysis

JOBS = [
    {"label": "Fishkill_validate",
     "pdf": os.path.join(HERE, "spike_samples", "397Fishkill.pdf"),
     "name": "Rider Painting", "email": "elliott@riderpaintingny.com"},
    {"label": "Eastern_validate",
     "pdf": os.path.join(HERE, "wingstop_local_run",
                         "Approved-WNG-Las_Vegas__NV_GL_AB309-Combined_Permit_Set_2025-08-21.pdf"),
     "name": "steven villalta", "email": "steve@pmmlv.com"},
]


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    summary = []
    for j in JOBS:
        log(f"===== START {j['label']} =====")
        if not os.path.exists(j["pdf"]):
            log(f"  SKIP — pdf missing: {j['pdf']}")
            continue
        try:
            result = run_analysis(
                [j["pdf"]], contact_name=j["name"], contact_email=j["email"],
                scope_notes="", rate_overrides=None, multi_pass=True,
            )
            pi = (result.get("analysis", {}) or {}).get("project_info", {}) or {}
            rooms = pi.get("total_rooms_found")
            fp = pi.get("footprint_sqft")
            sub = (result.get("cost_estimate", {}) or {}).get("subtotal", 0) or 0
            # data_quality_score lives top-level in the SAVED json, not in
            # the run_analysis return dict — read it from the output file.
            dqs = (result.get("validation", {}) or {}).get("data_quality_score")
            if dqs is None and result.get("output_json_path"):
                try:
                    with open(result["output_json_path"]) as _fh:
                        dqs = (json.load(_fh).get("validation") or {}).get(
                            "data_quality_score")
                except Exception:
                    pass
            mr = bool(result.get("manual_review_required"))
            rfi = len(result.get("rfi_items") or [])
            jp = result.get("output_json_path")
            pp = result.get("output_pdf_path")
            saved = {}
            for kind, src in (("json", jp), ("pdf", pp)):
                if src and os.path.exists(src):
                    dst = os.path.join(HERE, f"{j['label']}.{kind}")
                    shutil.copy(src, dst)
                    saved[kind] = dst
            row = {"label": j["label"], "rooms": rooms, "footprint": fp,
                   "subtotal": sub, "dqs": dqs, "manual_review": mr,
                   "rfi": rfi, "outputs": saved}
            log(f"DONE {j['label']}: rooms={rooms} fp={fp} sub=${sub:,.0f} "
                f"dqs={dqs} mr={mr} rfi={rfi}")
            summary.append(row)
        except Exception as e:
            log(f"FAILED {j['label']}: {e!r}")
            traceback.print_exc()
            summary.append({"label": j["label"], "error": repr(e)})
    with open(os.path.join(HERE, "validate_recovery_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    log(f"SUMMARY written: {summary}")


if __name__ == "__main__":
    main()
