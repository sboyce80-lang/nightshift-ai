#!/usr/bin/env python3
"""Calibrated confidence (Phase 2.4).

Three "confidence" numbers existed before this, none calibrated against the
Rider-verified jobs: data_quality_score (warning incidence, not error),
Will's level_pct (model self-report), and SCHEDULE_ESTIMATION_CONFIDENCE (a
derate). The 2026-06 review's fix (Part 5): compute confidence from the
*deterministic evidence the pipeline now produces* and calibrate it to true
per-job error from the golden set, so "±X% at 90% confidence" is a
measurable, falsifiable claim instead of vibes.

This module is pure/offline (no API, no PDF). It has three layers:

  1. compute_confidence_inputs(analysis, cost_estimate)
       -> the six deterministic signals + hard-gate flags, each now a real
          field on the analysis thanks to Phases 1-3 (coverage ledger,
          bbox/per-sheet anchors, verification pass, provenance gate,
          adjustment ledger).

  2. predict_error(inputs, calibration)
       -> a monotone EVIDENCE MODEL maps the inputs to a predicted error %.
          When the golden calibration table has >= MIN_CALIBRATION rows it
          REFITS the band to the observed per-bin 90th-percentile error;
          until then it reports calibrated=False with a conservative
          multiplier so the number never over-promises on thin data.

  3. assess_confidence(analysis, cost_estimate, calibration_path)
       -> the top-level: inputs + prediction + hard-gate caps, returned as
          analysis['calibrated_confidence'].

Hard gates are orthogonal (review): any failed plan page, missing
footprint, zero walls, or manual-review flag CAPS the displayed confidence
regardless of the evidence score — the number can never be high-and-wrong
for a reason already known.
"""
import json
import os

# Refit the band from data only once enough verified jobs exist; below this
# the evidence model's prior governs and we flag calibrated=False.
MIN_CALIBRATION = 8

# Evidence-model prior weights: each is the marginal error (percentage points)
# contributed when a signal is at its worst (1.0). Documented, not learned —
# they encode the review's mechanism (each lost-information source correlates
# with takeoff error) and are the starting point the golden data refines.
BASE_ERROR_PCT = 6.0
_W = {
    "missing_coverage": 35.0,   # (1 - coverage_pct): unmeasured plan pages
    "missing_anchor": 14.0,     # (1 - anchor_pct): rooms not tied to a label
    "verifier_miss": 18.0,      # verification found labeled rooms extraction missed
    "unanchored": 10.0,         # extracted rooms with no visible anchor (hallucination risk)
    "assumed_frac": 30.0,       # share of priced area that is heuristic, not measured
    "adjustment_mag": 12.0,     # |quantity adjustments| / measured baseline
    "over_extraction": 44.0,    # walls/ceiling ballooned relative to schedule doors
}

# Over-extraction signal (2026-06-13 calibration finding). The other inputs
# only sense UNDER-extraction; the deployed pipeline's dominant small-job
# failure is the OPPOSITE — confidently hallucinating 5-8x the real wall/
# ceiling area, internally consistent (footprint co-inflates, so geometry
# ratios look normal). The tell is CROSS-QUANTITY: doors come from the
# authoritative schedule, so wall/ceiling area PER DOOR spikes when the
# vision-extracted areas balloon while the door count stays anchored.
# Caps are the golden band + margin: verified jobs run 192-572 SF wall/door
# and 74-147 SF ceiling/door; the 5x-over Dutchess run hit 985 / 567 and
# the 2.9x-over Fishkill run hit 1,049 / 261, while an UNDER-extracted run
# stays in band (no false positive). Soft by design — it raises predicted
# error (lowering confidence, which the ready-to-send reconciliation then
# acts on) rather than hard-vetoing, and it only applies at >= MIN_DOORS so
# a legitimately low-door building (warehouse, open retail) isn't punished.
OVEREXT_WALLS_PER_DOOR_CAP = 700.0
OVEREXT_CEIL_PER_DOOR_CAP = 200.0
OVEREXT_MIN_DOORS = 5
OVEREXT_RATIO_CAP = 2.0       # clamp a single catastrophe's contribution

