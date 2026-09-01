#!/usr/bin/env python3
"""Variance recap: KnightShift vs JW takeoff for Phelps/Northwell.

Sources
  ground_truth.json  - JW's PRICE BREAKDOWN xlsx (the bid)
  jw_markup_key.json - JW's Bluebeam markups captured BEFORE stripping
                       (scoring only; never fed to the pipeline)
  result.json        - KnightShift output on the annotation-stripped plans
"""
import json, os, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
J = lambda n: json.load(open(os.path.join(HERE, n)))

# ---- JW side: scope buckets from his priced line items -------------------
def jw_buckets(gt):
    b = defaultdict(lambda: {"qty": 0.0, "dollars": 0.0, "unit": "", "lines": []})
    for li in gt["line_items"]:
        desc, qty = li["desc"], li.get("qty") or 0
        tot, unit = li.get("total") or 0.0, li.get("unit") or ""
        low = desc.lower()
        if li.get("section") is None:
            cat = "general_requirements"
        elif re.match(r"^pt\d|plywood", low):
            cat = "walls"
        elif "ceiling" in low or "deck paint" in low:
            cat = "ceilings"
        elif "door" in low:
            cat = "doors"
        elif "window" in low:
            cat = "windows"
        elif low.startswith("wc"):
            cat = "wallcovering"
        else:
            cat = "other"
        d = b[cat]
        d["qty"] += qty; d["dollars"] += tot; d["unit"] = unit
        d["lines"].append(f"{desc[:44]} | {qty:,.0f} {unit} | ${tot:,.0f}")
    return b

# ---- KS side: buckets from aggregated_totals + cost line items -----------
def ks_buckets(ks):
    agg = (ks.get("analysis") or {}).get("aggregated_totals", {}) or {}
    ce = ks.get("cost_estimate", {}) or {}
    q = {
        "walls": (agg.get("total_paintable_wall_sqft") or 0)
                 + (agg.get("total_cmu_wall_sqft") or 0),
        "ceilings": (agg.get("total_paintable_ceiling_sqft") or 0)
                    + (agg.get("total_dryfall_ceiling_sqft") or 0),
        "doors": (agg.get("total_doors_full_paint") or 0)
                 + (agg.get("total_doors_hm_panel") or 0),
        "windows": agg.get("total_windows_painted_interior") or 0,
        "wallcovering": agg.get("total_wallcovering_sqft") or 0,
        "base_trim_lf": agg.get("total_base_trim_lf") or 0,
    }
    d = defaultdict(float); lines = defaultdict(list)
    for li in (ce.get("line_items") or []):
        item = str(li.get("item", "")); low = item.lower()
        tot = li.get("total") or 0.0
        if re.search(r"ceil|soffit|deck", low):               cat = "ceilings"
        elif re.search(r"wall(?!.*cover)|gyp|cmu", low):      cat = "walls"
        elif re.search(r"door|frame", low):                   cat = "doors"
        elif re.search(r"window", low):                       cat = "windows"
        elif re.search(r"wall.?cover|wc-", low):              cat = "wallcovering"
        elif re.search(r"base|trim", low):                    cat = "base_trim_lf"
        elif re.search(r"mobiliz|supervis|manage|clean|permit|bond|misc", low):
            cat = "general_requirements"
        else:                                                 cat = "other"
        d[cat] += tot
        lines[cat].append(f"{item[:44]} | {li.get('qty')} | ${tot:,.0f}")
    return q, d, lines

def main():
    gt, ks = J("ground_truth.json"), J("result.json")
    key = J("jw_markup_key.json")
    JW, (ksq, ksd, kslines) = jw_buckets(gt), ks_buckets(ks)
    ce = ks.get("cost_estimate", {}) or {}
    sub, bid = ce.get("subtotal", 0.0), gt["final_base_bid"]

    out = []
    A = out.append
    A("# Phelps Hospital / Northwell (JW RP 26-010-AUG) — KnightShift vs JW\n")
    A(f"- JW final base bid: **${bid:,.2f}**  ({gt['gross_sqft']:,} GSF)")
    A(f"- KnightShift subtotal: **${sub:,.2f}**")
    dlt = sub - bid
    A(f"- Variance: **${dlt:+,.2f} ({dlt/bid*100:+.1f}%)**")
    A(f"- Manual review flagged: **{ks.get('manual_review_required')}**\n")

    A("## Scope comparison\n")
    A("| Scope | JW qty | KS qty | qty Δ | JW $ | KS $ | $ Δ |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for cat in ("walls", "ceilings", "doors", "windows", "wallcovering",
                "base_trim_lf", "general_requirements", "other"):
        jq = JW[cat]["qty"] if cat in JW else 0.0
        jd = JW[cat]["dollars"] if cat in JW else 0.0
        kq, kd = ksq.get(cat, 0.0), ksd.get(cat, 0.0)
        if not any((jq, jd, kq, kd)):
            continue
        dq = f"{(kq-jq)/jq*100:+.0f}%" if jq else ("n/a" if not kq else "new")
        A(f"| {cat} | {jq:,.0f} | {kq:,.0f} | {dq} | ${jd:,.0f} | ${kd:,.0f} "
          f"| ${kd-jd:+,.0f} |")
    A("")

    A("## JW markup measurements (his own answer key, stripped before the run)\n")
    agg = defaultdict(lambda: [0, 0.0])
    for k in key:
        m = re.match(r"^([\d,\.]+)\s*sf$", k["content"])
        agg[k["subject"] or "(none)"][0] += 1
        if m:
            agg[k["subject"] or "(none)"][1] += float(m.group(1).replace(",", ""))
    A("| Markup subject | count | SF |")
    A("|---|---:|---:|")
    for s, (n, sf) in sorted(agg.items(), key=lambda x: -x[1][1]):
        A(f"| {s} | {n} | {sf:,.0f} |")
    A("")

    A("## JW priced lines\n")
    for cat in JW:
        for ln in JW[cat]["lines"]:
            A(f"- `{cat}` {ln}")
    A("\n## KnightShift priced lines\n")
    for cat in kslines:
        for ln in kslines[cat]:
            A(f"- `{cat}` {ln}")

    txt = "\n".join(out)
    open(os.path.join(HERE, "comparison_northwell.md"), "w").write(txt)
    print(txt)

if __name__ == "__main__":
    main()
