#!/usr/bin/env python3
"""G1-G3 gates (2026-08-22): door density, exterior evidence, L5 exclude."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td


class TestDoorDensity(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_DOOR_DENSITY_RECONCILE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_DOOR_DENSITY_RECONCILE", None)

    def _an(self, doors_per_room, n_rooms=5, mult=34):
        rooms = [{"room_id": f"t{i}", "room_name": f"King 1BR - R{i}",
                  "unit_type": "King One Bedroom Suite",
                  "unit_multiplier": mult, "in_scope": True,
                  "dimensions": {},
                  "elements": {"doors_full_paint": doors_per_room}}
                 for i in range(n_rooms)]
        total = doors_per_room * n_rooms * mult
        return {"floors": [{"floor_name": "T", "rooms": rooms}],
                "aggregated_totals": {"total_doors_full_paint": total},
                "notes": []}

    def test_hudson_shape_caps_at_rooms_plus_two(self):
        # 11.2 doors/unit on a 5-room template → cap 7/unit
        a = self._an(doors_per_room=2.25, n_rooms=5, mult=34)  # 11.25/unit
        out = td._reconcile_door_density(a)
        agg = out["aggregated_totals"]["total_doors_full_paint"]
        self.assertAlmostEqual(agg, 7 * 34, delta=2)
        self.assertTrue(out.get("manual_review_required"))

    def test_sane_density_untouched(self):
        a = self._an(doors_per_room=1, n_rooms=5, mult=34)  # 5/unit
        out = td._reconcile_door_density(a)
        self.assertEqual(out["aggregated_totals"]
                         ["total_doors_full_paint"], 170)
        self.assertFalse(out.get("manual_review_required"))

    def test_schedule_authoritative_skips(self):
        a = self._an(doors_per_room=3)
        a["_schedule_authoritative_counts"] = {"total_doors_full_paint": 99}
        out = td._reconcile_door_density(a)
        self.assertEqual(out["_door_density_reconcile"].get("noop"),
                         "schedule_authoritative")

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_DOOR_DENSITY_RECONCILE"] = "0"
        a = self._an(doors_per_room=3)
        out = td._reconcile_door_density(a)
        self.assertNotIn("_door_density_reconcile", out)


class TestExteriorEvidenceGate(unittest.TestCase):
    def setUp(self):
        os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"

    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE", None)

    def _run(self, ext_data):
        a = {"project_info": {"building_type": "commercial"},
             "exterior": {}, "notes": []}
        with mock.patch.object(td, "_extract_exterior_scope",
                               return_value=dict(ext_data)), \
             mock.patch.object(td.time, "sleep"):
            td._maybe_run_exterior_pass(None, "/x/p.pdf", a)
        return a

    def test_no_evidence_passes_through_then_pricing_tier_zeroes(self):
        # 2026-08-25: the in-pass zero is retired — quantities pass
        # through so the per-item pricing-tier gate (same flag) can
        # resolve them (negative veto -> allowance, positive keep, or
        # zero+RFI). The end state for a no-evidence job is unchanged.
        a = self._run({"exterior_paint_sqft": 16000,
                       "cornice_lf": 200,
                       "paint_evidence": "NONE",
                       "notes": "power wash and tuck-point only"})
        self.assertEqual(a["exterior"].get("exterior_paint_sqft", 0), 16000)
        a = td._enforce_exterior_evidence(a)
        self.assertEqual(a["exterior"].get("exterior_paint_sqft", 0), 0)
        self.assertTrue(any("Exterior" in str(r.get("category"))
                            for r in (a.get("_pre_pricing_rfis") or []))
                        or any("Exterior Evidence" in str(n)
                               for n in a.get("notes", [])))

    def test_quoted_paint_evidence_passes(self):
        a = self._run({"exterior_paint_sqft": 4762,
                       "paint_evidence":
                       "EXTERIOR SIDING PAINT: pressure wash, scrape and "
                       "PAINT all fiber cement siding SW7674",
                       "notes": "siding painted per keynote"})
        self.assertEqual(a["exterior"]["exterior_paint_sqft"], 4762)

    def test_structural_counts_pass_without_evidence(self):
        a = self._run({"exterior_door_count": 6, "railing_lf": 40,
                       "paint_evidence": "NONE", "notes": ""})
        self.assertEqual(a["exterior"]["exterior_door_count"], 6)
        self.assertEqual(a["exterior"]["railing_lf"], 40)


class TestLevel5Exclude(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_LEVEL5_EXCLUDE", None)

    def test_exclude_zeroes_line_with_note(self):
        os.environ["NIGHTSHIFT_LEVEL5_EXCLUDE"] = "1"
        analysis = {"notes": []}
        ce = td.calculate_costs({"total_level_5_finish_sqft": 22589},
                                building_type="commercial",
                                analysis=analysis)
        l5 = next(l for l in ce["line_items"]
                  if "Level 5" in str(l.get("item")))
        self.assertEqual(l5["total"], 0)
        self.assertTrue(any("EXCLUDED" in str(n)
                            for n in analysis["notes"]))

    def test_default_prices_normally(self):
        os.environ["NIGHTSHIFT_LEVEL5_EXCLUDE"] = "0"
        ce = td.calculate_costs({"total_level_5_finish_sqft": 1000},
                                building_type="commercial")
        l5 = next(l for l in ce["line_items"]
                  if "Level 5" in str(l.get("item")))
        self.assertGreater(l5["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
