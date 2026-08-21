#!/usr/bin/env python3
"""Sweep→schedule rescue (window-count fix #1, 2026-08-21).

The sweep locates door/window schedule pages the pre-scans missed; the
rescue extracts them and applies the authoritative override path."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td

RESCUED = {
    "window_schedule": {
        "total_windows": 161, "windows_painted_interior": 0,
        "windows_owner_provided": 0, "windows_factory_finished": 0,
        "windows_field_paintable_wood": 161,
        "windows_with_casing": 161, "windows_with_apron": 150,
        "windows_with_stool_sill": 161, "windows_with_wood_return": 0,
        "windows_with_drywall_return": 0, "window_types": []},
    "door_schedule": {"total_doors_full_paint": 197,
                      "total_doors_hm_panel": 0,
                      "door_marks_counted": []},
}


def _an():
    return {
        "project_info": {"building_type": "commercial"},
        "aggregated_totals": {"total_windows_painted_interior": 0,
                              "total_doors_full_paint": 62},
        "floors": [], "notes": [],
        "_scope_sweep": {"pages_swept": [
            {"file": "plans.pdf", "page": 9, "page_kind": "window_schedule"},
            {"file": "plans.pdf", "page": 9, "page_kind": "door_schedule"},
            {"file": "plans.pdf", "page": 2, "page_kind": "elevation"},
        ]},
    }


class TestSweepScheduleRescue(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE", None)

    def test_flag_off_noop(self):
        os.environ["NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE"] = "0"
        a = _an()
        with mock.patch.object(td, "analyze_schedule_images_consensus") as m:
            td._rescue_swept_schedules(None, ["/x/plans.pdf"], a)
            m.assert_not_called()
        self.assertNotIn("schedule_data", a)

    def test_rescues_and_applies_overrides(self):
        os.environ["NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE"] = "1"
        a = _an()
        with mock.patch.object(td, "analyze_schedule_images_consensus",
                               return_value=dict(RESCUED)) as m:
            td._rescue_swept_schedules(None, ["/x/plans.pdf"], a)
            m.assert_called_once()
            # 0-based page index passed
            self.assertEqual(m.call_args[0][2], [8])
        sd = a["schedule_data"]
        self.assertEqual(sd["window_schedule"]["total_windows"], 161)
        self.assertTrue(a["has_window_schedule"])
        self.assertTrue(a["has_door_schedule"])
        agg = a["aggregated_totals"]
        # override path pushed component counts + authoritative door count
        self.assertEqual(agg.get("total_window_casings_painted"), 161)
        self.assertEqual(agg.get("total_doors_full_paint"), 197)
        self.assertTrue(any("Sweep Schedule Rescue" in str(n)
                            for n in a["notes"]))

    def test_existing_schedule_not_overwritten(self):
        os.environ["NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE"] = "1"
        a = _an()
        a["schedule_data"] = {"window_schedule": {"total_windows": 40},
                              "door_schedule": {"total_doors_full_paint": 9}}
        with mock.patch.object(td, "analyze_schedule_images_consensus") as m:
            td._rescue_swept_schedules(None, ["/x/plans.pdf"], a)
            m.assert_not_called()
        self.assertEqual(a["schedule_data"]["window_schedule"]
                         ["total_windows"], 40)

    def test_extraction_failure_nonfatal(self):
        os.environ["NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE"] = "1"
        a = _an()
        with mock.patch.object(td, "analyze_schedule_images_consensus",
                               side_effect=RuntimeError("boom")):
            out = td._rescue_swept_schedules(None, ["/x/plans.pdf"], a)
        self.assertIsInstance(out, dict)
        self.assertNotIn("schedule_data", out)


class TestTextScanChannel(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE", None)

    def test_measured_page_schedule_located_by_text(self):
        os.environ["NIGHTSHIFT_SWEEP_SCHEDULE_RESCUE"] = "1"
        a = {"project_info": {"building_type": "commercial"},
             "aggregated_totals": {}, "floors": [], "notes": [],
             "_scope_sweep": {"pages_swept": []}}  # sweep found nothing
        with mock.patch.object(td, "_locate_schedule_pages_by_text",
                               return_value={"door_schedule": [],
                                             "window_schedule": [7]}),              mock.patch.object(td, "analyze_schedule_images_consensus",
                               return_value=dict(RESCUED)) as m:
            td._rescue_swept_schedules(None, ["/x/plans.pdf"], a)
            m.assert_called_once()
            self.assertEqual(m.call_args[0][2], [7])
        self.assertTrue(a["has_window_schedule"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
