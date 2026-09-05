#!/usr/bin/env python3
"""
Knight Shift — Per-Job Flag Resolver
===================================
Decides which NIGHTSHIFT_* flags a single job runs under, instead of
leaving every flag to the worker's process-wide environment.

WHY THIS EXISTS (2026-09-01). The 9/1 Caris prod-posture smoke test came
in at -44.9% against a banded (+2.6%) validation run. Nothing in the
engine had regressed: the banded result depended on
NIGHTSHIFT_WALL_BASIS_FACES, which is correctly OFF in production
because it is a fact about ONE customer's ground truth (Caris counts
wall faces, 39,752 SF; run-basis reads 15,667), not a change the engine
should make for everybody. Every convention-dependent result we have
banded — Caris faces, Dutchess/364 interior-only, Honey schedule-scope —
is unreachable in production until a job can carry its own conventions.
Process-wide env can only ever hold one customer's answer.

TWO KINDS OF FLAG
-----------------
MEASUREMENT flags describe how mature the engine is: per-sheet
extraction, the vector measurement engine, provenance gating, draw
median. They are the same for every customer, they only ever move
forward, and they converge to always-on. The resolver pins them so a
job's measurement posture is recorded rather than inherited from
whatever the worker happened to boot with.

CONVENTION flags encode a FACT ABOUT A CUSTOMER's bidding practice:
whether their bids exclude exterior scope, whether they count wall faces
or wall runs, whether they price only scheduled areas, which allowances
they carry. These are per-customer data. They are wrong to set globally
and wrong to guess.

RESOLUTION ORDER (last writer wins)
    1. engine    — MEASUREMENT_FLAGS + CONVENTION defaults
    2. profile   — Organization.convention_profile (the customer's
                   confirmed conventions; written back by reviewers)
    3. estimate  — per-submission overrides chosen for THIS job
    4. evidence  — switches the plans themselves justify (e.g. a set
                   stamped "NO INTERIOR WORK")

UNKNOWN CUSTOMER. An org with no confirmed profile does NOT get a guess.
It runs on conservative engine defaults, raises a convention RFI naming
each unresolved question, and is held for mandatory review. A reviewer
answers the RFI once and writes the answers back to the profile; every
later job for that customer resolves clean.

A profile carrying PROFILE_CONFIRMED_KEY is a reviewer's statement that
the profile is COMPLETE: conventions it does not mention are the
engine default deliberately, not by omission. Without that marker an
unmentioned convention is an open question and still raises its RFI —
otherwise a half-filled profile would silently answer "no" to
everything a reviewer never got to.

The resolver is inert unless NIGHTSHIFT_FLAG_RESOLVER is on: with the
flag off, resolve_flags() reports what it WOULD do and touches nothing,
so it can be shadowed in production before it steers a single job.
"""

import os
from typing import Optional


# Read at call time, never at import — a mid-rollout flip and the test
# suite both need to take effect without a worker restart.
def resolver_enabled() -> bool:
    """True when the resolver may steer a job's flags."""
    return os.environ.get(
        "NIGHTSHIFT_FLAG_RESOLVER", "0").strip() in ("1", "true", "True")


# ---------------------------------------------------------------------------
# MEASUREMENT LADDER
# ---------------------------------------------------------------------------
# The engine-maturity posture, lifted from the K=3 marathon child's
# BASELINE_FLAGS + NEW_FIX_FLAGS (nsai_k3_marathon_2026-08-25/
# run_k3_child.py) — the set every banded validation run was scored
# under. These are NOT customer-specific and NOT negotiable per job;
# they are pinned here so a job records the posture it actually ran.
#
# Anything still under validation stays OUT of this dict and off.
MEASUREMENT_FLAGS = {
    # Baseline ladder
    "NIGHTSHIFT_VME_PRIMARY": "1",
    "NIGHTSHIFT_VME_AUTHORITATIVE_WALLS": "1",
    "NIGHTSHIFT_VECTOR_MEASURE": "1",
    "NIGHTSHIFT_PER_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_PROVENANCE_GATE": "1",
    "NIGHTSHIFT_STAIR_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_CLOSET_RECOVERY": "1",
    # Merged fix flags
    "NIGHTSHIFT_WILL_SCOPE_REMOVAL": "1",
    "NIGHTSHIFT_ELEV_REQUIRE_SHEETS": "1",
    "NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP": "1",
    "NIGHTSHIFT_WC_TYPICAL_MATCH": "1",
    "NIGHTSHIFT_DOOR_TYPICAL_TRANSFER": "1",
    "NIGHTSHIFT_SALES_FLOOR_ACT_EVIDENCE": "1",
    "NIGHTSHIFT_ELEV_STRUCTURED_MEASURE": "1",
    "NIGHTSHIFT_ELEV_TEXT_EVIDENCE": "1",
    "NIGHTSHIFT_PAINT_SCHEDULE_GATE": "1",
    "NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP": "1",
    "NIGHTSHIFT_WC_DEDUCT_FLOOR": "1",
    "NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE": "1",
    "NIGHTSHIFT_ELEV_PASS_CONSENSUS": "3",
    # The variance program. Single-draw production eats the variance K=3
    # exists to remove (Caris 9/1: 27 rooms / 11,408 SF dropped on a
    # single draw vs 11 rooms / 789 SF banded).
    "NIGHTSHIFT_JOB_DRAW_MEDIAN": "3",
}


