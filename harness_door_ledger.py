#!/usr/bin/env python3
"""Door-ledger golden harness (spec: golden harness BEFORE any pipeline code).

Scores door_ledger.build_door_ledger against every locally-stored set with
a known drawn-door count. Targets are DRAWN doors (what's countable on the
sheets) — template×unit multiplication stays downstream, so Homewood-class
jobs (393 priced from 100-ish drawn) validate mode/detector behavior, not
count equality.

Free and offline: no API calls, reads only local plan PDFs.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from door_ledger import build_door_ledger, parse_schedule_pages, \
    count_symbol_doors  # noqa: E402

REPO = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo"
BATCH = os.path.join(REPO, "nsai_batch_2026-08-20")

# target_drawn: doors countable on the submitted sheets (JW's own counts
# where JW had no schedule either — they counted symbols too).
# expect_mode: what a correct ledger should decide for this set.
CASES = [
    {"id": "harlem", "pdfs": [os.path.join(BATCH, "harlem_valley",
                                           "plans_clean.pdf")],
     "target_drawn": 29, "expect_mode": "symbols"},
    {"id": "ulum", "pdfs": [os.path.join(BATCH, "under_canvas_ulum",
                                         "plans_clean.pdf")],
     "target_drawn": 26, "expect_mode": "symbols",
     "note": "schedule page A-603 is CURVES (2 text marks) — mode A must "
             "NOT claim this set"},
    {"id": "northwell", "pdfs": [os.path.join(
        REPO, "nsai_jw_northwell_2026-08-31", "plans_clean.pdf")],
     "target_drawn": 78, "expect_mode": "symbols",
     "note": "no schedule issued; JW counted 78 from the sheets"},
    {"id": "hudson", "pdfs": [os.path.join(BATCH, "hudson_hotel",
                                           "plans_clean.pdf")],
     "target_drawn": None, "expect_mode": None,
     "note": "8-page set, no schedule; typicals x units — ledger should "
             "abstain or report honestly, target is mode behavior only"},
    {"id": "caris", "pdfs": [os.path.join(BATCH, "caris_hyde_park",
                                          "plans_clean.pdf")],
     "target_drawn": 75, "expect_mode": None,
     "note": "JW priced 75; mode unknown — recon case"},
    {"id": "homewood", "pdfs": [os.path.join(BATCH, "homewood_suites",
                                             "plans_clean.pdf")],
     "target_drawn": None, "expect_mode": None,
     "note": "393 priced = template x units; drawn count unknown — recon"},
]


def main():
    results = []
    for c in CASES:
        missing = [p for p in c["pdfs"] if not os.path.exists(p)]
        if missing:
            print(f"SKIP {c['id']}: missing {missing}")
            continue
        led = build_door_ledger(c["pdfs"])
        sched = parse_schedule_pages(c["pdfs"])
        sym = count_symbol_doors(c["pdfs"])
        row = {
            "id": c["id"], "target_drawn": c["target_drawn"],
            "mode": led["mode"], "count": led["count"],
            "sched_entries": len(sched["entries"]),
            "tag_marks": len(sym["marks"]),
            "arc_count": sym["arc_count"],
            "per_page": sym["per_page"],
            "detector": led.get("detector"),
        }
        if c["target_drawn"] and led["count"]:
            row["delta_pct"] = round(
                (led["count"] - c["target_drawn"]) / c["target_drawn"] * 100,
                1)
        results.append(row)
        print(f"\n=== {c['id']} (target {c['target_drawn']}, "
              f"expect {c['expect_mode']}) ===")
        print(f"  mode={led['mode']} count={led['count']} "
              f"detector={led.get('detector')} "
              f"delta={row.get('delta_pct', 'n/a')}%")
        print(f"  modeA entries={len(sched['entries'])} "
              f"headers={sched['headers_seen']}")
        print(f"  modeB tag_marks={len(sym['marks'])} "
              f"arcs={sym['arc_count']}")
        for pp in sym["per_page"][:12]:
            print(f"    p{pp['page']+1}: tags={pp['tag_marks']} "
                  f"arcs={pp['arcs']}")
    out = os.path.join(HERE, "door_ledger_harness_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
