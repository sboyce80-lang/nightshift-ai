#!/usr/bin/env python3
"""Scope mask — the typed contract between the LLM and the geometry layer.

Phase 1 of the accuracy program. Target architecture: deterministic
measurement first — VME geometry and schedule ledgers own QUANTITIES;
the LLM's only output is a SCOPE MASK over that geometry: per-room
in/out decisions, painted fractions, classifications, exclusions and
RFIs. The LLM never emits SF/LF/EA, and the validator here makes that
structural: any quantity-shaped key anywhere in a mask is a validation
error, not a style complaint.

Mask schema (MASK_VERSION "1.0")
================================

    {
      "mask_version": "1.0",
      "source": "extraction" | "llm" | "manual",     # optional provenance
      "rooms": {
        "<identity>": {                # room_number when present, else room_id
          "in_scope": true,            # REQUIRED — the one mandatory field
          "painted_fraction": 0.75,    # optional; scalar 0..1, OR per-surface:
                                       #   {"walls": 1.0, "ceiling": 0.0,
                                       #    "trim": 1.0}
          "classification": {          # optional; str or per-surface strs,
            "ceiling": "ACT",          #   e.g. ACT / GWB / exposed / DRYFALL
            "walls": "GYP"
          },
          "evidence": [                # optional citations for the decision
            {"sheet": "A104", "citation": "finish schedule row 417"}
          ],
          "confidence": 0.9,           # optional; 0..1
          "reason": "..."              # optional; why out of scope, etc.
        },
        ...
      },
      "exclusions": ["...strings..."],  # job-level scope exclusions
      "rfis": ["...strings..."]         # job-level questions for the customer
    }

Room identity is the stable join key used everywhere else in the chain
(_apply_schedule_room_scope, the door ledger): room_number when the room
has one, else room_id. Rooms with neither cannot be masked.

The core rule
=============
A mask may not carry quantities. validate_scope_mask rejects ANY dict
key, at ANY depth (including room-identity keys), matching
_QUANTITY_KEY_RX (sqft | _lf | _ea | count | area, case-insensitive).
That is the interface: quantities flow only from the deterministic
measurement layer; the mask only says what fraction of them is in the
paint scope and what the surfaces are.

apply_scope_mask never touches aggregated_totals — recomputing
quantities from the masked room set is the geometry layer's job
(_recalculate_totals and the VME gates), not the mask's.
"""
import copy
import re

MASK_VERSION = "1.0"
SUPPORTED_VERSIONS = (MASK_VERSION,)

# The no-quantities rule. Any dict key anywhere in a mask matching this
# pattern invalidates the mask.
_QUANTITY_KEY_RX = re.compile(r"sqft|_lf|_ea|count|area", re.IGNORECASE)

_SURFACES = ("walls", "ceiling", "trim")
_ENTRY_KEYS = ("in_scope", "painted_fraction", "classification",
               "evidence", "confidence", "reason")
_TOP_KEYS = ("mask_version", "source", "rooms", "exclusions", "rfis")
_EVIDENCE_KEYS = ("sheet", "citation")


