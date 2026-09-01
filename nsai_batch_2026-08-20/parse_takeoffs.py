#!/usr/bin/env python3
"""Parse JW/RP takeoff xlsx (PRICE BREAKDOWN sheet) into normalized
ground_truth JSON next to each takeoff file."""
import glob, json, os, re
import openpyxl

def parse(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["PRICE BREAKDOWN"]
    out = {"source": os.path.basename(path), "gross_sqft": None,
           "project_id": None, "line_items": [], "final_base_bid": None,
           "subtotal_finishes": None}
    section = None
    for row in ws.iter_rows():
        vals = [c.value for c in row]
        txt = [str(v).strip() if v is not None else "" for v in vals]
        joined = " ".join(txt)
        if not joined.strip():
            continue
        low = joined.lower()
        if txt[1].startswith("PROJECT ID:"):
            out["project_id"] = txt[1].replace("PROJECT ID:", "").strip()
        if "gross sqft" in low:
            for v in vals[2:]:
                if isinstance(v, (int, float)):
                    out["gross_sqft"] = v; break
        # section headers: non-item rows with a description only
        if vals[0] is None and txt[1] and not any(
                isinstance(v, (int, float)) for v in vals[2:8]):
            if not txt[1].startswith(("PROJECT", "ADDRESS", "SCOPE", "DATE",
                                       "BUILDING", "DIVISION", "Subtotal")):
                section = txt[1][:60]
        if "subtotal (finishes)" in low:
            for v in reversed(vals):
                if isinstance(v, (int, float)):
                    out["subtotal_finishes"] = round(v, 2); break
        if "final base bid" in low:
            for v in reversed(vals):
                if isinstance(v, (int, float)):
                    out["final_base_bid"] = round(v, 2); break
        # line item: ITEM # numeric
        if isinstance(vals[0], (int, float)):
            desc = re.sub(r"\s+", " ", txt[1])
            qty, unit = vals[2], txt[5]
            unit_cost, total = vals[6], vals[7]
            notes = txt[8] if len(txt) > 8 else ""
            out["line_items"].append({
                "item": int(vals[0]), "section": section, "desc": desc,
                "qty": qty, "unit": unit,
                "unit_cost": unit_cost if isinstance(unit_cost, (int, float)) else None,
                "total": round(total, 2) if isinstance(total, (int, float)) else None,
                "notes": notes})
    return out

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(glob.glob(os.path.join(here, "*", "takeoff*.xlsx"))):
        gt = parse(f)
        dst = f.replace(".xlsx", ".ground_truth.json").replace(
            "takeoff", "ground_truth").replace(".ground_truth.ground_truth", ".ground_truth")
        dst = os.path.join(os.path.dirname(f),
                           os.path.basename(f).replace("takeoff", "ground_truth").replace(".xlsx", ".json"))
        json.dump(gt, open(dst, "w"), indent=2)
        priced = [li for li in gt["line_items"] if li.get("total")]
        print(f"{os.path.relpath(f, here):55s} bid=${gt['final_base_bid'] or 0:>12,.2f}  "
              f"gross={gt['gross_sqft']}  items={len(gt['line_items'])} priced={len(priced)}")
