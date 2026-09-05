#!/usr/bin/env python3
"""Pre-delivery deterministic reconciliation suite (Phase 3).

Every check codifies a NAMED historical miss — nothing here is
speculative lint. The suite is read-only over the analysis (it never
mutates a quantity); the chain hook stores the record at
analysis["_delivery_verification"] and, only under the explicit HOLD
flag, escalates flags into manual review.

    run_delivery_checks(analysis, costs=None)
        -> {"checks": [{"id", "status": pass|flag|skip, "detail"}],
            "n_flags": int}

The checks and the misses they codify:

  totals_reconcile     the $11,904 Devine/INNIO ceiling phantom — an
                       aggregate mutated outside the adjustment ledger
                       (reads the chain's own _ledger_reconcile record).
  schedule_vs_instance the Northwell 78-door schedule vs 31%-of-bid
                       priced-door gap — a parsed schedule and the priced
                       aggregate diverging silently.
  cross_sheet_dedup    the 88 Academy stairwell counted 6x across sheets
                       (82% of an +87.7% overage in ONE room number).
  read_then_discarded  the Toyota deck / Profeta A-207 class — an
                       extracted authority (finish schedule, door ledger,
                       per-room ceiling data) contributing ZERO, silently.
  white_label          the 2026-09-01 Profeta delivery carrying 11
                       "Rider" references — grep the JSON, not just the
                       PDF.
  page_coverage        the Toyota 9-failed-pages class — failed/excluded
                       pages are missing scope, and failed finish-plan
                       pages (RCP/A6xx) are the worst kind.
  confidence_floor     a held or low-confidence job must never read as
                       clean at the delivery boundary.

Flags:
  NIGHTSHIFT_DELIVERY_VERIFICATION       default OFF (burn-in; becomes
                                         default ON after calibration)
  NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD  default OFF; when on, any
                                         n_flags>0 sets
                                         manual_review_required with the
                                         check ids in the reason.
"""

import json
import os
import re

PASS, FLAG, SKIP = "pass", "flag", "skip"

# Brand names that must never appear in a customer-facing result unless
# the job belongs to that contractor (the white-label leak class).
COMPETITOR_BRANDS = ("rider painting", "rider llc", "jw painting")

# Key-path segments that legitimately carry competitor names (golden
# fixtures, calibration tables, test scaffolding) — everything else in
# the result JSON is customer-facing or feeds customer-facing copy.
_WHITE_LABEL_EXEMPT = ("golden", "test", "fixture", "calibration")

# Records whose presence (as a real, non-noop record) proves the room
# finish schedule was actually CONSUMED by a gate rather than read and
# forgotten. A {"noop": ...} or {"error": ...} record is proof of the
# opposite: the gate looked and stood down.
_SCHEDULE_CONSUMER_KEYS = (
    "_schedule_room_scope", "_schedule_scope_clip", "_paint_schedule_gate",
    "_wc_schedule_gate", "_schedule_authoritative_counts",
    "_floor_finish_reconcile", "_ceiling_scope_gate",
)

_FINISH_SHEET_RE = re.compile(r"(rcp|finish|a-?6\d|reflected)", re.I)


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _check(cid, status, detail):
    return {"id": cid, "status": status, "detail": detail}


def _rfs(analysis):
    """Room finish schedule rows, wherever the run stored them."""
    rows = (analysis.get("room_finish_schedule")
            or (analysis.get("schedule_data") or {}).get(
                "room_finish_schedule")
            or [])
    return [r for r in rows if isinstance(r, dict)]


def _record_used(v):
    """True when a chain record shows a gate actually acted (not a noop
    stand-down, not an error)."""
    if not v:
        return False
    if isinstance(v, dict):
        return "noop" not in v and "error" not in v
    return True


# ---------------------------------------------------------------- checks

