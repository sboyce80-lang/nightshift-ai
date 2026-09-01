#!/usr/bin/env python3
"""Run ONE (job, config) cross-class regression cell in a subprocess.

Usage: run_child.py <job_key> <config: baseline|jwflags>

Config `baseline` mirrors the Render prod env group (same set run_one.py used
for the 8/20 JW batch). Config `jwflags` = baseline + the validated 28-flag
JW-class set parsed live from nsai_batch_2026-08-20/rerun_batch.sh (single
source of truth — no transcription drift).

Writes <here>/results/<job>_<config>.json with scored quantity errors vs
regression_test.REFERENCE_CASES targets.
"""
import os, sys, json, re, time, shutil, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

BASELINE_FLAGS = {
    # Prod-equivalent (Render env group as of 2026-08-11, per run_one.py).
    # MARKUP_TAKEOFF stays OFF: golden targets come from Rider takeoffs, so a
    # markup read would be circular if any annotations survive in the samples.
    "NIGHTSHIFT_MARKUP_TAKEOFF": "0",
    "NIGHTSHIFT_VME_PRIMARY": "1",
    "NIGHTSHIFT_VME_AUTHORITATIVE_WALLS": "1",
    "NIGHTSHIFT_VECTOR_MEASURE": "1",
    "NIGHTSHIFT_PER_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_PROVENANCE_GATE": "1",
    "NIGHTSHIFT_STAIR_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_CLOSET_RECOVERY": "1",
}

JOBS = {
    "fishkill_397":       os.path.join(ROOT, "spike_samples", "397Fishkill.pdf"),
    "364_main":           os.path.join(ROOT, "spike_samples", "364Main.pdf"),
    "dutchess_livestock": os.path.join(ROOT, "golden", "plans",
                                       "Dutchess_Livestock_Bidding_Documents.pdf"),
    "tsc_fusion_highland": os.path.join(ROOT, "golden", "plans",
                                        "TSC_Fusion_Highland_Rev2.pdf"),
    "honey_farms_malta":  os.path.join(ROOT, "golden", "plans",
                                       "Honey_Farms_Malta_100pct_Pricing_Set.pdf"),
}

EXCL = {"cost_estimate_subtotal", "footprint_sqft"}


def jw_flag_set():
    flags = {}
    sh = os.path.join(ROOT, "nsai_batch_2026-08-20", "rerun_batch.sh")
    for line in open(sh):
        m = re.match(r"export\s+([A-Z0-9_]+)=(\S+)", line.strip())
        if m and m.group(1) != "NIGHTSHIFT_MARKUP_TAKEOFF":
            flags[m.group(1)] = m.group(2)
    return flags


def main():
    job_key, config = sys.argv[1], sys.argv[2]
    pdf = JOBS[job_key]
    flags = dict(BASELINE_FLAGS)
    if config == "jwflags":
        flags.update(jw_flag_set())
    for k, v in flags.items():
        os.environ[k] = v

    out_dir = os.path.join(HERE, "results")
    os.makedirs(out_dir, exist_ok=True)
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(
        out_dir, f"{job_key}_{config}.progress.txt")

    meta = {"job": job_key, "config": config, "flags": flags,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        import regression_test as rt
        from Takeoff_DIRECT import run_analysis
        result = run_analysis(
            [pdf],
            contact_name="CrossClass Regression",
            contact_email="sboyce80+crossclass@gmail.com",
            scope_notes="", rate_overrides=None,
            multi_pass=True,
        )
        # NOTE: .result.json suffix — a bare .json here collides with the
        # meta file written at the end and loses the full result.
        for kind, src in (("result.json", result.get("output_json_path")),
                          ("result.pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(out_dir, f"{job_key}_{config}.{kind}"))

        data = {"analysis": result.get("analysis", {}),
                "cost_estimate": result.get("cost_estimate", {})}
        m = rt.extract_metrics(data)
        rows = []
        for k, sp in (rt.REFERENCE_CASES[job_key].get("targets") or {}).items():
            if k in EXCL:
                continue
            t = sp[0] if isinstance(sp, (list, tuple)) else sp
            a = m.get(k)
            if a is None or not t:
                continue
            rows.append({"metric": k, "actual": float(a), "target": float(t),
                         "err_pct": abs(float(a) - float(t)) / float(t) * 100})
        ce = result.get("cost_estimate", {}) or {}
        meta.update({
            "ok": True,
            "mean_abs_err_pct": (sum(r["err_pct"] for r in rows) / len(rows)
                                 if rows else None),
            "rows": rows,
            "subtotal": ce.get("subtotal", 0),
            "manual_review": bool(result.get("manual_review_required")),
            "elapsed_s": round(time.time() - t0, 1),
        })
    except Exception as e:
        meta.update({"ok": False, "error": repr(e),
                     "traceback": traceback.format_exc()[-4000:],
                     "elapsed_s": round(time.time() - t0, 1)})
    with open(os.path.join(out_dir, f"{job_key}_{config}.json"), "w") as f:
        json.dump(meta, f, indent=2)
    sys.exit(0 if meta.get("ok") else 1)


if __name__ == "__main__":
    main()