def _is_num(v):
    """Real number (bool is an int subclass and is NOT a number here)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _quantity_keys(obj, path=""):
    """Every dict key in obj (recursively) matching the quantity pattern."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and _QUANTITY_KEY_RX.search(k):
                hits.append(kp)
            hits.extend(_quantity_keys(v, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_quantity_keys(v, f"{path}[{i}]"))
    return hits


def _validate_fraction(v, where, errors):
    if _is_num(v):
        if not (0.0 <= float(v) <= 1.0):
            errors.append(f"{where}: painted_fraction {v} outside 0..1")
    elif isinstance(v, dict):
        for sk, sv in v.items():
            if sk not in _SURFACES:
                errors.append(
                    f"{where}: painted_fraction surface '{sk}' not one of "
                    f"{_SURFACES}")
            elif not _is_num(sv):
                errors.append(
                    f"{where}: painted_fraction.{sk} must be a number, "
                    f"got {type(sv).__name__}")
            elif not (0.0 <= float(sv) <= 1.0):
                errors.append(
                    f"{where}: painted_fraction.{sk} {sv} outside 0..1")
    else:
        errors.append(
            f"{where}: painted_fraction must be a number or a per-surface "
            f"dict, got {type(v).__name__}")


def _validate_classification(v, where, errors):
    if isinstance(v, str):
        if not v.strip():
            errors.append(f"{where}: classification is an empty string")
    elif isinstance(v, dict):
        for sk, sv in v.items():
            if sk not in _SURFACES:
                errors.append(
                    f"{where}: classification surface '{sk}' not one of "
                    f"{_SURFACES}")
            elif not (isinstance(sv, str) and sv.strip()):
                errors.append(
                    f"{where}: classification.{sk} must be a non-empty "
                    f"string")
    else:
        errors.append(
            f"{where}: classification must be a string or a per-surface "
            f"dict, got {type(v).__name__}")


def _validate_evidence(v, where, errors):
    if not isinstance(v, list):
        errors.append(f"{where}: evidence must be a list")
        return
    for i, ev in enumerate(v):
        ep = f"{where}.evidence[{i}]"
        if not isinstance(ev, dict):
            errors.append(f"{ep}: must be a dict {{sheet, citation}}")
            continue
        for k in ev:
            if k not in _EVIDENCE_KEYS:
                errors.append(f"{ep}: unknown key '{k}'")
        if not (isinstance(ev.get("sheet"), str) and ev["sheet"].strip()):
            errors.append(f"{ep}: 'sheet' must be a non-empty string")
        if "citation" in ev and not isinstance(ev["citation"], str):
            errors.append(f"{ep}: 'citation' must be a string")


def _validate_entry(identity, entry, errors):
    where = f"rooms['{identity}']"
    if not isinstance(entry, dict):
        errors.append(f"{where}: entry must be a dict")
        return
    for k in entry:
        if k not in _ENTRY_KEYS:
            errors.append(f"{where}: unknown key '{k}'")
    if "in_scope" not in entry:
        errors.append(f"{where}: missing required key 'in_scope'")
    elif not isinstance(entry["in_scope"], bool):
        errors.append(f"{where}: in_scope must be a bool, got "
                      f"{type(entry['in_scope']).__name__}")
    if "painted_fraction" in entry:
        _validate_fraction(entry["painted_fraction"], where, errors)
    if "classification" in entry:
        _validate_classification(entry["classification"], where, errors)
    if "evidence" in entry:
        _validate_evidence(entry["evidence"], where, errors)
    if "confidence" in entry:
        c = entry["confidence"]
        if not _is_num(c):
            errors.append(f"{where}: confidence must be a number")
        elif not (0.0 <= float(c) <= 1.0):
            errors.append(f"{where}: confidence {c} outside 0..1")
    if "reason" in entry and not isinstance(entry["reason"], str):
        errors.append(f"{where}: reason must be a string")


def validate_scope_mask(mask):
    """Validate a scope mask against the v1 contract.

    Returns (ok, errors): ok is True iff errors is empty. Checks types,
    ranges, the closed key set, and — the core rule — that no dict key
    anywhere in the mask (room identities included) is quantity-shaped
    (sqft | _lf | _ea | count | area). A mask that carries quantities is
    not a mask; it is the LLM trying to own measurement again.
    """
    errors = []
    if not isinstance(mask, dict):
        return False, ["mask must be a dict"]
    for k in mask:
        if k not in _TOP_KEYS:
            errors.append(f"unknown top-level key '{k}'")
    ver = mask.get("mask_version")
    if ver is None:
        errors.append("missing required key 'mask_version'")
    elif ver not in SUPPORTED_VERSIONS:
        errors.append(f"unsupported mask_version {ver!r} "
                      f"(supported: {SUPPORTED_VERSIONS})")
    if "source" in mask and not isinstance(mask["source"], str):
        errors.append("source must be a string")
    rooms = mask.get("rooms")
    if rooms is None:
        errors.append("missing required key 'rooms'")
    elif not isinstance(rooms, dict):
        errors.append("rooms must be a dict keyed by room identity")
    else:
        for identity, entry in rooms.items():
            if not (isinstance(identity, str) and identity.strip()):
                errors.append(
                    f"room identity {identity!r} must be a non-empty string")
            _validate_entry(identity, entry, errors)
    for lk in ("exclusions", "rfis"):
        if lk in mask:
            v = mask[lk]
            if not isinstance(v, list):
                errors.append(f"{lk} must be a list of strings")
            else:
                for i, s in enumerate(v):
                    if not (isinstance(s, str) and s.strip()):
                        errors.append(
                            f"{lk}[{i}] must be a non-empty string")
    for kp in _quantity_keys(mask):
        errors.append(
            f"quantity-shaped key not allowed in a scope mask: '{kp}' "
            f"(quantities belong to the measurement layer)")
    return (not errors), errors


def room_identity(room):
    """Stable mask identity for a room dict: room_number when present,
    else room_id, else None (unmaskable)."""
    if not isinstance(room, dict):
        return None
    for k in ("room_number", "room_id"):
        v = room.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _iter_rooms(analysis):
    for fl in (analysis.get("floors") or []):
        if not isinstance(fl, dict):
            continue
        for rm in (fl.get("rooms") or []):
            if isinstance(rm, dict):
                yield rm


def apply_scope_mask(analysis, mask):
    """Apply a validated scope mask to an analysis. PURE: the input
    analysis is never mutated; a masked deep copy is returned.

    What it does: for every room whose identity appears in the mask, it
    marks the room in/out of scope (recording a scope_exclusion_reason
    and a note, mirroring _apply_schedule_room_scope), and stores the
    entry's painted fractions / classification / confidence / evidence
    on the room under the "_scope_mask" namespace. Job-level exclusions
    become notes; job-level RFIs are queued in _pre_pricing_rfis (same
    shape _gate_add_rfi produces, de-duplicated the same way). A
    "_scope_mask_applied" record {version, rooms_masked, source, ...}
    is stamped on the analysis.

    What it deliberately does NOT do: touch aggregated_totals or any
    quantity. Recomputing quantities from the masked room set is the
    geometry layer's job (_recalculate_totals / the VME gates); the mask
    only decides scope. Raises ValueError if the mask fails
    validate_scope_mask.
    """
    ok, errors = validate_scope_mask(mask)
    if not ok:
        raise ValueError("invalid scope mask: " + "; ".join(errors[:10]))
    out = copy.deepcopy(analysis)
    if not isinstance(out, dict):
        return out
    entries = mask.get("rooms") or {}
    masked = 0
    excluded, included = [], []
    for rm in _iter_rooms(out):
        identity = room_identity(rm)
        if identity is None or identity not in entries:
            continue
        entry = entries[identity]
        masked += 1
        ns = {"prior_in_scope": bool(rm.get("in_scope", True))}
        for k in ("painted_fraction", "classification", "confidence",
                  "evidence", "reason"):
            if k in entry:
                ns[k] = copy.deepcopy(entry[k])
        rm["_scope_mask"] = ns
        want = bool(entry.get("in_scope"))
        have = bool(rm.get("in_scope", True))
        if have and not want:
            rm["in_scope"] = False
            reason = str(entry.get("reason") or "").strip()
            rm["scope_exclusion_reason"] = (
                "excluded by scope mask" + (f": {reason}" if reason else ""))
            excluded.append(rm.get("room_name") or identity)
        elif want and not have:
            rm["in_scope"] = True
            rm["scope_exclusion_reason"] = ""
            included.append(rm.get("room_name") or identity)
    notes = out.setdefault("notes", [])
    if excluded or included:
        parts = []
        if excluded:
            names = ", ".join(str(n) for n in excluded[:8])
            more = f" (+{len(excluded) - 8} more)" if len(excluded) > 8 \
                else ""
            parts.append(f"{len(excluded)} room(s) marked out of scope: "
                         f"{names}{more}")
        if included:
            parts.append(f"{len(included)} room(s) returned to scope")
        notes.append("[Scope Mask] " + "; ".join(parts) + ". Quantities "
                     "are recomputed by the measurement layer, not here.")
    for excl in (mask.get("exclusions") or []):
        notes.append(f"[Scope Mask] Exclusion: {excl}")
    if mask.get("rfis"):
        bucket = out.setdefault("_pre_pricing_rfis", [])
        for q in mask["rfis"]:
            if not any(isinstance(r, dict) and
                       r.get("category") == "Scope Mask" and
                       r.get("question") == q for r in bucket):
                bucket.append({"category": "Scope Mask", "question": q})
    out["_scope_mask_applied"] = {
        "version": mask.get("mask_version"),
        "rooms_masked": masked,
        "rooms_excluded": len(excluded),
        "rooms_included": len(included),
        "source": mask.get("source") or "unknown",
    }
    return out


def _as_bool(v):
    """The extraction schema's boolean, which arrives as bool OR string
    ('true'/'True'/'false'/...). Mirrors the ceiling_painted coercion."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def mask_from_extraction(analysis, source="extraction"):
    """Derive a v1 scope mask from what extraction already produces.

    This is the bridge that lets the mask interface run in SHADOW mode on
    existing results and proves the contract fits real data: today's
    per-room scope signal (in_scope + scope_exclusion_reason), painted
    surfaces (materials.ceiling_painted; walls painted whenever the room
    is in scope; trim painted when the room carries base trim) and
    material classifications map onto mask fields with no quantity ever
    copied. Rooms without a room_number or room_id are skipped; when two
    rooms share an identity (template units), the first occurrence wins
    and later ones are not re-keyed — the mask speaks about identities,
    not instances.
    """
    rooms = {}
    if isinstance(analysis, dict):
        for rm in _iter_rooms(analysis):
            identity = room_identity(rm)
            if identity is None or identity in rooms:
                continue
            in_scope = bool(rm.get("in_scope", True))
            mats = rm.get("materials") or {}
            ceil_painted = _as_bool(
                mats.get("ceiling_painted", rm.get("ceiling_painted", False)))
            elems = rm.get("elements") or {}
            try:
                has_trim = float(elems.get("base_trim_lf") or 0) > 0
            except (TypeError, ValueError):
                has_trim = False
            entry = {
                "in_scope": in_scope,
                "painted_fraction": {
                    "walls": 1.0 if in_scope else 0.0,
                    "ceiling": 1.0 if (in_scope and ceil_painted) else 0.0,
                    "trim": 1.0 if (in_scope and has_trim) else 0.0,
                },
            }
            cls = {}
            for src_key, surf in (("walls", "walls"), ("ceiling", "ceiling")):
                v = mats.get(src_key)
                if isinstance(v, str) and v.strip():
                    cls[surf] = v.strip()
            if cls:
                entry["classification"] = cls
            sheet = rm.get("source_sheet")
            if isinstance(sheet, str) and sheet.strip():
                entry["evidence"] = [{
                    "sheet": sheet.strip(),
                    "citation": f"extraction record for room {identity}",
                }]
            if not in_scope:
                reason = str(rm.get("scope_exclusion_reason") or "").strip()
                if reason:
                    entry["reason"] = reason
            rooms[identity] = entry
    exclusions = sorted({
        str(rm.get("scope_exclusion_reason")).strip()
        for rm in _iter_rooms(analysis if isinstance(analysis, dict) else {})
        if not rm.get("in_scope", True)
        and str(rm.get("scope_exclusion_reason") or "").strip()})
    rfis = []
    if isinstance(analysis, dict):
        for r in (analysis.get("_pre_pricing_rfis") or []):
            if isinstance(r, dict):
                q = str(r.get("question") or "").strip()
                if q and q not in rfis:
                    rfis.append(q)
            elif isinstance(r, str) and r.strip():
                rfis.append(r.strip())
    return {
        "mask_version": MASK_VERSION,
        "source": source,
        "rooms": rooms,
        "exclusions": exclusions,
        "rfis": rfis,
    }
