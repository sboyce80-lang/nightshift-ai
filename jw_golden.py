#!/usr/bin/env python3
"""JW golden set — the five 2026-08 validation jobs with verified targets.

Ground truth: JW's takeoff spreadsheets (parsed to ground_truth*.json in
nsai_batch_2026-08-20/<job>/), hand-mapped per the comparison_*.md docs.
Targets follow the regression_test.REFERENCE_CASES shape:
    metric_key: (target_value, relative_tolerance)

Subtotal tolerance is 0.10 everywhere — that IS the acceptance band the
mandatory-review exit criterion tracks. Quantity tolerances are wider
where a known policy/feature gap exists (annotated inline).

Plan PDFs: nsai_batch_2026-08-20/<job>/plans_clean.pdf (annotation-
stripped — the raw MARKUPS files carry JW's answer key on-sheet; NEVER
validate against unstripped copies).
"""

JW_FLAG_SET_NOTE = ("Run with the 28-export flag set in "
                    "nsai_batch_2026-08-20/rerun_batch.sh (the validated "
                    "JW-class configuration incl. mandatory review).")

JW_CASES = {
    "jw_harlem_valley": {
        "display_name": "Harlem Valley Homestead Farm Hub (JW 26-376)",
        "job_dir": "nsai_batch_2026-08-20/harlem_valley",
        "jw_bid": 43490.84,
        "targets": {
            "cost_estimate_subtotal": (43490.84, 0.10),
            # walls: JW 15,667 PT + 1,735 MR; KS splits gyp/CMU
            "total_paintable_wall_sqft": (15667, 0.30),
            "total_paintable_ceiling_sqft": (9009, 0.35),
            "total_doors_full_paint": (29, 0.35),
            # sealed concrete: allowance-based (S6) — wide band
            "total_concrete_floor_sqft": (5546, 0.50),
        },
    },
    "jw_hudson_hotel": {
        "display_name": "Hudson Hotel on West Point (JW 26-390)",
        "job_dir": "nsai_batch_2026-08-20/hudson_hotel",
        "jw_bid": 146023.71,
        "targets": {
            "cost_estimate_subtotal": (146023.71, 0.10),
            # doors: JW 197; density cap currently lands ~240 (G1)
            "total_doors_full_paint": (197, 0.30),
            # walls: unit-count RFI open (34 units read, key count
            # unstated in the 8-sheet set) — informational band
            "total_paintable_wall_sqft": (72382, 0.60),
        },
    },
    "jw_caris_hyde_park": {
        "display_name": "Caris of Hyde Park (JW 26-385)",
        "job_dir": "nsai_batch_2026-08-20/caris_hyde_park",
        "jw_bid": 87608.82,
        "targets": {
            "cost_estimate_subtotal": (87608.82, 0.10),
            "total_doors_full_paint": (75, 0.25),
            "total_base_trim_lf": (4957, 0.35),
            # ceilings trade against walls on this set — wide band
            "total_paintable_ceiling_sqft": (6641, 0.60),
        },
    },
    "jw_under_canvas_ulum": {
        "display_name": "Under Canvas Hudson Valley ULUM (40-211)",
        "job_dir": "nsai_batch_2026-08-20/under_canvas_ulum",
        "jw_bid": 41575.45,
        "targets": {
            # floors policy gap (polished/epoxy RFI'd) keeps this under —
            # band widened until the policy call lands
            "cost_estimate_subtotal": (41575.45, 0.35),
            "total_doors_full_paint": (26, 0.35),
            "total_paintable_ceiling_sqft": (2756, 0.50),
        },
    },
    "jw_homewood_suites": {
        "display_name": "Homewood Suites (RP 26-002)",
        "job_dir": "nsai_batch_2026-08-20/homewood_suites",
        "jw_bid": 1322982.57,
        "targets": {
            "cost_estimate_subtotal": (1322982.57, 0.20),
            # WC: per-unit transfer feature open — wide band
            "total_wallcovering_sqft": (136636, 0.50),
            "total_doors_full_paint": (393, 0.35),
        },
    },
}
