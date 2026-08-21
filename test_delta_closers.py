#!/usr/bin/env python3
"""Delta-closer fixes from the 2026-08-21 rerun findings:
- F5b: floor finish from machine-read schedule rows (prose notes vary)
- sweep review trigger: quantified unpriced scope forces manual review
- power-wash allowance line (rate-gated)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


class TestScheduleFloorFinish(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_FLOOR_FINISH_RECONCILE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_FLOOR_FINISH_RECONCILE", None)

    def _an(self, floor_finish):
        sched = [{"room_number": str(100 + i), "room_name": f"R{i}",
                  "wall_finish": "PT-1", "floor_finish": "VCT"}
                 for i in range(6)]
        sched[0]["floor_finish"] = floor_finish
        sched[0]["room_number"] = "101"
        return {
            "room_finish_schedule": sched,
            "notes": [],
            "floors": [{"floor_name": "1", "rooms": [
                {"room_id": "101", "room_number": "101", "room_name": "R0",
                 "in_scope": True, "notes": "",
                 "dimensions": {"floor_area_sqft": 400},
                 "elements": {"concrete_floor_sqft": 0}, "materials": {}}]}],
            "aggregated_totals": {},
        }

    def test_ep_code_in_schedule_row_wires_floor(self):
        out = td._reconcile_floor_finishes(self._an("EP-1 Epoxy"))
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 400)

    def test_sealed_concrete_row(self):
        out = td._reconcile_floor_finishes(self._an("Sealed Concrete"))
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 400)

    def test_vct_row_no_change(self):
        out = td._reconcile_floor_finishes(self._an("VCT"))
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 0)

    def test_polished_row_rfi_only(self):
        out = td._reconcile_floor_finishes(self._an("PC-1 Polished Concrete"))
        self.assertEqual(out["floors"][0]["rooms"][0]["elements"]
                         ["concrete_floor_sqft"], 0)
        self.assertTrue(any("Polished concrete" in str(n)
                            for n in out["notes"]))


class TestPowerWashAllowance(unittest.TestCase):
    SWEEP = {"findings": [{
        "category": "exterior", "item": "Exterior facade cleaning scope",
        "detail": "Power washing scope defined: CLEAN ENTIRETY OF BUILDING "
                  "FACADE +/- 24,652 SF. Sheet A2.01.",
        "sheet": "A2.01"}]}

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_POWER_WASH_ALLOWANCE", None)

    def _run(self, flag, with_rate):
        os.environ["NIGHTSHIFT_POWER_WASH_ALLOWANCE"] = flag
        pm = {k: dict(v) if isinstance(v, dict) else v
              for k, v in td.PRICING_MODEL.items()}
        if with_rate:
            pm["power_washing"] = {"unit": "sqft", "markup": 0.06,
                "tiers": [{"min_qty": 0, "max_qty": None,
                           "rate": 0.35}]}
        analysis = {"_scope_sweep": dict(self.SWEEP)}
        return td.calculate_costs({"total_paintable_wall_sqft": 100},
                                  building_type="commercial",
                                  analysis=analysis,
                                  pricing_model_override=pm)

    def test_rate_plus_flag_prices_allowance(self):
        ce = self._run("1", True)
        pw = [l for l in ce["line_items"]
              if str(l.get("item", "")).startswith("Power Washing")]
        self.assertEqual(len(pw), 1)
        self.assertEqual(pw[0]["qty"], 24652)
        self.assertGreater(pw[0]["total"], 0)

    def test_no_rate_no_line(self):
        ce = self._run("1", False)
        self.assertFalse([l for l in ce["line_items"]
                          if "Power Washing" in str(l.get("item", ""))])

    def test_flag_off_no_line(self):
        ce = self._run("0", True)
        self.assertFalse([l for l in ce["line_items"]
                          if "Power Washing" in str(l.get("item", ""))])


if __name__ == "__main__":
    unittest.main(verbosity=2)
