#!/usr/bin/env python3
"""
Knight Shift — Billing-Convention Layer (Phase 1 of the accuracy program)
=========================================================================
Makes the customer's billing convention a DECLARED, TESTED transform
between measurement and price instead of an implicit property of
whichever pipeline pass ran last.

WHY THIS EXISTS (engineering review, 2026-09-04). A frontier model
measured 364 Main's wall geometry to +2.8% of the customer's takeoff and
then billed +61% wrong purely by CONVENTION — it billed both wall faces
while Rider bills run-LF × per-floor height. The 7/21 VME run made the
same face-vs-run error (+32%). Geometry was never the problem; the
undeclared convention was.

WHAT A PROFILE DECLARES
-----------------------
  basis              "run" (each partition counted once — centerline LF ×
                     height) or "faces" (each painted face counted;
                     gross multiplied by faces_factor).
  faces_factor       multiplier for the faces basis (2.0 = both faces).
  per_floor_heights  OPTIONAL height fallback rules, used ONLY for rooms
                     with NO measured ceiling height (and no wall area
                     already priced). Measured heights always win — the
                     hard-numbers policy is unchanged; these are the
                     customer's own confirmed heights, not guesses.
  wall_openings_rule record-only documentation of how the customer
                     treats openings. It changes nothing yet; it exists
                     so the convention record is complete.

PROFILE SELECTION IS EXPLICIT, NEVER INFERRED. resolve_profile() reads
env NIGHTSHIFT_BILLING_PROFILE (worker/job-level — the flag resolver's
per-job env application is the same channel org conventions already
travel) or an explicitly plumbed org key on the analysis. It never
guesses a profile from job content. Unknown names fall back to
"default" loudly (warning note + `requested` recorded in the stamp).

FACES-BASIS PRECEDENCE (decided 2026-09-05, tested): the EXPLICITLY SET
env flag NIGHTSHIFT_WALL_BASIS_FACES wins over the profile. When that
env var is set (non-empty), it alone decides engagement and
NIGHTSHIFT_WALL_FACES_FACTOR decides the factor; the profile is
consulted only when the env var is absent. One source, one multiply —
Takeoff_DIRECT._wall_faces_basis() is the single resolution point and
the VME walls gate multiplies exactly once.

Flag-gated NIGHTSHIFT_BILLING_CONVENTION (default OFF). When ON,
apply_billing_convention() ALWAYS stamps analysis["_billing_convention"]
with which convention priced the job. With the "default" profile it
must not change any quantity (byte-identity, tested).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

FLAG = "NIGHTSHIFT_BILLING_CONVENTION"
PROFILE_ENV = "NIGHTSHIFT_BILLING_PROFILE"

BASIS_RUN = "run"
BASIS_FACES = "faces"


def enabled() -> bool:
    """True when the billing-convention layer may act (read at call time)."""
    return os.environ.get(FLAG, "0").strip() in ("1", "true", "True")


@dataclass(frozen=True)
class BillingConvention:
    """One customer's declared billing convention."""

    name: str
    basis: str = BASIS_RUN
    faces_factor: float = 1.0
    # Height fallback rules keyed "first" / "basement" / "default_upper",
    # applied ONLY to rooms with no measured ceiling height AND no wall
    # area (measured heights always win — hard-numbers policy).
    per_floor_heights: Optional[Dict[str, float]] = None
    wall_openings_rule: str = ""
    evidence: str = ""


