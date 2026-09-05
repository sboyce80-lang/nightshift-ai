"""Tiered agentic reviewer — the bounded model pass on flagged jobs.

Phase 3, second half. The deterministic suite (delivery_verification.py)
decides WHICH jobs get expensive attention; this module is the attention.
It does what the manual review sessions did: reconcile the artifacts the
suite flagged and write the human-readable "here is what looks wrong and
why" note, with RFIs and a hold/release recommendation.

Hard constraints, structural, not prompt-hoped:
  * It runs only when the suite flagged the job (tiered — the median job
    never pays for it) and only under NIGHTSHIFT_DELIVERY_REVIEWER=1.
  * It can never edit a number. Nothing numeric from the response is
    applied anywhere — the only fields consumed are verdict, findings
    text, and RFI text. A reviewer that adjusts quantities is K-draws
    reinvented (round-4: the draw-median rejected a −5.4% draw for a
    −26.5% one).
  * "hold" may ADD manual review; "release" NEVER clears an existing
    hold — release recommendations are recorded for the audit trail
    only, until a month of audited calls earns auto-release (the Phase 3
    exit criterion).
  * Cost is capped: one call, evidence packet truncated to
    _PACKET_CAP_CHARS, max_tokens 1500, temperature 0.
  * Any failure (API, parse) records verdict "hold" — fail safe, and
    never raises into the chain.

Flags:
  NIGHTSHIFT_DELIVERY_REVIEWER        default OFF
  NIGHTSHIFT_DELIVERY_REVIEWER_MODEL  default claude-sonnet-4-6
"""
import json
import os

_PACKET_CAP_CHARS = 18000
_MAX_TOKENS = 1500

_SYSTEM = (
    "You are the pre-delivery reviewer for a commercial painting "
    "estimate pipeline. A deterministic reconciliation suite flagged "
    "this job; your task is to explain what is suspicious, in terms a "
    "human reviewer can act on, and to recommend hold or release.\n"
    "Rules:\n"
    "1. You NEVER propose, compute, or adjust quantities or prices — "
    "not in any field. If a quantity looks wrong, say WHY it looks "
    "wrong and what document would settle it (that becomes an RFI).\n"
    "2. Cite the specific evidence from the packet (check ids, keys, "
    "page/sheet references) for every finding.\n"
    "3. Recommend \"hold\" unless the flags are clearly benign.\n"
    "Respond with ONLY a JSON object: {\"verdict\": \"hold\"|\"release\", "
    "\"findings\": [{\"check_id\": str, \"severity\": \"high\"|\"medium\""
    "|\"low\", \"explanation\": str}], \"rfis\": [str], "
    "\"reviewer_note\": str}")


def _enabled():
    return os.environ.get(
        "NIGHTSHIFT_DELIVERY_REVIEWER", "0").strip() in ("1", "true", "True")


def build_review_packet(analysis):
    """Compact, text-only evidence bundle for the flagged job. Truncated
    hard at _PACKET_CAP_CHARS — the reviewer is bounded by construction."""
    dv = analysis.get("_delivery_verification") or {}
    packet = {
        "delivery_checks": dv.get("checks"),
        "ledger_reconcile": analysis.get("_ledger_reconcile"),
        "agg_drift": (analysis.get("_agg_drift") or {}).get("keys"),
        "door_ledger": analysis.get("_door_ledger"),
        "schedule_room_scope": analysis.get("_schedule_room_scope"),
        "billing_convention": analysis.get("_billing_convention"),
        "aggregated_totals": analysis.get("aggregated_totals"),
        "manual_review_reason": analysis.get("manual_review_reason"),
        "notes_tail": (analysis.get("notes") or [])[-12:],
        "rfi_categories": sorted({str(r.get("category"))
                                  for r in analysis.get("rfi_items") or []
                                  if isinstance(r, dict)}),
    }
    text = json.dumps(packet, default=str, sort_keys=True)
    if len(text) > _PACKET_CAP_CHARS:
        text = text[:_PACKET_CAP_CHARS] + "…[truncated]"
    return text


def _parse_response(raw):
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        obj = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    verdict = obj.get("verdict")
    if verdict not in ("hold", "release"):
        return None
    findings = []
    for f in obj.get("findings") or []:
        if isinstance(f, dict):
            findings.append({
                "check_id": str(f.get("check_id") or "")[:60],
                "severity": str(f.get("severity") or "medium")[:10],
                "explanation": str(f.get("explanation") or "")[:600]})
    rfis = [str(r)[:400] for r in (obj.get("rfis") or [])
            if isinstance(r, str)][:8]
    return {"verdict": verdict, "findings": findings[:12], "rfis": rfis,
            "reviewer_note": str(obj.get("reviewer_note") or "")[:1200]}


def run_delivery_review(analysis, client=None):
    """One bounded model pass. Returns the review record; caller stores
    and applies it (hold-only). Raises nothing — failure = hold."""
    packet = build_review_packet(analysis)
    model = os.environ.get(
        "NIGHTSHIFT_DELIVERY_REVIEWER_MODEL", "claude-sonnet-4-6").strip()
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            timeout=120.0,
            system=_SYSTEM,
            messages=[{"role": "user", "content": packet}])
        raw = "".join(getattr(b, "text", "") or "" for b in resp.content)
        parsed = _parse_response(raw)
        if parsed is None:
            return {"verdict": "hold", "error": "unparseable response",
                    "raw_head": raw[:200]}
        return parsed
    except Exception as e:
        return {"verdict": "hold",
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


def attach_delivery_review(analysis, client=None):
    """Suite-flagged jobs only. Stores _delivery_review; a hold verdict
    adds manual review; a release verdict is RECORDED ONLY — it never
    clears an existing hold (audit month first). Never raises."""
    if not _enabled() or not isinstance(analysis, dict):
        return analysis
    dv = analysis.get("_delivery_verification") or {}
    if not dv.get("n_flags"):
        return analysis
    try:
        review = run_delivery_review(analysis, client=client)
        review["applied"] = "hold" if review["verdict"] == "hold" else \
            "recorded-only"
        analysis["_delivery_review"] = review
        if review["verdict"] == "hold":
            reason = ("Reviewer hold: "
                      + (review.get("reviewer_note")
                         or review.get("error") or "see _delivery_review")
                      [:300])
            analysis["manual_review_required"] = True
            prior = analysis.get("manual_review_reason")
            analysis["manual_review_reason"] = (
                f"{prior} | {reason}" if prior else reason)
        for q in review.get("rfis") or []:
            analysis.setdefault("rfi_items", []).append(
                {"category": "Delivery Review", "question": q})
        n_f = len(review.get("findings") or [])
        print(f"   🧑‍⚖️ Delivery reviewer: {review['verdict']} "
              f"({n_f} finding(s), {len(review.get('rfis') or [])} RFI(s))")
    except Exception as e:
        try:
            print(f"   ⚠️  delivery reviewer failed (non-fatal): "
                  f"{type(e).__name__}: {str(e)[:160]}")
            analysis.setdefault("_delivery_review", {
                "verdict": "hold", "error": str(e)[:160]})
        except Exception:
            pass
    return analysis
