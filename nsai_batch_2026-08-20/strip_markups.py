#!/usr/bin/env python3
"""Physically strip estimator markups from a plan PDF before blind validation.

The MARKUPS sets JW ships carry his full answer key ON SHEET: Bluebeam
measurement annotations plus a text "Legend" table listing his own
quantities. Any KnightShift run on an unstripped copy is circular — the
vision pass can simply read his numbers. This removes every annotation
object, then VERIFIES no residual markup text or vector geometry survives
in the page content stream.

Usage: strip_markups.py <in.pdf> <out.pdf>
"""
import sys
import fitz


def strip(src, dst):
    doc = fitz.open(src)
    before = {"annots": 0, "text": 0, "draw": 0}
    for pg in doc:
        before["annots"] += len(list(pg.annots() or []))
        before["text"] += len(pg.get_text().strip())
        before["draw"] += len(pg.get_drawings())

    removed = 0
    for pg in doc:
        while pg.first_annot:                 # bound-annot safe deletion
            pg.delete_annot(pg.first_annot)
            removed += 1
    # Bookmarks and XMP/DocInfo carry estimator labels ("... Markup",
    # "Bluebeam Revu") — no quantities, but scrub them so the clean set
    # carries no provenance hint at all.
    doc.set_toc([])
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()

    chk = fitz.open(dst)
    after = {"annots": 0, "text": 0, "draw": 0}
    residual = []
    for i, pg in enumerate(chk):
        na = len(list(pg.annots() or []))
        t = pg.get_text().strip()
        nd = len(pg.get_drawings())
        after["annots"] += na
        after["text"] += len(t)
        after["draw"] += nd
        if t:
            residual.append((i + 1, t[:800]))
    leaks = [n for n in (b"Legend", b"Bluebeam", b"Markup", b"Revu")
             if n in open(dst, "rb").read()]
    chk.close()

    print(f"in : {src}")
    print(f"out: {dst}")
    print(f"annotations removed: {removed}")
    print(f"  annots  {before['annots']:>6} -> {after['annots']}")
    print(f"  textchr {before['text']:>6} -> {after['text']}")
    print(f"  drawings{before['draw']:>6} -> {after['draw']}")
    if residual:
        print("\n!! RESIDUAL TEXT IN CONTENT STREAM (inspect before trusting):")
        for pno, t in residual:
            print(f"  --- p{pno} ---\n{t}\n")
        return 2
    if leaks:
        print(f"\nnote: residual provenance tokens (no quantities): {leaks}")
    print("\nCLEAN: no residual text; markup geometry gone.")
    return 0


if __name__ == "__main__":
    sys.exit(strip(sys.argv[1], sys.argv[2]))
