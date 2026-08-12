"""VME measurement independence from markups (2026-08-11).

PyMuPDF's get_drawings() includes annotation APPEARANCE STREAMS, so
measurement markups (estimator polylines, revision clouds, stamps) read as
drawn geometry and inflate the wall measurement. Harlem Valley blind test:
1,438.6 LF measured on the estimator's marked-up sheet vs 1,372.3 LF on the
same sheet with annotations stripped (+4.8% phantom wall).

The guarantee this suite locks in: the geometric engine measures the DRAWING
and only the drawing — adding any amount of annotation ink to a page must
not move the measured quantity by a single point. Markup quantities are the
markup-takeoff gate's job; geometry is geometry.
"""
import fitz

import vme_attribution as vme

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _build(path, with_annots):
    """One 'floor plan' page: a drawn vector rectangle (the building) plus a
    scale note — optionally buried under annotation ink (measurement
    polylines, a revision cloud polygon, a filled stamp rectangle)."""
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((60, 40), 'GROUND FLOOR PLAN   SCALE: 1/8" = 1\'-0"')
    # drawn geometry: 40ft x 20ft room at 9 pt/ft
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(200, 200, 200 + 360, 200 + 180))
    shape.finish(width=2.0)
    shape.commit()
    if with_annots:
        for i in range(12):
            y = 100 + i * 40
            a = page.add_polyline_annot(
                [fitz.Point(100, y), fitz.Point(900, y)])
            info = a.info
            info["subject"] = "Interior finishes: Paint"
            info["content"] = "88'-0\""
            a.set_info(info)
            a.update()
        cloud = page.add_polygon_annot(
            [fitz.Point(300, 300), fitz.Point(500, 280),
             fitz.Point(520, 420), fitz.Point(320, 430)])
        cloud.update()
        stamp = page.add_rect_annot(fitz.Rect(700, 500, 900, 600))
        stamp.set_colors(stroke=(1, 0, 0), fill=(1, 0, 0))
        stamp.update()
    doc.save(path)
    doc.close()


CLEAN = "/tmp/test_vme_strip_clean.pdf"
MARKED = "/tmp/test_vme_strip_marked.pdf"
_build(CLEAN, with_annots=False)
_build(MARKED, with_annots=True)

print("\nMeasurement copies")
copies = vme._measurement_copies([CLEAN, MARKED])
check(copies[0] == CLEAN, "annotation-free file passes through untouched")
check(copies[1] != MARKED, "annotated file is replaced by a stripped copy")
check(copies[1].endswith("test_vme_strip_marked.pdf"),
      "stripped copy keeps the original basename (anchor tokens)")
stripped = fitz.open(copies[1])
check(all(not p.first_annot for p in stripped),
      "stripped copy carries zero annotations")
n_clean = len(fitz.open(CLEAN)[0].get_drawings())
n_stripped = len(stripped[0].get_drawings())
check(n_clean == n_stripped,
      f"stripped copy has identical drawn geometry ({n_stripped} paths)")
n_marked = len(fitz.open(MARKED)[0].get_drawings())
check(n_marked > n_clean,
      f"un-stripped marked file DOES leak annotation ink into "
      f"get_drawings ({n_marked} vs {n_clean} paths) — the failure mode")

print("\nEngine invariance")
s_clean = vme.compute_vme_shadow_v2([CLEAN]) or {}
s_marked = vme.compute_vme_shadow_v2([MARKED]) or {}
check(s_clean.get("total_wall_run_lf") == s_marked.get("total_wall_run_lf"),
      f"wall measurement identical with and without markups "
      f"({s_clean.get('total_wall_run_lf')} vs "
      f"{s_marked.get('total_wall_run_lf')} LF)")

print("\nFail-open")
copies_bad = vme._measurement_copies(["/tmp/does_not_exist_vme.pdf"])
check(copies_bad == ["/tmp/does_not_exist_vme.pdf"],
      "unreadable file passes through (measured downstream as before)")

print()
if fails:
    print(f"{len(fails)} FAILURE(S)")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL PASS")
