#!/usr/bin/env python3
"""F2b (2026-08-20 JW batch): window trim ops priced from schedule components.

The schedule scan counted paintable components (casings/stools/aprons) but
no pricing line consumed them — window trim was $0 on all 5 batch jobs
while JW priced stool&apron&casing per window."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td

AGG = {
    "total_paintable_wall_sqft": 1000,
    "total_windows_painted_interior": 0,   # sashes policy-excluded
    "total_window_casings_painted": 161,
    "total_window_stools_painted": 161,
    "total_window_aprons_painted": 150,
    "total_window_wood_returns_painted": 0,
}


def _trim_line(ce):
    return next(li for li in ce["line_items"]
                if str(li.get("item", "")).startswith("Window Trim"))


class TestWindowTrimScope(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_WINDOW_TRIM_SCOPE", None)

    def test_flag_off_prices_zero(self):
        os.environ["NIGHTSHIFT_WINDOW_TRIM_SCOPE"] = "0"
        ce = td.calculate_costs(dict(AGG), building_type="commercial")
        line = _trim_line(ce)
        self.assertEqual(line["qty"], 0)
        self.assertEqual(line["total"], 0)

    def test_flag_on_prices_max_component_count(self):
        os.environ["NIGHTSHIFT_WINDOW_TRIM_SCOPE"] = "1"
        ce = td.calculate_costs(dict(AGG), building_type="commercial")
        line = _trim_line(ce)
        # one window with several components = ONE op → max(161,161,150)=161
        self.assertEqual(line["qty"], 161)
        self.assertGreater(line["total"], 0)

    def test_sash_line_stays_zero_under_policy(self):
        os.environ["NIGHTSHIFT_WINDOW_TRIM_SCOPE"] = "1"
        ce = td.calculate_costs(dict(AGG), building_type="commercial")
        sash = next(li for li in ce["line_items"]
                    if str(li.get("item", "")).startswith("Windows (Interior"))
        self.assertEqual(sash["qty"], 0)

    def test_no_components_no_charge(self):
        os.environ["NIGHTSHIFT_WINDOW_TRIM_SCOPE"] = "1"
        agg = {k: (0 if k.startswith("total_window_") else v)
               for k, v in AGG.items()}
        ce = td.calculate_costs(agg, building_type="commercial")
        self.assertEqual(_trim_line(ce)["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
