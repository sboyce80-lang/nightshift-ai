"""Sheet-index-based floor-plan detection (NIGHTSHIFT_SHEET_INDEX_TITLES).

Northwell/Phelps class of failure: the plan sheets' title blocks carry no
extractable title text (top-font lines are just the sheet number), so
select_floor_plan_pages returned [] and VME never ran — walls shipped from
vision at −34%. The drawing index (G000 "SHEET LIST") names every sheet;
these tests cover parsing it as a table and classifying pages by their
index title, plus the finish-plan fallback for a floor whose construction
plan is listed in the index but absent from the submitted set.
"""
import os
import unittest

import fitz

import vme_attribution as va


def _make_set(tmpdir):
    """3-page synthetic set: index page + two mute-title plan sheets."""
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "SHEET LIST", fontsize=14)
    # real cover sheets are busy: notes/legend text outranks the 9pt index
    # rows in the top-font ranking (Northwell G000 classifies by its
    # headers, not its rows)
    # the parser separates columns by x-gap (>=24pt); notes columns in real
    # indexes sit far from the title column
    for n in range(14):
        p.insert_text((420, 110 + n * 20),
                      f"GENERAL NOTE {n + 1}: SEE SPECIFICATIONS",
                      fontsize=12)
    rows = [
        ("G000", "COVER SHEET & SHEET LIST"),
        ("A100", "FIRST FLOOR CONSTRUCTION PLAN"),
        ("A101", "SECOND FLOOR FINISH PLAN"),
        ("A102", "SECOND FLOOR FURNITURE PLAN"),
        ("M101", "MECHANICAL NEW WORK PLAN - FIRST FLOOR"),
    ]
    y = 110
    for num, title in rows:
        p.insert_text((72, y), num, fontsize=9)
        p.insert_text((150, y), title, fontsize=9)
        y += 14
    # sheet pages whose only text is the sheet number (mute title block)
    for num in ("A100", "A101"):
        pg = doc.new_page(width=612, height=792)
        pg.insert_text((540, 760), num, fontsize=18)
    path = os.path.join(tmpdir, "synthetic_set.pdf")
    doc.save(path)
    doc.close()
    return path


class SheetIndexTitleTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = _make_set(self.tmp.name)
        self._old = os.environ.get("NIGHTSHIFT_SHEET_INDEX_TITLES")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("NIGHTSHIFT_SHEET_INDEX_TITLES", None)
        else:
            os.environ["NIGHTSHIFT_SHEET_INDEX_TITLES"] = self._old
        self.tmp.cleanup()

    def test_index_parse(self):
        titles = va.sheet_index_titles([self.pdf])
        self.assertEqual(titles.get("A100"), "FIRST FLOOR CONSTRUCTION PLAN")
        self.assertEqual(titles.get("A101"), "SECOND FLOOR FINISH PLAN")
        self.assertEqual(titles.get("M101"),
                         "MECHANICAL NEW WORK PLAN - FIRST FLOOR")

    def test_flag_off_unchanged(self):
        os.environ["NIGHTSHIFT_SHEET_INDEX_TITLES"] = "0"
        self.assertEqual(va.select_floor_plan_pages([self.pdf]), [])

    def test_index_route_claims_mute_plan_sheet(self):
        os.environ["NIGHTSHIFT_SHEET_INDEX_TITLES"] = "1"
        pages = va.select_floor_plan_pages([self.pdf])
        by_sheet = {p["sheet"]: p for p in pages}
        self.assertIn("A100", by_sheet)
        self.assertEqual(by_sheet["A100"]["floors"], ["1"])
        self.assertEqual(by_sheet["A100"]["src"], "index")

    def test_finish_fallback_only_for_uncovered_floor(self):
        # floor 2 has no construction plan in the set -> its finish plan
        # (A101) is the only geometry carrier and gets claimed
        os.environ["NIGHTSHIFT_SHEET_INDEX_TITLES"] = "1"
        pages = va.select_floor_plan_pages([self.pdf])
        by_sheet = {p["sheet"]: p for p in pages}
        self.assertIn("A101", by_sheet)
        self.assertEqual(by_sheet["A101"]["floors"], ["2"])
        self.assertEqual(by_sheet["A101"]["src"], "index-finish")
        # the index page itself must never be claimed
        self.assertNotIn("G000", by_sheet)
        self.assertEqual({p["page"] for p in pages}, {1, 2})

    def test_finish_fallback_yields_to_real_plan(self):
        # same set plus a REAL floor-2 plan sheet -> fallback must not fire
        doc = fitz.open(self.pdf)
        pg = doc.new_page(width=612, height=792)
        pg.insert_text((300, 400), "SECOND FLOOR PLAN", fontsize=22)
        path = os.path.join(self.tmp.name, "with_plan2.pdf")
        doc.save(path)
        doc.close()
        os.environ["NIGHTSHIFT_SHEET_INDEX_TITLES"] = "1"
        pages = va.select_floor_plan_pages([path])
        srcs = {p["src"] for p in pages}
        self.assertNotIn("index-finish", srcs)
        floors = {f for p in pages for f in p["floors"]}
        self.assertEqual(floors, {"1", "2"})


class TwoColumnIndexTests(unittest.TestCase):
    def test_second_column_pair_not_absorbed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            doc = fitz.open()
            p = doc.new_page(width=792, height=612)
            p.insert_text((60, 60), "DRAWING INDEX", fontsize=14)
            # one visual row carrying two number/title pairs
            p.insert_text((60, 100), "A100", fontsize=9)
            p.insert_text((120, 100), "FIRST FLOOR PLAN", fontsize=9)
            p.insert_text((420, 100), "E101", fontsize=9)
            p.insert_text((480, 100), "ELECTRICAL POWER PLAN", fontsize=9)
            path = os.path.join(tmp, "twocol.pdf")
            doc.save(path)
            doc.close()
            titles = va.sheet_index_titles([path])
            self.assertEqual(titles.get("A100"), "FIRST FLOOR PLAN")
            self.assertEqual(titles.get("E101"), "ELECTRICAL POWER PLAN")


if __name__ == "__main__":
    unittest.main()
