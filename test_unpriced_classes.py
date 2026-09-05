#!/usr/bin/env python3
"""Unpriced-classes pricing (NIGHTSHIFT_PRICE_UNPRICED_CLASSES).

Locks in: flag OFF leaves calculate_costs byte-identical (no General
Requirements line, WC install at the legacy $9.00 rate, legacy
total_windows fallback intact); flag ON prices confirmed interior window
paint from total_windows_painted_interior only, holds unconfirmed
windows (total_windows_all with no confirmed-painted count) at $0 with a
Windows RFI, prices wallcovering install at the unit-corrected $/SF rate
(config WC_INSTALL_RATE_PER_SF = $9.00/LY ÷ 13.5 SF/LY ≈ $0.67/SF — the
quantity is SF, the old 9.00 was a per-lineal-yard trade quote applied
per SF), keeps the explicit NIGHTSHIFT_WC_INSTALL_RATE env override
winning, and emits a commercial-only General Requirements percentage
line (config GENERAL_REQUIREMENTS_PCT of the trade subtotal, no markup)
that is included in the returned subtotal.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Deterministic posture: clear every flag this suite touches or that
# could perturb the fixture's pricing path.
for _k in (
        "NIGHTSHIFT_PRICE_UNPRICED_CLASSES",
        "NIGHTSHIFT_WC_INSTALL_RATE",
        "NIGHTSHIFT_WC_INSTALL_RATE_PER_SF",
        "NIGHTSHIFT_GENERAL_REQUIREMENTS_PCT",
        "NIGHTSHIFT_WC_WALL_DEDUCT",
        "NIGHTSHIFT_WINDOW_TRIM_SCOPE",
        "NIGHTSHIFT_WINDOW_SASH_OPS",
        "NIGHTSHIFT_EXTENDED_SCOPE",
        "NIGHTSHIFT_LEVEL5_ALLOWANCE",
        "NIGHTSHIFT_LEVEL5_EXCLUDE",
        "NIGHTSHIFT_POWER_WASH_ALLOWANCE",
        "NIGHTSHIFT_EXT_PRICING_FIX",
        "NIGHTSHIFT_ALLOWANCE_LINES"):
    os.environ.pop(_k, None)

import Takeoff_DIRECT as T  # noqa: E402
import config as _cfg  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def fixture_agg():
    return {
        "total_paintable_wall_sqft": 8000,
        "total_paintable_ceiling_sqft": 3000,
        "total_base_trim_lf": 400,
        "total_doors_full_paint": 20,
        "total_windows_painted_interior": 30,
        "total_windows_all": 30,
        "total_wallcovering_sqft": 1000,
    }


def fixture_analysis():
    return {"floors": [], "notes": [],
            "project_info": {"building_type": "commercial office renovation"}}


def run(agg=None, building_type="commercial office renovation",
        analysis=None, project_info=None):
    return T.calculate_costs(
        dict(agg if agg is not None else fixture_agg()),
        exterior={},
        building_type=building_type,
        project_info=dict(project_info or {}),
        analysis=analysis if analysis is not None else fixture_analysis(),
    )


def find_line(costs, prefix):
    for li in costs["line_items"]:
        if li["item"].startswith(prefix):
            return li
    return None


print("unpriced-classes pricing checks")

# --- Flag OFF: two identical runs, byte-identical output. ---
os.environ.pop("NIGHTSHIFT_PRICE_UNPRICED_CLASSES", None)
off1 = run()
off2 = run()
check(json.dumps(off1, sort_keys=True) == json.dumps(off2, sort_keys=True),
      "flag OFF determinism: two identical runs produce identical output")
check(find_line(off1, "General Requirements") is None,
      "flag OFF baseline: no General Requirements line is emitted")
_wc_off = find_line(off1, "Wallcovering Install")
check(_wc_off is not None and "@ $9.00" in _wc_off["item"]
      and abs(_wc_off["cost"] - 9000.0) < 0.01,
      "flag OFF baseline: WC install stays at the legacy $9.00/SF rate "
      f"(got {_wc_off})")
_win_off = find_line(off1, "Windows (Interior Paint)")
check(_win_off is not None and _win_off["qty"] == 30,
      "flag OFF baseline: confirmed windows still price (30 EA)")

# Legacy fallback: no painted_interior key, only total_windows -> priced OFF.
_legacy_agg = fixture_agg()
del _legacy_agg["total_windows_painted_interior"]
_legacy_agg["total_windows"] = 25
_legacy_off = run(agg=_legacy_agg)
check(find_line(_legacy_off, "Windows (Interior Paint)")["qty"] == 25,
      "flag OFF baseline: legacy total_windows fallback still prices 25 EA")

# --- Flag ON: same fixture. ---
os.environ["NIGHTSHIFT_PRICE_UNPRICED_CLASSES"] = "1"
on = run()

# Windows: confirmed count priced at the config tiered rate (26+ -> $425).
_win_on = find_line(on, "Windows (Interior Paint)")
check(_win_on is not None and _win_on["qty"] == 30
      and abs(_win_on["cost"] - 30 * 425.00) < 0.01,
      f"flag ON windows: 30 confirmed EA price at $425 tier (got {_win_on})")

# WC unit correctness: install rate is the converted $/SF rate.
_exp_wc_rate = float(getattr(_cfg, "WC_INSTALL_RATE_PER_SF", 0.67))
_wc_on = find_line(on, "Wallcovering Install")
check(_wc_on is not None
      and abs(_wc_on["cost"] - 1000 * _exp_wc_rate) < 0.01
      and f"@ ${_exp_wc_rate:.2f}" in _wc_on["item"],
      f"flag ON WC unit fix: 1,000 SF at ${_exp_wc_rate:.2f}/SF "
      f"(per-LY 9.00 / 13.5), not $9.00/SF (got {_wc_on})")

# Explicit env override still wins over the flag's config rate.
os.environ["NIGHTSHIFT_WC_INSTALL_RATE"] = "0.50"
_wc_env_run = run()
_wc_env_line = find_line(_wc_env_run, "Wallcovering Install")
check(_wc_env_line is not None
      and abs(_wc_env_line["cost"] - 500.0) < 0.01,
      "flag ON WC env override: NIGHTSHIFT_WC_INSTALL_RATE=0.50 wins "
      f"(got {_wc_env_line})")
os.environ.pop("NIGHTSHIFT_WC_INSTALL_RATE", None)

# General requirements: commercial percentage line, in the subtotal, no markup.
_gr = find_line(on, "General Requirements")
_gr_pct = float(getattr(_cfg, "GENERAL_REQUIREMENTS_PCT", 0.07))
check(_gr is not None, "flag ON gen-req: General Requirements line emitted "
                       "on commercial")
if _gr is not None:
    _trade = round(on["subtotal"] - _gr["total"], 2)
    check(abs(_gr["total"] - round(_trade * _gr_pct, 2)) <= 0.02,
          f"flag ON gen-req math: line = {_gr_pct:.0%} of trade subtotal "
          f"(trade ${_trade:,.2f}, GR ${_gr['total']:,.2f})")
    check(_gr["markup"] == 0.0,
          "flag ON gen-req markup: LS line carries no markup")
    check(abs(on["subtotal"] - (_trade + _gr["total"])) < 0.01,
          "flag ON gen-req subtotal: GR total is included in the returned "
          "subtotal")

# Gen-req is commercial-only.
_res = run(building_type="multi-family residential",
           analysis={"floors": [], "notes": [],
                     "project_info": {"building_type":
                                      "multi-family residential"}})
check(find_line(_res, "General Requirements") is None,
      "flag ON gen-req scope: no General Requirements line on residential")

# Unconfirmed windows: total_windows_all (and even legacy total_windows)
# without a confirmed-painted count -> $0 + Windows RFI, never priced.
_unconf_agg = fixture_agg()
del _unconf_agg["total_windows_painted_interior"]
_unconf_agg["total_windows"] = 25       # legacy fallback bait
_unconf_agg["total_windows_all"] = 25
_unconf_analysis = fixture_analysis()
_unconf = run(agg=_unconf_agg, analysis=_unconf_analysis)
_win_unconf = find_line(_unconf, "Windows (Interior Paint)")
check(_win_unconf is not None and _win_unconf["qty"] == 0
      and _win_unconf["total"] == 0.0,
      "flag ON unconfirmed windows: not priced (qty 0, $0) despite "
      "total_windows_all=25 and legacy total_windows=25")
_rfis = _unconf_analysis.get("rfi_items", [])
check(any(isinstance(r, dict) and r.get("category") == "Windows"
          for r in _rfis),
      "flag ON unconfirmed windows: Windows RFI raised")
# RFI is de-duplicated across repeat pricing runs.
_unconf2 = run(agg=_unconf_agg, analysis=_unconf_analysis)
check(sum(1 for r in _unconf_analysis.get("rfi_items", [])
          if isinstance(r, dict) and r.get("category") == "Windows") == 1,
      "flag ON unconfirmed windows: RFI de-duplicated on rerun")

# Confirmed-zero windows on a windowless job: no RFI noise.
_nowin_agg = fixture_agg()
_nowin_agg["total_windows_painted_interior"] = 0
_nowin_agg["total_windows_all"] = 0
_nowin_analysis = fixture_analysis()
run(agg=_nowin_agg, analysis=_nowin_analysis)
check(not _nowin_analysis.get("rfi_items"),
      "flag ON windowless job: no Windows RFI when total_windows_all is 0")

# --- Flag OFF again: output identical to the original OFF baseline. ---
os.environ.pop("NIGHTSHIFT_PRICE_UNPRICED_CLASSES", None)
off3 = run()
check(json.dumps(off3, sort_keys=True) == json.dumps(off1, sort_keys=True),
      "flag round-trip: OFF after ON reproduces the OFF baseline exactly")

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all unpriced-classes checks passed")