# Hard gates: when any trips, predicted error is floored and the displayed
# confidence level is capped — the estimate is known-incomplete.
HARD_GATE_ERROR_FLOOR_PCT = 25.0
HARD_GATE_CONFIDENCE_CAP = 60

# Until calibrated, widen the reported 90%-CI band by this factor so a thin
# evidence base never reads as precise. Shrinks toward 1.0 as N grows.
UNCALIBRATED_SAFETY = 1.5


def _num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _clamp01(x):
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------------------
# Layer 1 — deterministic inputs
# ---------------------------------------------------------------------------

def _all_rooms(analysis):
    for fl in (analysis.get("floors") or []):
        for r in (fl.get("rooms") or []):
            if isinstance(r, dict):
                yield r


def compute_confidence_inputs(analysis, cost_estimate=None):
    """Extract the six deterministic confidence signals + hard-gate flags
    from a finished analysis. Every value is in [0,1] (signals) or bool
    (gates). Missing data degrades gracefully toward the neutral/worst end
    so absence never reads as high confidence."""
    if not isinstance(analysis, dict):
        analysis = {}
    pi = analysis.get("project_info", {}) or {}

    # --- 1. Coverage: measured plan pages / plan-relevant pages ---
    cov = (analysis.get("coverage") or {}).get("totals") or {}
    measured = _num(cov.get("measured"))
    failed = _num(cov.get("failed"))
    degraded = _num(cov.get("degraded"))
    unaccounted = _num(cov.get("unaccounted"))
    plan_pages = measured + failed + degraded + unaccounted  # 'excluded' = out of scope
    coverage_pct = (measured / plan_pages) if plan_pages > 0 else None

    # --- 2. Anchor coverage: rooms tied to a real text-layer label ---
    bbox = analysis.get("bbox_spike_summary") or {}
    anchor_pct = None
    if bbox.get("total_rooms"):
        anchor_pct = _clamp01(_num(bbox.get("coverage_pct")) / 100.0)
    else:
        rooms = list(_all_rooms(analysis))
        if rooms:
            anchored = sum(1 for r in rooms if r.get("_anchor"))
            anchor_pct = anchored / len(rooms)

    # --- 3/4. Verifier recall: misses found + unanchored flagged (per-sheet) ---
    rooms = list(_all_rooms(analysis))
    n_rooms = len(rooms)
    added = sum(1 for r in rooms if r.get("_added_by_verification"))
    no_anchor = sum(1 for r in rooms if r.get("_no_anchor"))
    verifier_miss_rate = (added / n_rooms) if n_rooms else 0.0
    unanchored_rate = (no_anchor / n_rooms) if n_rooms else 0.0

    # --- 5. Provenance: share of priced AREA that is assumed, not measured ---
    pt = (analysis.get("_priced_takeoff") or {}).get("breakdown") or {}
    area_keys = ("total_paintable_wall_sqft", "total_paintable_ceiling_sqft")
    priced_area = sum(_num(pt.get(k, {}).get("priced")) for k in area_keys)
    assumed_area = sum(_num(pt.get(k, {}).get("assumed")) for k in area_keys)
    assumed_frac = (assumed_area / priced_area) if priced_area > 0 else 0.0

    # --- 6. Adjustment magnitude: |assumed adjustments| / measured baseline ---
    ledger = analysis.get("_quantity_adjustments") or []
    agg = analysis.get("aggregated_totals") or {}
    wall_final = _num(agg.get("total_paintable_wall_sqft"))
    assumed_adj = sum(abs(_num(e.get("delta"))) for e in ledger
                      if e.get("source") == "assumed"
                      and e.get("item") in area_keys)
    adjustment_mag = (assumed_adj / wall_final) if wall_final > 0 else 0.0

    # --- Over-extraction: wall/ceiling area per (schedule-anchored) door ---
    ceil_final = _num(agg.get("total_paintable_ceiling_sqft"))
    doors = (_num(agg.get("total_doors_full_paint"))
             + _num(agg.get("total_doors_hm_panel")))
    walls_per_door = (wall_final / doors) if doors > 0 else None
    ceil_per_door = (ceil_final / doors) if doors > 0 else None
    over_extraction_ratio = 0.0
    if doors >= OVEREXT_MIN_DOORS:
        excess = max((walls_per_door or 0) / OVEREXT_WALLS_PER_DOOR_CAP,
                     (ceil_per_door or 0) / OVEREXT_CEIL_PER_DOOR_CAP)
        over_extraction_ratio = max(0.0, min(OVEREXT_RATIO_CAP, excess - 1.0))

    # --- Hard gates ---
    bt = str(pi.get("building_type", "")).lower()
    is_residential = any(k in bt for k in
                         ("residential", "apartment", "condo", "multi", "mixed"))
    footprint = _num(pi.get("footprint_sqft"))
    gates = {
        "failed_pages": failed > 0,
        "missing_footprint": is_residential and footprint <= 0,
        "zero_walls": wall_final <= 0,
        "manual_review": bool(analysis.get("manual_review_required")),
    }

    return {
        "coverage_pct": coverage_pct,
        "anchor_pct": anchor_pct,
        "verifier_miss_rate": round(verifier_miss_rate, 4),
        "unanchored_rate": round(unanchored_rate, 4),
        "assumed_frac": round(assumed_frac, 4),
        "adjustment_mag": round(adjustment_mag, 4),
        "walls_per_door": round(walls_per_door, 1) if walls_per_door is not None else None,
        "ceil_per_door": round(ceil_per_door, 1) if ceil_per_door is not None else None,
        "over_extraction_ratio": round(over_extraction_ratio, 3),
        "n_rooms": n_rooms,
        "hard_gates": gates,
        "hard_gate_tripped": any(gates.values()),
    }