PROFILES: Dict[str, BillingConvention] = {
    # No customer assumptions: run basis, factor 1.0, no height fallback.
    # This is the engine's own behavior — byte-identical quantities.
    "default": BillingConvention(
        name="default",
        basis=BASIS_RUN,
        faces_factor=1.0,
        per_floor_heights=None,
        wall_openings_rule=(
            "record-only: no opening-deduct threshold declared; openings "
            "price as measured"),
        evidence="engine default — no per-customer facts applied",
    ),
    # Rider Painting, interior takeoffs. Decoded from Rider's own 364 Main
    # takeoff xlsx (memory: rider-takeoff-convention): wall quantity is
    # RUN LF (single count — his 8,629 wall LF EXACTLY equals his
    # base-trim LF, proof he does not count both faces) × PER-FLOOR
    # heights: 12' first floor (retail), 9.5' 2nd/3rd, 9' basement.
    # Golden 85,353 SF = Σ(run LF × floor height) exactly. VME reproduced
    # 86% of golden under this convention (vs a bogus "56% over" when
    # faces × uniform 9' was assumed).
    "rider_interior": BillingConvention(
        name="rider_interior",
        basis=BASIS_RUN,
        faces_factor=1.0,
        per_floor_heights={
            "first": 12.0,
            "basement": 9.0,
            "default_upper": 9.5,
        },
        wall_openings_rule=(
            "record-only: 364 Main takeoff shows no opening deduct on "
            "wall runs (run LF == base-trim LF); openings price as "
            "measured"),
        evidence=(
            "364 Main takeoff: 8,629 run LF == base-trim LF (single-count "
            "proof); heights 12'/9.5'/9' per floor; golden 85,353 SF = "
            "sum(run x height); VME 86% of golden under this convention"),
    ),
    # Caris Hyde Park (a JW customer) bids each painted FACE. Ground
    # truth carries 39,752 SF of wall paint where run-basis read 15,667;
    # run × 2 − WC deduct lands ≈ their number exactly (2,607 LF × 9' ×
    # 2 − WC ≈ 41.7k). This is a CARIS fact, not JW-wide (see "jw"
    # below) — the same evidence that put the "never a class default"
    # warning on NIGHTSHIFT_WALL_BASIS_FACES.
    "caris_faces": BillingConvention(
        name="caris_faces",
        basis=BASIS_FACES,
        faces_factor=2.0,
        per_floor_heights=None,
        wall_openings_rule=(
            "record-only: Caris reconciliation deducts WC from the gross "
            "faces quantity (run x h x 2 - WC ~= ground truth)"),
        evidence=(
            "Caris Hyde Park ground truth 39,752 SF wall paint vs "
            "run-basis 15,667; 2,607 LF x 9' x 2 - WC ~= 41.7k "
            "(k3 marathon runner note + Takeoff_DIRECT faces comment, "
            "2026-08-26)"),
    ),
    # JW = default ON PURPOSE: the evidence is NOT a single JW-wide
    # convention. Caris (a JW job) bids faces, but JW's own Harlem Valley
    # target sits at run basis (nsai_k3_marathon_2026-08-25/
    # run_k3_child.py: "Faces wall basis is a CARIS fact ... not a
    # JW-wide convention — Harlem's own target sits at run basis";
    # flag_resolver.py: "Caris Hyde Park bids faces; Rider and JW Harlem
    # Valley bid runs"). No ground_truth.json in nsai_batch_2026-08-20
    # settles a per-floor height rule either. Until a reviewer confirms
    # a JW-wide fact, jw carries no assumptions.
    "jw": BillingConvention(
        name="jw",
        basis=BASIS_RUN,
        faces_factor=1.0,
        per_floor_heights=None,
        wall_openings_rule=(
            "record-only: no JW-wide opening rule confirmed"),
        evidence=(
            "evidence is mixed (Caris faces vs Harlem run) — jw is the "
            "engine default until a reviewer confirms a JW-wide fact; "
            "use caris_faces for Caris jobs"),
    ),
}


def resolve_profile(
        analysis: Optional[dict] = None,
) -> Tuple[BillingConvention, str, Optional[str]]:
    """Resolve which profile prices this job. EXPLICIT selection only.

    Precedence:
      1. env NIGHTSHIFT_BILLING_PROFILE — worker/job-level. The flag
         resolver applies org conventions to the job env, so this is
         also the channel a confirmed org profile arrives on.
      2. analysis["_org_billing_profile"] — forward hook for direct
         org-level plumbing (Organization.convention_profile writers).
      3. "default".

    NEVER inferred from job content. Unknown names fall back to
    "default" with a warning (third return value).

    Returns (profile, source, warning) with source in "env"|"org"|"default".
    """
    name = None
    source = "default"
    env_name = os.environ.get(PROFILE_ENV, "").strip()
    if env_name:
        name, source = env_name, "env"
    elif isinstance(analysis, dict):
        org_name = analysis.get("_org_billing_profile")
        if isinstance(org_name, str) and org_name.strip():
            name, source = org_name.strip(), "org"

    if name is None:
        return PROFILES["default"], "default", None
    profile = PROFILES.get(name)
    if profile is None:
        return (
            PROFILES["default"], "default",
            f"unknown billing profile '{name}' (from {source}) — fell "
            f"back to 'default' (no customer assumptions)")
    return profile, source, None


_BASEMENT_RE = re.compile(r"\b(basement|cellar|lower\s*level)\b", re.I)
_FIRST_RE = re.compile(
    r"\b(first|1st|ground|(?:level|floor|lvl)\s*-?0?1)\b(?!\d)", re.I)


def _classify_floor(floor_name: Any) -> str:
    """Map a floor name onto a per_floor_heights key. Conservative:
    anything not clearly basement/first is "default_upper"."""
    name = str(floor_name or "")
    if _BASEMENT_RE.search(name):
        return "basement"
    if _FIRST_RE.search(name):
        return "first"
    return "default_upper"