# ---------------------------------------------------------------------------
# CONVENTION REGISTRY
# ---------------------------------------------------------------------------
class ConventionFlag:
    """One per-customer convention the resolver can answer.

    default:  the conservative setting used when nobody has confirmed an
              answer — always the engine's own behavior, never a guess.
    question: the RFI text a reviewer answers to establish the fact.
    """

    __slots__ = ("name", "key", "default", "summary", "question", "on_means")

    def __init__(self, name, key, default, summary, question, on_means):
        self.name = name            # env var
        self.key = key              # profile key
        self.default = default
        self.summary = summary
        self.question = question
        self.on_means = on_means

    def __repr__(self):
        return f"<ConventionFlag {self.key}>"


CONVENTION_FLAGS = (
    ConventionFlag(
        "NIGHTSHIFT_INTERIOR_ONLY_CONVENTION", "interior_only", "0",
        "Bids exclude exterior and window scope",
        "Do this customer's bids cover exterior and window scope, or "
        "interior only? (Dutchess Livestock and 364 Main are scored "
        "against interior-only bids; 397 Fishkill's bid includes "
        "exterior.)",
        "exterior/window scope is excluded from the estimate",
    ),
    ConventionFlag(
        "NIGHTSHIFT_WALL_BASIS_FACES", "wall_basis_faces", "0",
        "Wall quantities counted as faces, not runs",
        "Does this customer measure wall paint by FACE (both sides of a "
        "partition) or by RUN (centerline length x height)? Caris Hyde "
        "Park bids faces; Rider and JW Harlem Valley bid runs.",
        "wall area is roughly doubled versus run basis",
    ),
    ConventionFlag(
        "NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE", "schedule_scope", "0",
        "Only scheduled areas are in scope",
        "On a fitout, does this customer paint only the rooms named in "
        "the finish schedule, or the whole tenant area? (Honey Farms "
        "Malta is bid schedule-only.)",
        "rooms absent from the finish schedule are not priced",
    ),
    ConventionFlag(
        "NIGHTSHIFT_CEILING_ASSUME_PAINTED", "ceilings_assumed_painted", "0",
        "Enclosed ceilings default to painted",
        "When a room has no ceiling finish called out, does this "
        "customer carry it as painted or leave it out until confirmed?",
        "enclosed rooms with no called-out ceiling finish are priced painted",
    ),
    ConventionFlag(
        "NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT", "ceilings_assumed_act", "0",
        "ACT-by-function rooms join the painted default",
        "Do rooms whose ACT ceiling is inferred from room function (not "
        "called out) follow the same painted default? Inert unless "
        "ceilings_assumed_painted is on.",
        "function-inferred ACT rooms are priced painted",
    ),
    ConventionFlag(
        "NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE", "allow_sealed_concrete", "0",
        "Carries a sealed-concrete allowance",
        "Does this customer carry sealed//exposed concrete floor "
        "finishing in their painting bid?",
        "a sealed-concrete allowance is added",
    ),
    ConventionFlag(
        "NIGHTSHIFT_LEVEL5_ALLOWANCE", "allow_level5", "0",
        "Carries a Level 5 finish allowance",
        "Does this customer carry Level 5 drywall finish as part of the "
        "paint scope?",
        "a Level 5 finish allowance is added",
    ),
    ConventionFlag(
        "NIGHTSHIFT_POWER_WASH_ALLOWANCE", "allow_power_wash", "0",
        "Carries a power-wash allowance",
        "Does this customer include power washing in exterior bids?",
        "a power-wash allowance is added",
    ),
    ConventionFlag(
        "NIGHTSHIFT_WINDOW_SASH_OPS", "allow_window_sash_ops", "0",
        "Prices window sash operations",
        "Does this customer price window sash prep/ops separately?",
        "window sash operations are priced",
    ),
    ConventionFlag(
        "NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE", "allow_factory_finish", "0",
        "Deducts factory-finished surfaces",
        "Does this customer deduct factory/pre-finished surfaces from "
        "the paint scope?",
        "factory-finished surfaces are deducted",
    ),
)