# ---------------------------------------------------------------------------
# Layer 2 — evidence model + calibration
# ---------------------------------------------------------------------------

def _evidence_score(inputs):
    """Predicted error % from the prior evidence model (pre-calibration).
    Monotone: every signal moving toward 'worse' can only raise the error."""
    # A None coverage/anchor signal is treated as the worst case (we have no
    # evidence it's good) but softened (0.5) so a library call lacking the
    # ledger isn't punished as hard as a real failed page.
    cov = inputs.get("coverage_pct")
    cov_gap = 1.0 - cov if cov is not None else 0.5
    anc = inputs.get("anchor_pct")
    anc_gap = 1.0 - anc if anc is not None else 0.5

    err = BASE_ERROR_PCT
    err += _W["missing_coverage"] * _clamp01(cov_gap)
    err += _W["missing_anchor"] * _clamp01(anc_gap)
    err += _W["verifier_miss"] * _clamp01(inputs.get("verifier_miss_rate", 0))
    err += _W["unanchored"] * _clamp01(inputs.get("unanchored_rate", 0))
    err += _W["assumed_frac"] * _clamp01(inputs.get("assumed_frac", 0))
    err += _W["adjustment_mag"] * _clamp01(inputs.get("adjustment_mag", 0))
    # Over-extraction is linear in the ratio (already clamped to OVEREXT_RATIO_CAP
    # in compute), NOT clamp01 — a 5x-over job must dominate the score, not
    # saturate at the same level as a 1.1x one.
    err += _W["over_extraction"] * inputs.get("over_extraction_ratio", 0.0)
    return err


def load_calibration(path):
    """Load the golden calibration table: a list of
    {job, inputs:{...}, true_error_pct}. Returns [] when absent/unreadable."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


def _percentile(values, q):
    """Linear-interpolated q-percentile (q in [0,1]) of a value list."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(s):
        return s[lo] + (s[lo + 1] - s[lo]) * frac
    return s[lo]