def check_totals_reconcile(analysis, costs=None):
    """Line items vs aggregated_totals: reads the chain's own
    _ledger_reconcile record (see _reconcile_quantity_ledger) — any
    n_keys > 0 is an aggregate that moved outside the adjustment ledger,
    the exact family behind the $11,904 ceiling phantom."""
    rec = analysis.get("_ledger_reconcile")
    if not isinstance(rec, dict):
        return _check("totals_reconcile", SKIP,
                      "no _ledger_reconcile record (kill-switched or "
                      "pre-chain analysis)")
    if rec.get("error"):
        return _check("totals_reconcile", SKIP,
                      f"ledger reconcile errored: {rec['error'][:120]}")
    drift = analysis.get("_agg_drift") or {}
    drift_note = ""
    if isinstance(drift, dict) and drift.get("max_abs_drift_pct") \
            is not None:
        drift_note = (f"; room-recompute drift max "
                      f"{drift['max_abs_drift_pct']}% across "
                      f"{drift.get('n_keys_drifted', 0)} key(s) "
                      f"(context only)")
    n = int(_num(rec.get("n_keys")))
    if n > 0:
        keys = sorted((rec.get("unledgered") or {}).items(),
                      key=lambda kv: -_num((kv[1] or {}).get(
                          "worst_gap_pct")))
        named = ", ".join(
            f"{k} (worst gap {_num((v or {}).get('worst_gap_pct')):.1f}%)"
            for k, v in keys[:4])
        return _check("totals_reconcile", FLAG,
                      f"{n} aggregate key(s) moved outside the adjustment "
                      f"ledger: {named}{drift_note}")
    return _check("totals_reconcile", PASS,
                  f"adjustment ledger contiguous for every priced key"
                  f"{drift_note}")


def check_schedule_vs_instance(analysis, costs=None):
    """Parsed door schedule vs priced door aggregate within 25% — the
    Northwell gap was a 78-door schedule the bid never priced (31% of
    the bid)."""
    dl = analysis.get("_door_ledger")
    if not isinstance(dl, dict) or dl.get("error"):
        return _check("schedule_vs_instance", SKIP,
                      "no parsed door schedule record to reconcile "
                      "against")
    if dl.get("mode") != "schedule" or _num(dl.get("count")) < 5:
        return _check("schedule_vs_instance", SKIP,
                      f"door ledger mode={dl.get('mode')!r} "
                      f"count={dl.get('count')} — no authoritative "
                      f"schedule parse")
    n = _num(dl.get("count"))
    agg = analysis.get("aggregated_totals") or {}
    priced = _num(agg.get("total_doors_full_paint")) + \
        _num(agg.get("total_doors_hm_panel"))
    delta = abs(n - priced) / max(n, priced, 1.0)
    demoted = " (ledger demoted with RFI — divergence is documented, "\
        "not resolved)" if dl.get("demoted") else ""
    if delta > 0.25:
        return _check("schedule_vs_instance", FLAG,
                      f"door schedule parsed {n:.0f} vs {priced:.0f} "
                      f"priced ({delta * 100:.0f}% apart){demoted}")
    return _check("schedule_vs_instance", PASS,
                  f"door schedule {n:.0f} vs {priced:.0f} priced "
                  f"({delta * 100:.0f}% apart)")


def check_cross_sheet_dedup(analysis, costs=None):
    """One room number priced many times — the 88 Academy stairwell
    appeared on 6 sheets and was priced 6x (82% of the +87.7% overage)."""
    seen = {}   # canon number -> [occurrences, set(floor keys)]
    n_numbered = 0
    for fi, fl in enumerate(analysis.get("floors") or []):
        if not isinstance(fl, dict):
            continue
        fkey = str(fl.get("floor_name") or fl.get("floor") or fi)
        for room in (fl.get("rooms") or []):
            if not isinstance(room, dict) or not room.get(
                    "in_scope", True):
                continue
            num = re.sub(r"\s+", "", str(room.get("room_number")
                                         or "")).upper()
            if not num or not re.search(r"\d", num):
                continue
            n_numbered += 1
            ent = seen.setdefault(num, [0, set()])
            ent[0] += 1
            ent[1].add(fkey)
    if not n_numbered:
        return _check("cross_sheet_dedup", SKIP,
                      "no in-scope rooms carry room numbers")
    offenders = [(num, occ, len(fls)) for num, (occ, fls) in seen.items()
                 if occ > 3 or len(fls) > 2]
    if offenders:
        offenders.sort(key=lambda t: -t[1])
        named = ", ".join(f"{num} x{occ} on {nf} floor(s)"
                          for num, occ, nf in offenders[:5])
        return _check("cross_sheet_dedup", FLAG,
                      f"{len(offenders)} room number(s) priced "
                      f"repeatedly in scope: {named}")
    return _check("cross_sheet_dedup", PASS,
                  f"{n_numbered} numbered in-scope room instance(s), "
                  f"none repeated beyond the dedup bounds")


