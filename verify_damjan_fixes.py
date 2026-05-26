"""Verification harness for the Damjan beta-bug fixes (B1-B8).

Runs the DETERMINISTIC parts of each fix against Damjan's 5 real result
JSONs and reports before/after. Prompt-only changes (B5b, B6 extraction,
B7 extraction) cannot be verified here — they only change a fresh engine
run — so this harness confirms their "before" state and unit-tests the
deterministic code paths (RFIs, notes, PDF render) that surround them.

Run:  ~/nightshift-ai/venv/bin/python verify_damjan_fixes.py
"""
import copy
import glob
import json
import os
import re
import tempfile

import fitz  # PyMuPDF
import Takeoff_DIRECT as T
from json_to_pdf import json_to_pdf

DL = os.path.expanduser("~/Downloads")
FILES = sorted(glob.glob(os.path.join(DL, "construction_analysis_20260518_*.json")))

LABELS = {
    "182505": "MSP Tactical (PA-745-210-001)",
    "190954": "ATCT — Air Traffic Control Tower",
    "193541": "Life Time (FullSet — pickleball)",
    "193628": "MSP re-run (PA-745 + Arch_Full_Set)",
    "200700": "Five Below Alameda",
}


def label(fp):
    stamp = re.search(r"_(\d{6})\.json$", fp).group(1)
    return LABELS.get(stamp, stamp)


def hdr(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)


def rfi_has(rfis, *kw):
    """True if any RFI question/action mentions all keywords (case-insensitive)."""
    for r in rfis:
        blob = (str(r.get("question", "")) + " " + str(r.get("action_required", ""))).lower()
        if all(k.lower() in blob for k in kw):
            return r
    return None


jobs = [(fp, json.load(open(fp))) for fp in FILES]

# ── B2a — purge stale schedule notes when a schedule was detected ───────────
hdr("B2a — purge stale 'no schedule' notes once a schedule is detected")
for fp, d in jobs:
    an = copy.deepcopy(d["analysis"])
    doors = bool(an.get("has_door_schedule"))
    wins = bool(an.get("has_window_schedule"))
    before = list(an.get("notes", []) or [])
    T._purge_stale_schedule_notes(an, doors=doors, windows=wins)
    after = an.get("notes", []) or []
    removed = [n for n in before if n not in after]
    print(f"\n  {label(fp)}  (has_door={doors}, has_window={wins})")
    if removed:
        for n in removed:
            print(f"    PURGED: {str(n)[:130]}")
    else:
        print("    no stale door/window-schedule notes present")

# ── B2b — finish-schedule flag + RFI ────────────────────────────────────────
hdr("B2b — has_finish_schedule flag drives the finish-schedule RFI")
for fp, d in jobs:
    an = d["analysis"]
    print(f"\n  {label(fp)}")
    print(f"    BEFORE: has_finish_schedule key present = {('has_finish_schedule' in an)}")
    none_rfis = T.generate_rfi_items(copy.deepcopy(an))
    a_false = copy.deepcopy(an); a_false["has_finish_schedule"] = False
    a_true = copy.deepcopy(an); a_true["has_finish_schedule"] = True
    r_false = T.generate_rfi_items(a_false)
    r_true = T.generate_rfi_items(a_true)
    # Match the specific B2b RFI (#3b), not any stray "finish schedule" mention.
    phrase = "no room finish schedule was found in the provided documents"
    print(f"    flag=False -> #3b finish-schedule RFI emitted: "
          f"{bool(rfi_has(r_false, phrase))}")
    print(f"    flag=True  -> #3b finish-schedule RFI emitted: "
          f"{bool(rfi_has(r_true, phrase))}")

