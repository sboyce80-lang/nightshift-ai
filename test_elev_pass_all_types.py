#!/usr/bin/env python3
"""S5 (2026-08-21): elevation pass opens to all non-single-family types."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Takeoff_DIRECT as td

EXT = {"exterior_paint_sqft": 4762}


def _an(bt):
    return {"project_info": {"building_type": bt}, "exterior": {},
            "notes": []}


class TestElevPassAllTypes(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NIGHTSHIFT_ELEV_PASS_ALL_TYPES", None)

    def _run(self, bt, flag):
        os.environ["NIGHTSHIFT_ELEV_PASS_ALL_TYPES"] = flag
        a = _an(bt)
        with mock.patch.object(td, "_extract_exterior_scope",
                               return_value=dict(EXT)) as m, \
             mock.patch.object(td.time, "sleep"):
            td._maybe_run_exterior_pass(None, "/x/plans.pdf", a)
            return m.call_count, a

    def test_care_facility_fires_with_flag(self):
        calls, a = self._run("assisted living / residential care facility",
                             "1")
        self.assertEqual(calls, 1)
        self.assertEqual(a["exterior"]["exterior_paint_sqft"], 4762)

    def test_hotel_fires_with_flag(self):
        calls, _ = self._run("multi-family / hotel", "1")
        self.assertEqual(calls, 1)

    def test_flag_off_keeps_old_gate(self):
        calls, _ = self._run("assisted living / residential care facility",
                             "0")
        self.assertEqual(calls, 0)

    def test_single_family_still_excluded(self):
        calls, _ = self._run("single-family residential", "1")
        self.assertEqual(calls, 0)

    def test_commercial_unaffected(self):
        calls, _ = self._run("commercial", "0")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