def check_read_then_discarded(analysis, costs=None):
    """An extracted authority contributing zero, silently — the Toyota
    deck (RCP data read then discarded) and Profeta A-207 (finish plan
    read, estimate unchanged) class."""
    signals = []
    rows = _rfs(analysis)
    if len(rows) >= 5:
        used = [k for k in _SCHEDULE_CONSUMER_KEYS
                if _record_used(analysis.get(k))]
        if not used:
            stood_down = [k for k in _SCHEDULE_CONSUMER_KEYS
                          if isinstance(analysis.get(k), dict)
                          and "noop" in analysis[k]]
            extra = (f" ({len(stood_down)} gate(s) recorded a "
                     f"stand-down)" if stood_down else "")
            signals.append(
                f"room finish schedule has {len(rows)} row(s) but no "
                f"schedule-consumer record shows it was used{extra}")

    dl = analysis.get("_door_ledger")
    if isinstance(dl, dict) and dl.get("mode") == "schedule" \
            and _num(dl.get("count")) >= 5 and not dl.get("demoted"):
        notes = " ".join(str(n) for n in (analysis.get("notes") or []))
        applied = "[Door Ledger]" in notes and "source: ledger" in notes
        if not applied:
            signals.append(
                f"door ledger parsed {dl.get('count')} door(s) "
                f"(mode A) but was neither applied nor demoted-with-RFI")

    agg = analysis.get("aggregated_totals") or {}
    if _num(agg.get("total_paintable_ceiling_sqft")) <= 0:
        ceil_sf = 0.0
        for fl in (analysis.get("floors") or []):
            if not isinstance(fl, dict):
                continue
            for room in (fl.get("rooms") or []):
                if not isinstance(room, dict) or not room.get(
                        "in_scope", True):
                    continue
                mats = room.get("materials") or {}
                if mats.get("ceiling_painted") is not True:
                    continue
                dims = room.get("dimensions") or {}
                ceil_sf += _num(dims.get("ceiling_area_sqft")
                                or dims.get("floor_area_sqft"))
        if ceil_sf > 500:
            signals.append(
                f"rooms carry {ceil_sf:,.0f} SF of ceiling_painted=True "
                f"area while priced ceilings total 0")

    if signals:
        return _check("read_then_discarded", FLAG, "; ".join(signals))
    if not rows and not isinstance(dl, dict):
        return _check("read_then_discarded", SKIP,
                      "no extracted schedule authorities to test")
    return _check("read_then_discarded", PASS,
                  "every extracted authority either contributed or "
                  "recorded its demotion")


def _walk_json(obj, path, hits):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_json(v, path + (str(k),), hits)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk_json(v, path + (str(i),), hits)
    else:
        try:
            s = obj if isinstance(obj, str) else json.dumps(
                obj, default=str)
        except (TypeError, ValueError):
            s = str(obj)
        low = s.lower()
        for brand in COMPETITOR_BRANDS:
            if brand in low:
                if any(any(ex in seg.lower() for ex in
                           _WHITE_LABEL_EXEMPT) for seg in path):
                    continue
                hits.append(("/".join(path) or "<root>", brand))


def check_white_label(analysis, costs=None):
    """Competitor brand names anywhere in the result JSON — the Profeta
    2026-09-01 delivery carried 11 'Rider' references. Grep the JSON,
    not just the PDF."""
    hits = []
    _walk_json(analysis, (), hits)
    if costs is not None:
        _walk_json(costs, ("costs",), hits)
    if hits:
        named = "; ".join(f"{p} -> {b!r}" for p, b in hits[:5])
        more = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
        return _check("white_label", FLAG,
                      f"{len(hits)} competitor-brand reference(s) in the "
                      f"result JSON: {named}{more}")
    return _check("white_label", PASS,
                  "no competitor brand names in the result JSON")


def check_page_coverage(analysis, costs=None):
    """Failed/excluded pages are scope that is NOT in this estimate —
    Toyota's 9 deterministically-failed RCP/finish pages were its
    missing deck scope."""
    cov = analysis.get("coverage")
    if not isinstance(cov, dict) or not cov.get("total_pages"):
        return _check("page_coverage", SKIP,
                      "no coverage ledger record on this analysis")
    t = cov.get("totals") or {}
    total = _num(cov.get("total_pages"))
    failed = _num(t.get("failed"))
    excluded = _num(t.get("excluded"))
    frac = (failed + excluded) / max(total, 1.0)
    finish_hint = []
    for f in (cov.get("files") or []):
        if not isinstance(f, dict) or not f.get("failed_pages"):
            continue
        if _FINISH_SHEET_RE.search(str(f.get("file") or "")):
            finish_hint.append(
                f"{f['file']} p.{','.join(map(str, f['failed_pages'][:8]))}")
    line = (f"{total:.0f} page(s): {failed:.0f} failed, "
            f"{excluded:.0f} excluded ({frac * 100:.0f}% of the set)")
    if finish_hint:
        return _check("page_coverage", FLAG,
                      f"{line}; FAILED pages on finish-plan-looking "
                      f"sheet(s): {'; '.join(finish_hint)}")
    if frac > 0.20:
        return _check("page_coverage", FLAG,
                      f"{line} — more than 20% of the set is not in "
                      f"this estimate")
    return _check("page_coverage", PASS, line)