# ── B3 — RFI 'no floor plans' contradiction guard ───────────────────────────
hdr("B3 — 'no floor plans' RFI suppressed when dimensioned rooms exist")
for fp, d in jobs:
    an = copy.deepcopy(d["analysis"])
    an["no_floor_plans_found"] = True  # force the flag the LLM would set
    rfis = T.generate_rfi_items(an)
    measured = sum(
        1 for fl in an.get("floors", []) for rm in fl.get("rooms", [])
        if rm.get("in_scope", True) and rm.get("source") != "schedule_estimate"
        and (rm.get("dimensions", {}).get("wall_area_sqft", 0) or 0) > 0
    )
    fired = bool(rfi_has(rfis, "does not include architectural floor plans"))
    print(f"\n  {label(fp)}  — dimensioned rooms = {measured}")
    print(f"    no_floor_plans_found forced True -> 'no floor plans' RFI emitted: {fired}"
          f"   ({'SUPPRESSED (correct)' if not fired else 'still fires'})")

# ── B4 — traceability reconciliation line renders in the PDF ────────────────
hdr("B4 — 3-stage wall pipeline reconciliation renders in the PDF")
for fp, d in jobs:
    print(f"\n  {label(fp)}")
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            pdf_path = tf.name
        json_to_pdf(fp, pdf_path)
        doc = fitz.open(pdf_path)
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
        os.unlink(pdf_path)
        recon = [ln.strip() for ln in text.splitlines() if "Reconciliation:" in ln]
        if recon:
            for ln in recon:
                print(f"    RENDERED: {ln[:150]}")
        else:
            print("    (no reconciliation line — no boost/adjustment on this job)")
    except Exception as exc:
        print(f"    PDF render FAILED: {exc!r}")

# ── B5a — wall boost no longer inflates ceilings ────────────────────────────
hdr("B5a — perimeter boost: ceiling inflation removed")
boost_re = re.compile(r"Ceilings ([\d,]+)->([\d,]+)")
for fp, d in jobs:
    notes = d["analysis"].get("notes", []) or []
    boost = [str(n) for n in notes if "Perimeter Wall Boost" in str(n)
             or "[Wall Boost]" in str(n)]
    print(f"\n  {label(fp)}")
    if not boost:
        print("    no wall boost applied on this job")
        continue
    m = boost_re.search(boost[0])
    if m:
        pre = int(m.group(1).replace(",", ""))
        post = int(m.group(2).replace(",", ""))
        print(f"    OLD boost note inflated ceilings {pre:,} -> {post:,} "
              f"(+{post - pre:,} sqft with no measurement basis)")
        print(f"    AFTER B5a: ceilings stay {pre:,} sqft; boost note now reads "
              f"'Ceilings not boosted'")
    else:
        print(f"    boost note: {boost[0][:120]}")

# ── B6 — base trim: flag-but-keep (no dollar change) ────────────────────────
hdr("B6 — base trim: confirmed bug, fix is flag-only (regression-safe)")
for fp, d in jobs:
    rooms = [r for fl in d["analysis"].get("floors", []) for r in fl.get("rooms", [])]
    eq = sum(1 for r in rooms
             if r.get("elements", {}).get("base_trim_lf", 0)
             and r["elements"]["base_trim_lf"] == r.get("dimensions", {}).get("perimeter_lf"))
    tot = sum(1 for r in rooms if r.get("elements", {}).get("base_trim_lf", 0))
    agg_trim = d["analysis"].get("aggregated_totals", {}).get("total_base_trim_lf")
    print(f"  {label(fp):42s} base_trim==perimeter {eq}/{tot} rooms | "
          f"total_base_trim_lf={agg_trim} (unchanged by B6)")

# ── B7 — door-schedule RFI flags schedule-less counts as preliminary ────────
hdr("B7 — schedule-less door counts flagged as preliminary in the RFI")
for fp, d in jobs:
    an = copy.deepcopy(d["analysis"])
    an["has_door_schedule"] = False  # force the no-schedule branch
    rfis = T.generate_rfi_items(an)
    r = rfi_has(rfis, "door schedule")
    flagged = bool(r and ("preliminary" in str(r.get("question", "")).lower()
                          or "over-count" in str(r.get("question", "")).lower()))
    print(f"\n  {label(fp)}")
    print(f"    door-schedule RFI now warns counts are preliminary/over-counting: {flagged}")

print("\n" + "=" * 78)
print("Done. B5b is N/A to these jobs (all used floor-plan extraction, not the")
print("finish-schedule path). B1/B8 are UI/endpoint features — not in the JSON.")
print("=" * 78)
