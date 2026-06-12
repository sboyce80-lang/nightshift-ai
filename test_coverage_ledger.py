#!/usr/bin/env python3
"""Offline tests for Phase 1(c): CoverageLedger + blocking gate.

Pins the invariant from the 2026-06 review: every page of every upload
ends the run in exactly one accounted state, failed pages BLOCK auto-send
(manual review + RFI naming file:pages), and the proposal carries one
honest coverage line. v1 policy: unaccounted pages report but don't block.

Run: python3 test_coverage_ledger.py
"""
import importlib.util as iu
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_pdf(pages=5):
    """Tiny real PDF so register_file can count pages."""
    import PyPDF2
    w = PyPDF2.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    w.write(f)
    f.close()
    return f.name


def main():
    pdf = make_pdf(5)
    try:
        led = T.CoverageLedger()
        led.register_file(pdf)
        s = led.summary()
        check("register counts pages", s["total_pages"] == 5, s)
        check("all pages start unaccounted", s["totals"]["unaccounted"] == 5)

        # Upgrade-only precedence
        led.mark(pdf, 0, "failed", "chunk failed")
        led.mark(pdf, 0, "measured", "retry recovered")   # upgrade ok
        led.mark(pdf, 1, "measured", "chunk 1")
        led.mark(pdf, 1, "failed", "later pass failed")   # downgrade ignored
        led.mark(pdf, 2, "excluded", "Structural")
        led.mark(pdf, 3, "failed", "dropped in retry")
        s = led.summary()
        f0 = s["files"][0]
        check("failed -> measured upgrades", f0["counts"]["measured"] == 2)
        check("measured never downgrades", 2 not in f0["failed_pages"])
        check("excluded recorded", f0["counts"]["excluded"] == 1)
        check("failed page listed 1-based", f0["failed_pages"] == [4], f0)
        check("remaining page unaccounted", f0["counts"]["unaccounted"] == 1)

        # Basename matching (retry paths hand back temp copies)
        led.mark(os.path.basename(pdf), 4, "measured", "via basename")
        check("basename fallback works",
              led.summary()["files"][0]["counts"]["unaccounted"] == 0)

        # Out-of-range / junk marks are ignored
        led.mark(pdf, 99, "failed")
        led.mark(pdf, "x", "failed")
        led.mark("/nonexistent.pdf", 0, "failed")
        check("junk marks ignored", led.summary()["totals"]["failed"] == 1)

        # Gate: failed page blocks + RFI note + coverage attached
        a = {"notes": []}
        T._apply_coverage_gate(a, led)
        check("coverage attached", "coverage" in a)
        check("gate sets manual review on failed page",
              a.get("manual_review_required") is True)
        check("gate RFI names file:page",
              any("RFI REQUIRED" in n and "p.4" in n for n in a["notes"]),
              a["notes"])
        check("summary note present",
              any(n.startswith("[Coverage]") and "measured" in n for n in a["notes"]))

        # Gate: clean ledger does NOT block
        led2 = T.CoverageLedger()
        led2.register_file(pdf)
        led2.mark_file(pdf, "measured", "single call")
        b = {"notes": []}
        T._apply_coverage_gate(b, led2)
        check("clean run does not block", "manual_review_required" not in b)

        # Gate: unaccounted reports but does not block (v1 policy)
        led3 = T.CoverageLedger()
        led3.register_file(pdf)
        led3.mark(pdf, 0, "measured")
        c = {"notes": []}
        T._apply_coverage_gate(c, led3)
        check("unaccounted does not block (v1)",
              "manual_review_required" not in c)
        check("unaccounted reported in summary line",
              any("4 untracked" in n for n in c["notes"]), c["notes"])

        # Gate: no ledger / empty ledger is a no-op
        d = {"notes": []}
        T._apply_coverage_gate(d, None)
        T._apply_coverage_gate(d, T.CoverageLedger())
        check("empty/None ledger no-op", d == {"notes": []})
    finally:
        os.unlink(pdf)

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
