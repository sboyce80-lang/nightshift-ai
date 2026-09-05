#!/usr/bin/env python3
"""Billing-convention layer (billing_convention.py) — Phase 1.

Locks in: explicit profile resolution (env selection, unknown-name
fallback, never inferred from content); flag-OFF byte-identity; default-
profile quantity byte-identity; faces profile engaging the existing VME
faces-factor path exactly once with env-wins precedence; rider_interior
height rules touching ONLY rooms with no measured ceiling height
(measured heights win); the convention stamp always written when ON.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_ENV_KEYS = (
    "NIGHTSHIFT_BILLING_CONVENTION",
    "NIGHTSHIFT_BILLING_PROFILE",
    "NIGHTSHIFT_WALL_BASIS_FACES",
    "NIGHTSHIFT_WALL_FACES_FACTOR",
)
_SAVED = {k: os.environ.get(k) for k in _ENV_KEYS}


def _set_env(**kwargs):
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kwargs.items():
        os.environ[k] = v


import billing_convention as bc  # noqa: E402
import Takeoff_DIRECT as td  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def sample_analysis():
    return {
        "floors": [
            {"floor_name": "First Floor", "rooms": [
                # No measured height, measured perimeter → fallback target
                {"room_name": "Retail", "in_scope": True,
                 "dimensions": {"length_feet": 20.0, "width_feet": 10.0,
                                "ceiling_height_feet": 0,
                                "wall_area_sqft": 0,
                                "perimeter_lf": 60.0}},
                # Measured height — must NEVER be touched
                {"room_name": "Office", "in_scope": True,
                 "dimensions": {"length_feet": 10.0, "width_feet": 10.0,
                                "ceiling_height_feet": 10.0,
                                "wall_area_sqft": 400.0,
                                "perimeter_lf": 40.0}},
            ]},
            {"floor_name": "Second Floor", "rooms": [
                # No height, no perimeter, but L×W → derivable
                {"room_name": "Studio", "in_scope": True,
                 "dimensions": {"length_feet": 12.0, "width_feet": 10.0,
                                "ceiling_height_feet": 0,
                                "wall_area_sqft": 0}},
            ]},
            {"floor_name": "Basement", "rooms": [
                # Nothing measured at all → must stay untouched
                {"room_name": "Storage", "in_scope": True,
                 "dimensions": {"ceiling_height_feet": 0,
                                "wall_area_sqft": 0}},
            ]},
        ],
        "aggregated_totals": {"total_paintable_wall_sqft": 400.0},
        "notes": [],
    }


print("billing convention checks")

# --- profile registry sanity ------------------------------------------------
check(set(bc.PROFILES) >= {"default", "rider_interior", "jw",
                           "caris_faces"},
      f"registry carries the declared profiles: {sorted(bc.PROFILES)}")
check(bc.PROFILES["default"].basis == "run"
      and bc.PROFILES["default"].per_floor_heights is None,
      "default profile carries no customer assumptions")
check(bc.PROFILES["jw"].basis == "run"
      and bc.PROFILES["jw"].per_floor_heights is None,
      "jw == engine default (Caris-faces vs Harlem-run evidence is mixed)")
check(bc.PROFILES["caris_faces"].basis == "faces"
      and bc.PROFILES["caris_faces"].faces_factor == 2.0,
      "caris_faces declares the faces basis at 2.0")
rh = bc.PROFILES["rider_interior"].per_floor_heights
check(bc.PROFILES["rider_interior"].basis == "run"
      and rh == {"first": 12.0, "basement": 9.0, "default_upper": 9.5},
      f"rider_interior = run basis + 12/9.5/9 per-floor heights: {rh}")

# --- resolution: env selection ---------------------------------------------
_set_env(NIGHTSHIFT_BILLING_PROFILE="rider_interior")
prof, source, warn = bc.resolve_profile({})
check(prof.name == "rider_interior" and source == "env" and warn is None,
      f"env selects the profile: {prof.name}/{source}")

# --- resolution: org hook ---------------------------------------------------
_set_env()
prof, source, warn = bc.resolve_profile({"_org_billing_profile": "jw"})
check(prof.name == "jw" and source == "org",
      f"org key selects the profile when env is absent: {prof.name}/{source}")

# --- resolution: env wins over org ------------------------------------------
_set_env(NIGHTSHIFT_BILLING_PROFILE="default")
prof, source, _ = bc.resolve_profile({"_org_billing_profile": "jw"})
check(prof.name == "default" and source == "env",
      "env selection outranks the org key")

# --- resolution: unknown name falls back loudly ------------------------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1",
         NIGHTSHIFT_BILLING_PROFILE="bogus_profile")
prof, source, warn = bc.resolve_profile({})
check(prof.name == "default" and warn is not None
      and "bogus_profile" in warn,
      f"unknown profile name falls back to default with a warning: {warn}")
a = sample_analysis()
bc.apply_billing_convention(a)
stamp = a.get("_billing_convention")
check(isinstance(stamp, dict)
      and stamp.get("requested") == "bogus_profile"
      and stamp.get("profile") == "default"
      and any("bogus_profile" in n for n in a["notes"]),
      "fallback is recorded in the stamp and noted")

# --- flag OFF: byte identity -------------------------------------------------
_set_env(NIGHTSHIFT_BILLING_PROFILE="rider_interior")  # flag NOT set
a = sample_analysis()
before = json.dumps(a, sort_keys=True)
out = bc.apply_billing_convention(a)
check(out is a and json.dumps(a, sort_keys=True) == before,
      "flag OFF: apply_billing_convention is a byte-identical no-op")

# --- flag ON, default profile: quantities byte-identical ---------------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1")
a = sample_analysis()
before_q = copy.deepcopy(a)
calls = []
bc.apply_billing_convention(a, recalc=calls.append)
stamp = a.get("_billing_convention")
check(isinstance(stamp, dict) and stamp["profile"] == "default"
      and stamp["basis"] == "run" and stamp["source"] == "default",
      f"default profile stamps the convention record: {stamp}")
stripped = {k: v for k, v in a.items()
            if k not in ("_billing_convention", "notes")}
ref = {k: v for k, v in before_q.items() if k != "notes"}
check(json.dumps(stripped, sort_keys=True) == json.dumps(ref, sort_keys=True),
      "default profile changes NO quantity (byte-identity minus stamp/note)")
check(not calls, "default profile never triggers a totals rebuild")
check(any(n.startswith("[Billing Convention]") for n in a["notes"]),
      "one customer-readable note names the convention")

# --- stamp always written when ON (every profile) ----------------------------
for name in ("default", "rider_interior", "jw", "caris_faces"):
    _set_env(NIGHTSHIFT_BILLING_CONVENTION="1",
             NIGHTSHIFT_BILLING_PROFILE=name)
    a = sample_analysis()
    bc.apply_billing_convention(a)
    s = a.get("_billing_convention")
    check(isinstance(s, dict) and s.get("profile") == name
          and s.get("source") == "env",
          f"stamp written when ON for '{name}'")

# --- idempotency -------------------------------------------------------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1",
         NIGHTSHIFT_BILLING_PROFILE="rider_interior")
a = sample_analysis()
bc.apply_billing_convention(a)
once = json.dumps(a, sort_keys=True)
bc.apply_billing_convention(a)
check(json.dumps(a, sort_keys=True) == once,
      "second apply is a no-op (idempotent via the stamp)")

# --- rider_interior heights: only missing heights, measured wins -------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1",
         NIGHTSHIFT_BILLING_PROFILE="rider_interior")
a = sample_analysis()
calls = []
bc.apply_billing_convention(a, recalc=calls.append)
retail = a["floors"][0]["rooms"][0]["dimensions"]
office = a["floors"][0]["rooms"][1]["dimensions"]
studio = a["floors"][1]["rooms"][0]["dimensions"]
storage = a["floors"][2]["rooms"][0]["dimensions"]
check(retail["ceiling_height_feet"] == 12.0
      and retail["wall_area_sqft"] == 720
      and retail["_wall_height_source"] == "billing_convention:rider_interior",
      f"first-floor room with no height gets 12' and walls 60×12: {retail}")
check(office["ceiling_height_feet"] == 10.0
      and office["wall_area_sqft"] == 400.0
      and "_wall_height_source" not in office,
      "measured height wins — the 10' room is untouched")
check(studio["ceiling_height_feet"] == 9.5
      and studio["perimeter_lf"] == 44
      and studio["wall_area_sqft"] == round(44 * 9.5),
      f"upper-floor room derives perimeter from L×W and gets 9.5': {studio}")
check(storage["ceiling_height_feet"] == 0
      and storage["wall_area_sqft"] == 0,
      "room with nothing measured stays at zero (no fabrication)")
check(len(calls) == 1 and calls[0] is a,
      "totals rebuild requested exactly once when heights changed")
s = a["_billing_convention"]
check(len(s["heights_filled"]) == 2
      and {r["room_name"] for r in s["heights_filled"]} == {"Retail",
                                                            "Studio"},
      f"stamp records exactly the filled rooms: {s['heights_filled']}")

# --- floor classification ----------------------------------------------------
check(bc._classify_floor("Basement") == "basement"
      and bc._classify_floor("Cellar Level") == "basement"
      and bc._classify_floor("First Floor") == "first"
      and bc._classify_floor("1st Floor") == "first"
      and bc._classify_floor("Ground Floor") == "first"
      and bc._classify_floor("Level 01") == "first"
      and bc._classify_floor("Level 10") == "default_upper"
      and bc._classify_floor("Third Floor") == "default_upper"
      and bc._classify_floor(None) == "default_upper",
      "floor classification is conservative (Level 10 is NOT first)")

# --- faces engagement through the profile ------------------------------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1",
         NIGHTSHIFT_BILLING_PROFILE="caris_faces")
a = sample_analysis()
bc.apply_billing_convention(a)
check(a["_billing_convention"]["basis"] == "faces"
      and bc.faces_engagement(a) == 2.0,
      "caris_faces stamp engages faces_engagement at 2.0")
on, factor, src = td._wall_faces_basis(a)
check(on and factor == 2.0 and src == "profile",
      f"VME faces resolution engages via the profile: {(on, factor, src)}")

# --- precedence: explicit env flag wins over the profile ---------------------
os.environ["NIGHTSHIFT_WALL_BASIS_FACES"] = "1"
os.environ["NIGHTSHIFT_WALL_FACES_FACTOR"] = "1.5"
on, factor, src = td._wall_faces_basis(a)
check(on and factor == 1.5 and src == "env",
      f"env flag ON wins over the profile (factor from env): "
      f"{(on, factor, src)}")
os.environ["NIGHTSHIFT_WALL_BASIS_FACES"] = "0"
on, factor, src = td._wall_faces_basis(a)
check(not on and src == "env",
      "env flag explicitly 0 DISENGAGES even with a faces profile")
os.environ.pop("NIGHTSHIFT_WALL_BASIS_FACES")
os.environ.pop("NIGHTSHIFT_WALL_FACES_FACTOR")

# --- no profile, no env → off ------------------------------------------------
_set_env()
on, factor, src = td._wall_faces_basis(sample_analysis())
check(not on and src == "off",
      "no env flag + no stamped profile → faces path stays off")
# billing flag off but a stale stamp present → faces_engagement refuses
stale = {"_billing_convention": {"basis": "faces", "faces_factor": 2.0}}
check(bc.faces_engagement(stale) is None,
      "faces_engagement is inert when NIGHTSHIFT_BILLING_CONVENTION is off")

# --- exactly one multiply site (no double-apply path) ------------------------
with open(td.__file__, encoding="utf-8") as f:
    src_text = f.read()
check(src_text.count("vme_gross *= _faces") == 1
      and src_text.count("_wall_faces_basis(analysis)") == 1,
      "the faces factor has exactly one resolution call and one multiply "
      "site in Takeoff_DIRECT")

# --- factor clamp ------------------------------------------------------------
_set_env(NIGHTSHIFT_BILLING_CONVENTION="1")
big = {"_billing_convention": {"basis": "faces", "faces_factor": 9.0}}
check(bc.faces_engagement(big) == 2.5,
      "profile faces factor is clamped to the existing [1.0, 2.5] band")

# --- restore env -------------------------------------------------------------
for k, v in _SAVED.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all billing convention checks passed")