def _num(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def faces_engagement(analysis: Optional[dict]) -> Optional[float]:
    """Profile-driven faces factor for the VME walls gate, or None.

    Consulted by Takeoff_DIRECT._wall_faces_basis() ONLY when the env
    flag NIGHTSHIFT_WALL_BASIS_FACES is absent — an explicitly set env
    flag wins over the profile (documented precedence). Returns a
    clamped factor when the job's stamped convention is faces-basis.
    """
    if not enabled():
        return None
    stamp = analysis.get("_billing_convention") if isinstance(
        analysis, dict) else None
    if not isinstance(stamp, dict) or stamp.get("basis") != BASIS_FACES:
        return None
    factor = _num(stamp.get("faces_factor")) or 2.0
    return min(2.5, max(1.0, factor))


def _fill_missing_heights(analysis: dict,
                          profile: BillingConvention) -> list:
    """Apply the profile's per-floor height fallback.

    ONLY rooms with no measured ceiling height AND no wall area are
    touched (measured heights always win — hard-numbers policy). A room
    also needs a measured perimeter (or L×W to derive one); with nothing
    measured there is nothing to apply a height to, exactly like the
    cross-sheet back-fill (_backfill_missing_wall_heights).
    """
    heights = profile.per_floor_heights or {}
    filled = []
    for floor in (analysis.get("floors") or []):
        if not isinstance(floor, dict):
            continue
        cls = _classify_floor(floor.get("floor_name"))
        rule_h = _num(heights.get(cls, heights.get("default_upper", 0)))
        if rule_h <= 0:
            continue
        for room in (floor.get("rooms") or []):
            if not isinstance(room, dict) or room.get("in_scope") is False:
                continue
            dims = room.get("dimensions")
            if not isinstance(dims, dict):
                continue
            if _num(dims.get("ceiling_height_feet", 0)) > 0:
                continue  # measured height wins, always
            if _num(dims.get("wall_area_sqft", 0)) > 0:
                continue  # walls already priced from some source
            perimeter = _num(dims.get("perimeter_lf", 0))
            if perimeter <= 0:
                length = _num(dims.get("length_feet", 0))
                width = _num(dims.get("width_feet", 0))
                if length > 0 and width > 0:
                    perimeter = 2 * (length + width)
                    dims["perimeter_lf"] = round(perimeter)
            if perimeter <= 0:
                continue  # nothing measured to apply the height to
            new_wall = round(perimeter * rule_h)
            dims["ceiling_height_feet"] = rule_h
            dims["wall_area_sqft"] = new_wall
            dims["_wall_height_source"] = (
                f"billing_convention:{profile.name}")
            filled.append({
                "room_name": room.get("room_name", "?"),
                "floor": floor.get("floor_name", ""),
                "floor_class": cls,
                "height_ft": rule_h,
                "wall_area_sqft": new_wall,
            })
    return filled


def apply_billing_convention(
        analysis: dict,
        recalc: Optional[Callable[[dict], Any]] = None) -> dict:
    """Declare which billing convention prices this job (the ONE
    application point, called from build_priced_takeoff before any
    quantity pass).

    Flag-gated NIGHTSHIFT_BILLING_CONVENTION, default OFF (byte-identical
    no-op when off). When ON:
      - ALWAYS stamps analysis["_billing_convention"] with the profile
        name, basis, faces_factor, heights rule and selection source.
      - Applies the profile's per-floor height fallback to rooms with
        no measured height (measured heights win; `recalc` — pass
        Takeoff_DIRECT._recalculate_totals — rebuilds aggregates when
        any room changed).
      - The faces basis engages through the stamp: the VME walls gate's
        _wall_faces_basis() consults faces_engagement() when the env
        flag is absent. No quantity is multiplied here — one source,
        one multiply, at the existing gate.
      - Appends one customer-readable note naming the convention.
      - With the "default" profile no quantity changes (tested).

    Idempotent via the stamp.
    """
    if not isinstance(analysis, dict):
        return analysis
    if not enabled():
        return analysis
    if isinstance(analysis.get("_billing_convention"), dict):
        return analysis  # already declared for this job

    profile, source, warning = resolve_profile(analysis)

    filled = []
    if profile.per_floor_heights:
        filled = _fill_missing_heights(analysis, profile)

    stamp = {
        "profile": profile.name,
        "basis": profile.basis,
        "faces_factor": profile.faces_factor,
        "heights_rule": (dict(profile.per_floor_heights)
                         if profile.per_floor_heights else None),
        "wall_openings_rule": profile.wall_openings_rule,
        "source": source,
        "heights_filled": filled,
    }
    if warning:
        requested = os.environ.get(PROFILE_ENV, "").strip() or None
        stamp["requested"] = requested
        stamp["warning"] = warning
    analysis["_billing_convention"] = stamp

    notes = analysis.setdefault("notes", [])
    if warning:
        notes.append(f"[Billing Convention] {warning}.")
    if profile.basis == BASIS_FACES:
        basis_text = (
            f"each painted wall FACE is counted (geometric run x "
            f"{profile.faces_factor:g}), matching this customer's "
            f"takeoff convention")
    else:
        basis_text = (
            "wall quantities count each partition run once "
            "(run LF x height)")
    height_text = ""
    if filled:
        height_text = (
            f"; {len(filled)} room(s) with no measured ceiling height "
            f"were priced at this customer's confirmed per-floor "
            f"heights (measured heights always take precedence)")
    notes.append(
        f"[Billing Convention] Priced under the '{profile.name}' "
        f"convention ({source}): {basis_text}{height_text}.")

    if filled and callable(recalc):
        recalc(analysis)
    return analysis
