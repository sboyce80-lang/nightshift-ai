# Cross-class golden regression — 2026-08-22

SHA: 5a09fb5  |  A = prod-equivalent baseline, B = baseline + 28-flag JW set

| job | A mean err | B mean err | Δ(B−A) | A subtotal | B subtotal | A mr | B mr | verdict |
|---|---|---|---|---|---|---|---|---|
| dutchess_livestock | 47.8% | 50.1% | +2.3 | $60,059 | $61,054 | False | False | neutral |
| fishkill_397 | 37.3% | 36.6% | -0.6 | $108,741 | $171,011 | False | False | neutral |
| 364_main | 34.9% | 29.7% | -5.2 | $166,504 | $195,671 | False | False | improved |
| tsc_fusion_highland | 68.9% | 80.3% | +11.4 | $26,805 | $40,137 | False | False | REGRESSION |
| honey_farms_malta | 133.8% | 137.0% | +3.3 | $47,802 | $17,785 | False | False | neutral |

## Per-metric detail

### dutchess_livestock
| metric | target | A actual | A err | B actual | B err |
|---|---|---|---|---|---|
| total_base_trim_lf | 391 | 468 | 20% | 452 | 15% |
| total_doors_full_paint | 28 | 11 | 61% | 11 | 61% |
| total_paintable_ceiling_sqft | 2,061 | 2,940 | 43% | 3,271 | 59% |
| total_paintable_wall_sqft | 5,371 | 6,219 | 16% | 6,204 | 16% |
| total_windows_painted_interior | 25 | 0 | 100% | 0 | 100% |

### fishkill_397
| metric | target | A actual | A err | B actual | B err |
|---|---|---|---|---|---|
| total_doors_full_paint | 159 | 148 | 7% | 143 | 10% |
| total_paintable_ceiling_sqft | 13,451 | 13,272 | 1% | 11,067 | 18% |
| total_paintable_wall_sqft | 43,003 | 44,303 | 3% | 45,276 | 5% |
| total_stair_sections | 8 | 14 | 75% | 12 | 50% |
| total_wallcovering_sqft | 1,758 | 0 | 100% | 0 | 100% |

### 364_main
| metric | target | A actual | A err | B actual | B err |
|---|---|---|---|---|---|
| total_base_trim_lf | 8,629 | 6,110 | 29% | 6,712 | 22% |
| total_doors_full_paint | 155 | 167 | 8% | 167 | 8% |
| total_doors_hm_panel | 28 | 26 | 7% | 26 | 7% |
| total_paintable_ceiling_sqft | 26,839 | 13,141 | 51% | 14,638 | 45% |
| total_paintable_wall_sqft | 85,353 | 104,142 | 22% | 99,339 | 16% |
| total_stair_sections | 11 | 14 | 27% | 12 | 9% |
| total_windows_painted_interior | 26 | 0 | 100% | 0 | 100% |

### tsc_fusion_highland
| metric | target | A actual | A err | B actual | B err |
|---|---|---|---|---|---|
| total_cmu_wall_sqft | 26,607 | 21,350 | 20% | 15,758 | 41% |
| total_doors_full_paint | 13 | 0 | 100% | 0 | 100% |
| total_paintable_wall_sqft | 5,447 | 711 | 87% | 0 | 100% |

### honey_farms_malta
| metric | target | A actual | A err | B actual | B err |
|---|---|---|---|---|---|
| total_doors_full_paint | 8 | 1 | 88% | 0 | 100% |
| total_paintable_ceiling_sqft | 1,029 | 1,763 | 71% | 323 | 69% |
| total_paintable_wall_sqft | 4,580 | 15,686 | 242% | 15,686 | 242% |

