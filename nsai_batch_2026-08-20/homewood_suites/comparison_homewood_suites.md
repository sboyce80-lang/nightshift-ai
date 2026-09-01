# homewood_suites — KnightShift vs JW takeoff

- JW final base bid: **$1,322,982.57**  (gross 82634 SF)
- KnightShift subtotal: **$673,842.86**
- Delta: **$-649,139.71 (-49.1%)**
- Manual review flagged: True

| Category | JW qty | KS qty | qty Δ% | JW $ | KS $ | $ Δ |
|---|---|---|---|---|---|---|
| walls_sf | 0 | 108,170 | KS only | $0 | $93,104 | $+93,104 |
| ceilings_sf | 0 | 40,467 | KS only | $0 | $34,831 | $+34,831 |
| trim_lf | 13,362 | 318 | -98% | $0 | $1,096 | $+1,096 |
| doors_ea | 0 | 372 | KS only | $0 | $59,148 | $+59,148 |
| windows_ea | 0 | 0 | n/a | $0 | $0 | $+0 |
| sealed_conc_sf | 1,963 | 0 | -100% | $12,702 | $0 | $-12,702 |
| stairs | 8 | 2,358 | +29375% | $12,000 | $30,728 | $+18,728 |
| wallcovering | 0 | 44,701 | KS only | $0 | $418,401 | $+418,401 |
| exterior | 0 | 3,601 | KS only | $0 | $15,059 | $+15,059 |
| specialty | 0 | 270 | KS only | $0 | $5,152 | $+5,152 |
| other | 1,896 | 2,616 | +38% | $3,974 | $16,324 | $+12,350 |

<details><summary>JW lines by category</summary>

**trim_lf**
- Paint @ Door Trims | 13362 LF | $0
**sealed_conc_sf**
- Sealed Concrete – Polished Finish Structural concrete slab p | 1084 SF | $2,550
- SC-HRI 1/4" Epoxy Mortar & Broadcast System - Stonshield HRI | 879 SF | $10,152
**stairs**
- Paint @ Stairs - underside, railings, balusters etc | 8 LOC | $12,000
**other**
- Exposed To Structure Deck Paint | 1891 SF | $3,574
- Paint @ Elec Panels PT-03 | 4 EA | $400
- Paint 200sqft for first coat & 400sqft for seccond coat | 1 Gallon | $0
</details>

<details><summary>KnightShift lines by category</summary>

**walls_sf**
- Gyp. Walls - 108,170 sqft @ $0.81 | 108169.79999999999 | $93,104
- CMU Walls (Full System) - 0 sqft @ $1.12 | 0 | $0
- Lyme Wash Walls - 0 sqft @ $4.50 | 0 | $0
- Plaster Walls - 0 sqft @ $7.50 | 0 | $0
**ceilings_sf**
- Gyp. Ceilings - 40,467 sqft @ $0.81 | 40467.4 | $34,831
- Dryfall Ceiling - 0 sqft @ $0.91 | 0 | $0
- Interior Soffits - 0 sqft @ $0.85 | 0 | $0
- Ext. Soffit/Fascia - 0 sqft @ $7.25 | 0 | $0
**trim_lf**
- Base Trim - 318 LF @ $3.25 | 318 | $1,096
- Exterior Window Trim - 0 LF @ $2.90 | 0 | $0
- Ext. Azek Trim - 0 LF @ $9.00 | 0 | $0
- Ext. Stain Trim Bands - 0 LF @ $2.50 | 0 | $0
**doors_ea**
- Doors (Full Paint) - 372 EA @ $150.00 | 372 | $59,148
- Doors (HM Panel) - 0 EA @ $110.00 | 0 | $0
- Doors (Frame Only) - 0 EA @ $55.00 | 0 | $0
- Ext. HM Doors - 0 EA @ $110.00 | 0 | $0
**windows_ea**
- Windows (Interior Paint) - 0 EA @ $120.00 | 0 | $0
**stairs**
- Stairs - 18 sections @ $1500.00 | 18 | $28,620
- Gyp. Between Stairs - 2,340 sqft @ $0.85 | 2340 | $2,108
**wallcovering**
- Wallcovering Install (Labor) - 44,701 sqft @ $9.00 | 44701.0 | $418,401
**exterior**
- Exterior Cornice - 0 LF @ $20.00 | 0 | $0
- Exterior Painting - 3,600 sqft @ $1.80 | 3600 | $6,739
- Ext. Hardie Siding - 0 sqft @ $4.85 | 0 | $0
- Ext. Corner Boards - 0 LF @ $9.00 | 0 | $0
- Ext. Steel Lintels - 0 LF @ $32.00 | 0 | $0
- Exterior Lift Rental - 1 EA @ $8000.00 | 1 | $8,320
- Ext. Stain Siding - 0 sqft @ $1.85 | 0 | $0
- Ext. Stain Railing - 0 LF @ $32.00 | 0 | $0
**specialty**
- Painted Cabinets - 0 sqft @ $8.00 | 0 | $0
- Painted Railings - 270 LF @ $18.00 | 270 | $5,152
**other**
- Level 5 Finish - 0 sqft @ $0.55 | 0 | $0
- Concrete Sealer - 0 sqft @ $2.20 | 0 | $0
- Painted Columns - 0 EA @ $200.00 | 0 | $0
- Stained Wood Panels - 2,616 sqft @ $6.00 | 2616 | $16,324
- Interior Lift Rental - 0 EA @ $2500.00 | 0 | $0
</details>

## Corrected mapping + root causes

Wallcovering IS this job: JW's 8 WC lines total ≈136,636 SF / $1,127,250 of his
$1.32M bid (Homewood Suites brand-standard WC package).

| Scope | JW | KS | Gap |
|---|---|---|---|
| Wallcovering | 136,636 SF / $1,127,250 | 44,701 SF / $418,401 | −67% qty — THE miss |
| Painted walls | 9,419 SF / $7,912 | 108,170 SF / $93,104 | KS reclassified WC walls as paint |
| Ceilings | 60,445 SF / $50,774 | 40,467 SF / $34,831 | −33% |
| Door frames/doors | 393 EA / $39,300 | 372 EA / $59,148 | count ✅, type/rate diff |
| Windows (344 ops) | $43,000 | 0 | missed again |
| Stairs | 8 LOC / $12,000 | 18 / $28,620 | 2x over |
| Epoxy (SC-HRI) | 879 SF / $10,152 | 0 | missed again |

1. **6 of 28 pages unparseable & MISSING (p3,8,13,19,23,25)** — the parse-death
   bug at its worst; manual_review=True correctly lists them. The WC schedule
   (per-type SF: WC-01 87,219 SF…) almost certainly lives on lost pages.
2. **WC quantities were GUESSED, not read:** log shows "~50% of wall area for
   WC+PT rooms", "≈109 SF based on bed wall", "~480 SF/unit" + RFIs — per-room
   approximation because the WC schedule was never captured. JW's takeoff reads
   exact per-type SF. Where a WC schedule exists it must be authoritative
   (same principle as VME/schedule-gate work in July).
3. **Wall misclassification double-error:** same walls JW covers under WC, KS
   priced as painted gyp at $0.81 — under-prices the WC AND fabricates paint
   scope the painter won't do.
4. **Windows 0 (344 ops, $43k)** — 5th job, same class: no window schedule
   detected → hard-numbers gate → 0 + RFI.
5. Positive signals: door count 372 vs 393 (−5%), ceiling within 33% despite
   6 lost pages, WC labor rate ($9.00 vs JW's ~$8.25 effective) is sane.
