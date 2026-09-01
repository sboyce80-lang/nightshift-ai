#!/usr/bin/env python3
"""Compare a KnightShift result.json against JW's ground_truth.json for one job.

Buckets both sides into scope categories, then emits comparison_<job>.md with
qty + $ deltas per category and an unmatched-lines appendix (nothing hidden).
Usage: compare_job.py <job_dir>
"""
import json, os, re, sys

CATS = [  # (category, unit, regex on item/desc text)
    ("walls_sf",      "SF", r"wall(?!.*cover)|gyp\.? ?walls?|gyp\.?board paint|cmu wall|shaft"),
    ("ceilings_sf",   "SF", r"ceil|celling|act |soffit"),
    ("trim_lf",       "LF", r"trim|base(board)?|casing|wood base"),
    ("doors_ea",      "EA", r"\bdoors?\b(?!.*trim)|door.?frame|hm frame"),
    ("windows_ea",    "EA", r"window"),
    ("sealed_conc_sf","SF", r"seal(ed)? conc|concrete floor|epoxy"),
    ("stairs",        "LOC", r"stair"),
    ("wallcovering",  "SF", r"wall.?cover|wc-|vinyl"),
    ("exterior",      "SF", r"exterior|siding|fascia|stucco|ext\.? "),
    ("specialty",     "-",  r"handrail|railing|bollard|cabinet|shelv|louver|grille|precast"),
]

def bucket(text):
    low = re.sub(r"\s+", " ", str(text)).lower()
    for cat, unit, rx in CATS:
        if re.search(rx, low):
            return cat
    return "other"

def load_gt(job_dir):
    for f in os.listdir(job_dir):
        if f.startswith("ground_truth") and f.endswith(".json"):
            return json.load(open(os.path.join(job_dir, f)))
    raise SystemExit("no ground truth json")

def agg(rows):
    out = {}
    for cat, qty, dollars, label in rows:
        d = out.setdefault(cat, {"qty": 0.0, "dollars": 0.0, "lines": []})
        if isinstance(qty, (int, float)):
            d["qty"] += qty
        if isinstance(dollars, (int, float)):
            d["dollars"] += dollars
        d["lines"].append(label)
    return out

def main(job_dir):
    job = os.path.basename(os.path.normpath(job_dir))
    gt = load_gt(job_dir)
    ks = json.load(open(os.path.join(job_dir, "result.json")))
    ce = ks.get("cost_estimate", {}) or {}

    gt_rows, ks_rows = [], []
    for li in gt["line_items"]:
        if li.get("section") is None and not li.get("total"):
            continue  # Div-01 zero rows
        if re.search(r"conversion|primer|paint –|paint -", li["desc"].lower()):
            continue  # info rows, not scope
        if not li.get("total") and not li.get("unit_cost"):
            # unpriced-but-real scope (e.g. trim "covered with door") — keep qty, $0
            pass
        gt_rows.append((bucket(li["desc"]), li.get("qty"), li.get("total") or 0.0,
                        f"{li['desc'][:60]} | {li.get('qty')} {li.get('unit')} | ${li.get('total') or 0:,.0f}"))
    for li in (ce.get("line_items") or []):
        ks_rows.append((bucket(li.get("item", "")), li.get("qty"),
                        li.get("total") or 0.0,
                        f"{str(li.get('item'))[:60]} | {li.get('qty')} | ${li.get('total') or 0:,.0f}"))

    G, K = agg(gt_rows), agg(ks_rows)
    cats = [c for c, _, _ in CATS] + ["other"]
    lines = [f"# {job} — KnightShift vs JW takeoff", ""]
    lines.append(f"- JW final base bid: **${gt['final_base_bid']:,.2f}**  (gross {gt['gross_sqft']} SF)")
    lines.append(f"- KnightShift subtotal: **${ce.get('subtotal', 0):,.2f}**")
    tot_delta = (ce.get("subtotal", 0) - (gt["final_base_bid"] or 0))
    pct = tot_delta / gt["final_base_bid"] * 100 if gt.get("final_base_bid") else 0
    lines.append(f"- Delta: **${tot_delta:+,.2f} ({pct:+.1f}%)**")
    lines.append(f"- Manual review flagged: {ks.get('manual_review_required')}")
    lines.append("")
    lines.append("| Category | JW qty | KS qty | qty Δ% | JW $ | KS $ | $ Δ |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in cats:
        g, k = G.get(c), K.get(c)
        if not g and not k:
            continue
        gq = g["qty"] if g else 0; kq = k["qty"] if k else 0
        gd = g["dollars"] if g else 0; kd = k["dollars"] if k else 0
        qpct = f"{(kq-gq)/gq*100:+.0f}%" if gq else ("n/a" if not kq else "KS only")
        lines.append(f"| {c} | {gq:,.0f} | {kq:,.0f} | {qpct} | ${gd:,.0f} | ${kd:,.0f} | ${kd-gd:+,.0f} |")
    for name, rows, side in (("JW lines", gt_rows, G), ("KnightShift lines", ks_rows, K)):
        lines.append("")
        lines.append(f"<details><summary>{name} by category</summary>\n")
        for c in cats:
            d = side.get(c)
            if not d:
                continue
            lines.append(f"**{c}**")
            for l in d["lines"]:
                lines.append(f"- {l}")
        lines.append("</details>")
    out = os.path.join(job_dir, f"comparison_{job}.md")
    open(out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:20]))
    print(f"\nwrote {out}")

if __name__ == "__main__":
    main(sys.argv[1])