CONVENTION_BY_KEY = {c.key: c for c in CONVENTION_FLAGS}
CONVENTION_BY_NAME = {c.name: c for c in CONVENTION_FLAGS}

LAYERS = ("engine", "profile", "estimate", "evidence")

# Set by the reviewer write-back. Marks the profile complete, so
# conventions it omits are the engine default on purpose.
PROFILE_CONFIRMED_KEY = "_conventions_confirmed_at"

_TRUTHY = ("1", "true", "True", True, 1)


def _norm(value) -> Optional[str]:
    """Normalize a profile/override value to the engine's "0"/"1" strings.

    Returns None for values that carry no answer (None, "", "unknown"),
    which is how a partially-filled profile leaves the rest of its
    conventions unresolved instead of silently answering them "no".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value).strip()
    if not text or text.lower() in ("unknown", "unset", "none", "?"):
        return None
    if text in _TRUTHY:
        return "1"
    if text.lower() in ("0", "false", "no", "off"):
        return "0"
    return text


def resolve_flags(profile=None, overrides=None, evidence=None,
                  org_label=None):
    """Resolve the flag posture for one job.

    Args:
        profile:   Organization.convention_profile — {<profile key>: value}
                   for conventions a reviewer has CONFIRMED. A key that is
                   absent or None is unconfirmed, not "no" — unless the
                   profile carries PROFILE_CONFIRMED_KEY, which declares
                   it complete.
        overrides: per-estimate answers for THIS job only (same key shape).
                   Never written back to the profile by the resolver.
        evidence:  switches the plans justify, {<profile key>: (value,
                   reason)}. Empty at enqueue (no plans read yet); the
                   worker can re-resolve mid-pipeline once evidence exists.
        org_label: customer name, for RFI wording only.

    Returns a dict:
        flags            {ENV_NAME: "0"/"1"/...} — the full posture
        provenance       {ENV_NAME: layer} — who set each flag
        conventions      {profile key: value} — convention subset
        unresolved       [profile key, ...] — conventions nobody answered
        rfi_items        [{"category", "question"}, ...] for the estimate
        manual_review    True when unresolved conventions were assumed
        review_reason    str or None
        enabled          whether the resolver may steer this job

    With NIGHTSHIFT_FLAG_RESOLVER off the return value is identical but
    `enabled` is False and callers must not apply it — that is the
    shadow mode used to compare resolved vs. live posture in prod.
    """
    profile = profile or {}
    overrides = overrides or {}
    evidence = evidence or {}

    flags = {}
    provenance = {}

    # Layer 1 — engine. Measurement ladder plus conservative convention
    # defaults. Every flag is stamped even when its value matches the
    # engine default, so the record is a complete posture, not a diff.
    for name, value in MEASUREMENT_FLAGS.items():
        flags[name] = value
        provenance[name] = "engine"
    for conv in CONVENTION_FLAGS:
        flags[conv.name] = conv.default
        provenance[conv.name] = "engine"

    # Layers 2-4 — profile, then this estimate, then plan evidence.
    resolved_keys = set()
    for layer, source in (("profile", profile), ("estimate", overrides)):
        for key, raw in source.items():
            conv = CONVENTION_BY_KEY.get(key)
            if conv is None:
                continue
            value = _norm(raw)
            if value is None:
                continue
            flags[conv.name] = value
            provenance[conv.name] = layer
            resolved_keys.add(key)

    evidence_reasons = {}
    for key, raw in evidence.items():
        conv = CONVENTION_BY_KEY.get(key)
        if conv is None:
            continue
        value, reason = raw if isinstance(raw, (tuple, list)) else (raw, None)
        value = _norm(value)
        if value is None:
            continue
        flags[conv.name] = value
        provenance[conv.name] = "evidence"
        resolved_keys.add(key)
        if reason:
            evidence_reasons[key] = reason

    # Unknown customer: everything nobody confirmed stays at the engine
    # default AND gets named in an RFI. A convention we assumed is a
    # number the customer cannot check, so the job is held.
    unresolved = [c.key for c in CONVENTION_FLAGS if c.key not in resolved_keys]
    profile_complete = bool(_norm(profile.get(PROFILE_CONFIRMED_KEY)))
    if profile_complete:
        # A reviewer vouched for the whole profile: the rest really are
        # the defaults. Record that as provenance rather than an RFI.
        for key in unresolved:
            provenance[CONVENTION_BY_KEY[key].name] = "profile"
        unresolved = []

    rfi_items = []
    manual_review = False
    review_reason = None
    if unresolved:
        label = org_label or "this customer"
        who = f"{label}'s"
        manual_review = True
        for key in unresolved:
            conv = CONVENTION_BY_KEY[key]
            rfi_items.append({
                "category": "Bidding Convention",
                "question": (
                    f"{conv.question} This estimate assumed the "
                    f"conservative default (OFF: not "
                    f"{conv.on_means}). Confirm once and we will apply "
                    f"it to every future {who} estimate automatically."
                ),
            })
        review_reason = (
            f"{len(unresolved)} bidding convention(s) unconfirmed for "
            f"{label} — "
            f"{'; '.join(CONVENTION_BY_KEY[k].summary.lower() for k in unresolved)}"
            f". Priced on conservative defaults; confirm before sending."
        )

    return {
        "flags": flags,
        "provenance": provenance,
        "conventions": {
            c.key: flags[c.name] for c in CONVENTION_FLAGS
        },
        "unresolved": unresolved,
        "rfi_items": rfi_items,
        "manual_review": manual_review,
        "review_reason": review_reason,
        "enabled": resolver_enabled(),
    }


def apply_flags(resolution, environ=None):
    """Push a resolution's flags into the process environment.

    No-op (returning an empty dict) when the resolver is disabled, so a
    shadow-mode worker resolves, records, and changes nothing.

    Returns {name: (previous, new)} for the flags actually changed.
    """
    if not resolution or not resolution.get("enabled"):
        return {}
    env = os.environ if environ is None else environ
    changed = {}
    for name, value in (resolution.get("flags") or {}).items():
        previous = env.get(name)
        if previous == value:
            continue
        env[name] = value
        changed[name] = (previous, value)
    return changed


def confirm_profile(existing, answers, confirmed_at, complete=True):
    """Merge a reviewer's confirmed conventions into a customer profile.

    This is the write-back that ends the RFI loop: a reviewer answers the
    convention questions once on the first held job, and every later job
    for that customer resolves from the profile with no RFI and no hold.

    Args:
        existing:     the org's current convention_profile (or None).
        answers:      {profile key: value} the reviewer confirmed. Keys
                      outside CONVENTION_FLAGS are ignored, and a value
                      that normalizes to None (blank, "unknown") leaves
                      that convention open rather than answering it "no".
        confirmed_at: ISO timestamp string for the completeness marker.
        complete:     True when the reviewer is vouching for the WHOLE
                      profile — conventions they left blank are the
                      engine default deliberately. False records the
                      answers but keeps the rest open (and still RFI'd),
                      which is the right call for a partial review.

    Returns the new profile dict. Never mutates `existing`.
    """
    profile = dict(existing or {})
    for key, raw in (answers or {}).items():
        if key not in CONVENTION_BY_KEY:
            continue
        value = _norm(raw)
        if value is None:
            continue
        profile[key] = value
    if complete:
        profile[PROFILE_CONFIRMED_KEY] = confirmed_at
    else:
        profile.pop(PROFILE_CONFIRMED_KEY, None)
    return profile


def profile_from_resolution(resolution, keys=None):
    """The write-back payload a reviewer's confirmation produces.

    Takes the conventions this job ran under and returns them in profile
    shape, so `Organization.convention_profile` can be updated after a
    reviewer signs off. `keys` limits the write-back to the conventions
    the reviewer actually confirmed; omitting it writes back all of them.
    """
    conventions = (resolution or {}).get("conventions") or {}
    if keys is None:
        return dict(conventions)
    return {k: v for k, v in conventions.items() if k in set(keys)}