def check_confidence_floor(analysis, costs=None):
    """A held or low-confidence job must never read as clean — carries
    manual_review_required and calibrated confidence < 30 through as a
    flag at the delivery boundary."""
    reasons = []
    if analysis.get("manual_review_required"):
        why = str(analysis.get("manual_review_reason") or
                  "no reason recorded")[:160]
        reasons.append(f"manual_review_required set ({why})")
    cc = analysis.get("calibrated_confidence")
    level = None
    if isinstance(cc, dict) and cc.get("confidence_level") is not None:
        level = _num(cc.get("confidence_level"))
        if level < 30:
            reasons.append(f"calibrated confidence {level:.0f} < 30")
    if reasons:
        return _check("confidence_floor", FLAG, "; ".join(reasons))
    detail = ("no manual-review hold" +
              (f"; calibrated confidence {level:.0f}"
               if level is not None
               else "; no calibrated confidence record"))
    return _check("confidence_floor", PASS, detail)


_CHECKS = (
    check_totals_reconcile,
    check_schedule_vs_instance,
    check_cross_sheet_dedup,
    check_read_then_discarded,
    check_white_label,
    check_page_coverage,
    check_confidence_floor,
)


def run_delivery_checks(analysis, costs=None):
    """Run every reconciliation check read-only over the analysis.

    Never mutates the analysis; a check that itself errors reports skip
    with the error (the suite is a thermometer, never a tourniquet).
    """
    checks = []
    if not isinstance(analysis, dict):
        analysis = {}
    for fn in _CHECKS:
        cid = fn.__name__.replace("check_", "")
        try:
            checks.append(fn(analysis, costs))
        except Exception as e:  # noqa: BLE001 — a check must never fail the job
            checks.append(_check(cid, SKIP,
                                 f"check errored: "
                                 f"{type(e).__name__}: {str(e)[:120]}"))
    return {"checks": checks,
            "n_flags": sum(1 for c in checks if c["status"] == FLAG)}


# ------------------------------------------------------------ chain hook

def _enabled():
    return os.environ.get(
        "NIGHTSHIFT_DELIVERY_VERIFICATION", "0").strip() in (
        "1", "true", "True")


def _hold_enabled():
    return os.environ.get(
        "NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD", "0").strip() in (
        "1", "true", "True")


def attach_delivery_verification(analysis, costs=None):
    """Chain hook: run the suite, store the record, print one line, and
    (HOLD flag only) escalate flags into manual review. The only key the
    suite ever writes is _delivery_verification — plus the review fields
    under the explicit HOLD flag. Never raises."""
    if not _enabled() or not isinstance(analysis, dict):
        return analysis
    try:
        rec = run_delivery_checks(analysis, costs)
        analysis["_delivery_verification"] = rec
        flagged = [c["id"] for c in rec["checks"]
                   if c["status"] == FLAG]
        n_pass = sum(1 for c in rec["checks"] if c["status"] == PASS)
        n_skip = sum(1 for c in rec["checks"] if c["status"] == SKIP)
        line = (f"{n_pass} pass / {rec['n_flags']} flag / {n_skip} skip"
                + (f" — flagged: {', '.join(flagged)}" if flagged
                   else ""))
        print(f"   🔎 Delivery verification: {line}")
        if flagged and _hold_enabled():
            reason = (f"Delivery verification flagged "
                      f"{rec['n_flags']} check(s): {', '.join(flagged)}")
            analysis["manual_review_required"] = True
            prior = analysis.get("manual_review_reason")
            analysis["manual_review_reason"] = (
                f"{prior} | {reason}" if prior else reason)
        if flagged:
            # Tier 2: the bounded model reviewer, flagged jobs only,
            # behind its own flag (see delivery_reviewer.py — hold-only
            # authority, never quantities, never raises).
            try:
                import delivery_reviewer as _dr
                analysis = _dr.attach_delivery_review(analysis)
            except Exception:
                pass
    except Exception as e:
        try:
            print(f"   ⚠️  delivery verification failed (non-fatal): "
                  f"{type(e).__name__}: {str(e)[:160]}")
        except Exception:
            pass
    return analysis
