#!/usr/bin/env python3
"""Tests for _vme_source_pdf_paths — VME must measure vector originals.

pdf_preprocess rasterizes oversized pages into `<base>_normalized.pdf`,
destroying the CAD line work the geometric engine measures (2026-07-07
Poly Western P5/P7/P10 shadows: scale detected, 0.0 LF walls). The helper
maps normalized paths back to the on-disk originals so VME shadow/primary/
authoritative all measure real geometry, and falls back to the given path
when there is nothing to map back to.

Run: python3 test_vme_source_paths.py
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


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


with tempfile.TemporaryDirectory() as td:
    orig = os.path.join(td, "PlanSet.pdf")
    norm = os.path.join(td, "PlanSet_normalized.pdf")
    open(orig, "wb").write(b"%PDF-1.4")
    open(norm, "wb").write(b"%PDF-1.4")

    # Normalized path with the original on disk -> original
    check("normalized maps back to vector original",
          T._vme_source_pdf_paths([norm]) == [orig])

    # Un-normalized path passes through untouched
    check("raw path passes through",
          T._vme_source_pdf_paths([orig]) == [orig])

    # Mixed multi-file set: each file mapped independently
    check("mixed set maps per-file",
          T._vme_source_pdf_paths([norm, orig]) == [orig, orig])

    # Original deleted -> fall back to the normalized copy (never a dead path)
    os.remove(orig)
    check("missing original falls back to normalized",
          T._vme_source_pdf_paths([norm]) == [norm])

# Empty / None input degrades to empty list
check("empty input -> empty list", T._vme_source_pdf_paths([]) == [])
check("None input -> empty list", T._vme_source_pdf_paths(None) == [])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