def predict_error(inputs, calibration=None, ci=0.90):
    """Map inputs -> {predicted_error_pct, ci_level, calibrated, basis}.

    Calibrated path (>= MIN_CALIBRATION golden rows): each row's evidence
    score is computed with the same model; rows whose score is within a
    window of this job's score form the bin; report that bin's `ci`-
    percentile observed error (binning, not ML — the review's prescription).

    Uncalibrated path: the evidence-model score widened by UNCALIBRATED_SAFETY.
    """
    base = _evidence_score(inputs)
    rows = calibration or []
    n = len(rows)

    if n >= MIN_CALIBRATION:
        job_score = base
        scored = []
        for r in rows:
            ri = r.get("inputs") or {}
            te = r.get("true_error_pct")
            if te is None:
                continue
            scored.append((_evidence_score(ri), _num(te)))
        if scored:
            # Bin = the K verified jobs whose evidence score is NEAREST this
            # job's (kNN), not a fixed window — a job at the extreme good/bad
            # end then bins against its true neighbours instead of dragging in
            # the middle of the set. Report that bin's `ci`-percentile observed
            # error (binning, not ML — the review's prescription).
            k = max(3, n // 3)
            scored.sort(key=lambda sc_te: abs(sc_te[0] - job_score))
            binned = [te for _sc, te in scored[:k]]
            pred = _percentile(binned, ci)
            return {
                "predicted_error_pct": round(pred, 1),
                "ci_level": ci,
                "calibrated": True,
                "basis": f"golden bin: {len(binned)} nearest of {n} verified "
                         f"jobs (evidence score {job_score:.0f})",
            }

    return {
        "predicted_error_pct": round(base * UNCALIBRATED_SAFETY, 1),
        "ci_level": ci,
        "calibrated": False,
        "basis": f"evidence-model prior (×{UNCALIBRATED_SAFETY} safety; "
                 f"{n}/{MIN_CALIBRATION} verified jobs — not yet calibrated)",
    }


# ---------------------------------------------------------------------------
# Layer 3 — top-level assessment
# ---------------------------------------------------------------------------

def _confidence_level(predicted_error_pct):
    """A 0-100 'confidence' for back-compat with level_pct consumers:
    high when the predicted error band is tight."""
    e = max(0.0, predicted_error_pct)
    # 5% err -> ~92, 10% -> ~85, 20% -> ~70, 35% -> ~48, 50% -> ~30
    return int(max(0, min(100, round(100 - e * 1.4))))


def assess_confidence(analysis, cost_estimate=None, calibration_path=None, ci=0.90):
    """Top-level: compute inputs, predict error, apply hard-gate caps.
    Returns the dict to store at analysis['calibrated_confidence']."""
    inputs = compute_confidence_inputs(analysis, cost_estimate)
    pred = predict_error(inputs, load_calibration(calibration_path), ci=ci)

    predicted = pred["predicted_error_pct"]
    caps_applied = []
    gates = inputs["hard_gates"]
    if inputs["hard_gate_tripped"]:
        tripped = [k for k, v in gates.items() if v]
        if predicted < HARD_GATE_ERROR_FLOOR_PCT:
            predicted = HARD_GATE_ERROR_FLOOR_PCT
            caps_applied.append(
                f"error floored to {HARD_GATE_ERROR_FLOOR_PCT}% "
                f"(hard gate: {', '.join(tripped)})")
        else:
            caps_applied.append(f"hard gate active: {', '.join(tripped)}")

    level = _confidence_level(predicted)
    if inputs["hard_gate_tripped"] and level > HARD_GATE_CONFIDENCE_CAP:
        caps_applied.append(
            f"confidence level capped {level}->{HARD_GATE_CONFIDENCE_CAP} "
            f"(hard gate)")
        level = HARD_GATE_CONFIDENCE_CAP

    return {
        "predicted_error_pct": round(predicted, 1),
        "ci_level": pred["ci_level"],
        "confidence_level": level,
        "calibrated": pred["calibrated"],
        "basis": pred["basis"],
        "caps_applied": caps_applied,
        "inputs": inputs,
    }


# ---------------------------------------------------------------------------
# Layer 4 — closed feedback loop (self-correcting calibration)
# ---------------------------------------------------------------------------
# The calibrated claim is only trustworthy if it refits as reality lands
# (review Part 5, property #1: "every customer/Rider correction auto-appends
# to golden/ and the curve refits"). These helpers are the append + status
# hooks the correction-handling path (or a batch run) calls.

def append_calibration_row(path, job, inputs, true_error_pct,
                           verified_on=None, metric="", detail=None,
                           source="correction"):
    """Append (or replace) one verified-error row in the calibration table at
    `path` and persist it. Dedupes by `job` so re-verifying a job updates its
    row rather than double-counting. Pure I/O — no API. Returns the new row
    count. The next assess_confidence() picks up the refit automatically once
    the table reaches MIN_CALIBRATION rows.
    """
    table = {"min_calibration": MIN_CALIBRATION, "rows": []}
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                table = loaded
            elif isinstance(loaded, list):
                table = {"min_calibration": MIN_CALIBRATION, "rows": loaded}
        except (json.JSONDecodeError, OSError):
            pass
    rows = [r for r in table.get("rows", []) if r.get("job") != job]
    row = {
        "job": job,
        "verified_on": verified_on,
        "true_error_pct": round(float(true_error_pct), 1),
        "true_error_metric": metric,
        "source": source,
        "inputs": inputs,
    }
    if detail:
        row["detail"] = detail
    rows.append(row)
    table["rows"] = rows
    table["min_calibration"] = MIN_CALIBRATION
    if path:
        with open(path, "w") as f:
            json.dump(table, f, indent=2)
    return len(rows)


def calibration_status(path):
    """Report whether calibration is active and how far from it.
    {n, min, active, needed}."""
    rows = load_calibration(path)
    n = len(rows)
    return {"n": n, "min": MIN_CALIBRATION, "active": n >= MIN_CALIBRATION,
            "needed": max(0, MIN_CALIBRATION - n)}


# Minimum calibrated confidence level to allow auto-send (mirrors Will's >=85).
READY_TO_SEND_MIN_LEVEL = 85


def reconcile_will_confidence(will_output, calibrated_confidence):
    """Reconcile Will's self-reported confidence with the calibrated band
    (review Part 5: Will's level_pct is model-self-report; the calibrated
    number is evidence-grounded). Mutates and returns will_output.

    Rule: the calibrated assessment can only TIGHTEN ready_to_send, never
    loosen it — Will keeps its veto, and the deterministic evidence adds a
    second, independent veto. So a job ships only when BOTH Will AND
    calibrated confidence are comfortable. Both numbers are recorded for the
    estimator; nothing is silently overwritten.
    """
    if not isinstance(will_output, dict) or not isinstance(calibrated_confidence, dict):
        return will_output
    conf = will_output.setdefault("confidence", {})
    conf["calibrated_error_pct"] = calibrated_confidence.get("predicted_error_pct")
    conf["calibrated_level"] = calibrated_confidence.get("confidence_level")
    conf["calibrated_is_calibrated"] = calibrated_confidence.get("calibrated")

    cal_level = calibrated_confidence.get("confidence_level")
    hard_gate = (calibrated_confidence.get("inputs") or {}).get("hard_gate_tripped")
    flags = will_output.setdefault("pipeline_flags", {})
    reasons = []
    if hard_gate:
        reasons.append("calibrated hard gate tripped "
                       f"({', '.join(k for k, v in (calibrated_confidence.get('inputs') or {}).get('hard_gates', {}).items() if v)})")
    if isinstance(cal_level, (int, float)) and cal_level < READY_TO_SEND_MIN_LEVEL:
        reasons.append(f"calibrated confidence {cal_level} < "
                       f"{READY_TO_SEND_MIN_LEVEL}")
    if reasons and flags.get("ready_to_send"):
        flags["ready_to_send"] = False
        flags["route_to_human_review"] = True
        flags.setdefault("ready_to_send_overrides", []).extend(reasons)
    return will_output
