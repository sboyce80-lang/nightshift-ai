import os

# Load .env file if present (for email credentials, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; fall back to env vars or hardcoded values

# API Key
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")

# Painting-Specific Pricing
DEFAULT_LABOR_RATE = 65

# Paint costs per gallon
PAINT_COSTS = {
    "interior_premium": 55,
    "interior_standard": 45,
    "exterior_premium": 65,
    "exterior_standard": 55,
    "primer": 38,
    "specialty": 75
}

# All-inclusive pricing (labor + materials per sqft)
PAINTING_SQFT_RATES = {
    "interior_walls_new": 3.00,
    "interior_walls_repaint": 3.75,
    "interior_walls_heavy_prep": 5.50,
    "ceilings": 2.75,
    "trim_per_lf": 6.50,
    "exterior_walls": 4.50,
    "exterior_trim": 8.00
}

# Cost variance - tighter estimates
MAX_COST_VARIANCE_PERCENT = 15  # ±15% instead of ±40%

# Business margins
OVERHEAD_PERCENTAGE = 0.15
PROFIT_MARGIN = 0.20

# Company Info - UPDATE THESE!
COMPANY_NAME = "Your Painting Company"
COMPANY_EMAIL = "proposals@yourcompany.com"
COMPANY_PHONE = "(555) 123-4567"

# Coverage rates
COVERAGE_RATES = {
    "smooth_wall": 400,
    "textured_wall": 300
}

# =============================================================================
# Rider Painting — Pricing Model (Volume-Tiered)
# =============================================================================
# Source: Rider Painting actual project takeoffs (364 Main, Summit, Ruel).
# Validated against Rider's "Updated Pricing" spreadsheets for accuracy.
# Each item has a "tiers" list (sorted low→high qty). The system picks the
# matching tier at pricing time based on the total project quantity.
# Adjust rates here per-project — no need to edit Takeoff_DIRECT.py.
#
PRICING_MODEL = {
    # ── Interior Surfaces ── (Items 1-2)
    # Small projects (<3,500 sf): $1.25/sf (from Ruel Residence single-family pricing)
    # Large projects (>=3,500 sf): $0.80/sf (from 364 Main multi-family pricing)
    "gyp_walls": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 3499, "rate": 1.25},   # < 3,500 sf (single-family rate)
            {"min_qty": 3500, "max_qty": None,  "rate": 0.80},   # >= 3,500 sf (multi-family rate)
        ],
    },
    "gyp_ceilings": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 3499, "rate": 1.25},   # single-family rate
            {"min_qty": 3500, "max_qty": None,  "rate": 0.80},   # multi-family rate
        ],
    },
    "gyp_between_stairs": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 3499, "rate": 0.85},   # Rider uses $0.85/sf
            {"min_qty": 3500, "max_qty": None,  "rate": 0.80},
        ],
    },
    # ── Base Trim ── (Items 8-10)
    "base_trim": {
        "unit": "lf", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 499,  "rate": 3.25},   # < 500 lf (Ruel single-family rate)
            {"min_qty": 500,  "max_qty": 1500, "rate": 3.25},   # 500–1,500 lf
            {"min_qty": 1501, "max_qty": None,  "rate": 1.15},   # > 1,500 lf (Rider rate)
        ],
    },
    # ── Crown Molding ── (Items 11-13)
    "crown_molding": {
        "unit": "lf", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 499,  "rate": 5.25},
            {"min_qty": 500,  "max_qty": 1500, "rate": 4.25},
            {"min_qty": 1501, "max_qty": None,  "rate": 2.50},
        ],
    },
    # ── Doors ── (Items 3-4)
    "doors_full_paint": {
        "unit": "ea", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,  "max_qty": 25,   "rate": 225.00},  # Single-family: door+frame (Ruel rate)
            {"min_qty": 26, "max_qty": None,  "rate": 150.00},  # Multi-family: volume rate (364 Main)
        ],
    },
    "doors_hm_panel": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 110.00}],  # HM panel only (Rider)
    },
    "doors_refinish": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 450.00}],  # Existing/refinish
    },
    "doors_frame_only": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 55.00}],   # HM frame only, no panel
    },
    # ── Windows ── (Items 5-7)
    # Full interior window paint: trim + sash + sill/apron bundled (Rider standard).
    "windows": {
        "unit": "ea", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,  "max_qty": 25,   "rate": 120.00},  # Single-family: pre-primed trim only (Ruel)
            {"min_qty": 26, "max_qty": None,  "rate": 425.00},  # Multi-family: full interior paint (364 Main)
        ],
    },
    "window_sash": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 150.00}],  # Per window per side
    },
    "window_sill_apron": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 40.00}],
    },
    # ── Stairs ── (Item 21)
    "stairs": {
        "unit": "ea", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 1500.00}],
    },
    # ── Specialty ──
    "level_5_finish": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 0.55}],    # PCA: skim coat per SF (was $1,600/ea)
    },
    # ── Exterior ── (Items 30-31, 46-47)
    "exterior_cornice": {
        "unit": "lf", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 20.00}],   # Cornice + fypon brackets (Rider)
    },
    "exterior_window_trim": {
        "unit": "lf", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 2.90}],    # Ext. window trim
    },
    "exterior_soffit_fascia": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 7.25}],
    },
    "exterior_lift_rental": {
        "unit": "ea", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 4000.00}], # Monthly
    },
    # ── Interior Lift (commercial high-ceiling) ──
    "interior_lift_rental": {
        "unit": "ea", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 2500.00}], # Scissor lift monthly
    },
    # ── Painted Columns (commercial) ──
    "painted_columns": {
        "unit": "ea", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,  "max_qty": 10,   "rate": 200.00},
            {"min_qty": 11, "max_qty": None,  "rate": 175.00},
        ],
    },
    # ── CMU Walls (commercial) ── (Items 23-24)
    "cmu_walls_full": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 1.10}],    # Block filler + 2 coats (Rider Mazda)
    },
    "cmu_walls_finish_only": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 0.95}],    # 2 coats only
    },
    # ── Exposed Ceiling / Dryfall (commercial) ── (Items 25-27)
    "exposed_ceiling": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,     "max_qty": 4999,  "rate": 1.80},
            {"min_qty": 5000,  "max_qty": 10000, "rate": 1.15},
            {"min_qty": 10001, "max_qty": None,   "rate": 0.80},
        ],
    },
    "dryfall_ceiling": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,     "max_qty": 4999,  "rate": 0.90},      # Rider Mazda rate
            {"min_qty": 5000,  "max_qty": 10000, "rate": 0.90},      # Flat rate per Rider
            {"min_qty": 10001, "max_qty": None,   "rate": 0.80},
        ],
    },
    # ── Concrete Sealer (garages/basements) ── (Items 28-29)
    "concrete_sealer": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,     "max_qty": 49999, "rate": 2.20},      # Rider Mazda rate
            {"min_qty": 50000, "max_qty": None,   "rate": 1.30},
        ],
    },
    # ── Wallcovering Install (labor only) ── (Rider Mazda: WC-3, WC-5, WC-6)
    "wallcovering_install": {
        "unit": "sqft", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 9.00}],    # Labor-only install rate
    },
    # ── Stained Wood / Clear-Coat Panels ── (oak panels, wood veneer, accent walls)
    "stained_wood": {
        "unit": "sqft", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 6.00}],    # Stain/clear-coat per Rider
    },
    # ── Exterior Wall/Panel Painting ── (Rider Mazda: EP-2, EP-3, EP-4, EX-PNL)
    "exterior_painting": {
        "unit": "sqft", "markup": 0.04,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 1.80}],    # Exterior paint per Rider
    },
    # ── Exterior Material-Specific Items ── (Fishkill 397 manual takeoff rates)
    "exterior_hardie_siding": {
        "unit": "sqft", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 4.85}],    # Hardie/fiber cement siding
    },
    "exterior_azek_trim": {
        "unit": "lf", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 9.00}],    # Azek/PVC trim boards
    },
    "exterior_corner_board": {
        "unit": "lf", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 9.00}],    # Azek corner boards
    },
    "exterior_steel_lintel": {
        "unit": "lf", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 32.00}],   # Steel exposed lintels
    },
    # ── Wallcovering Prep (residential bathroom heuristic) ──
    "wallcovering_prep": {
        "unit": "sqft", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 0.50}],    # Prep/sizing only (not full install)
    },
    # ── Exterior Stain Items ── (Edgehill: wood shingles, trim bands, railings)
    # For projects with wood siding/shingles that need staining (not painting).
    "exterior_stain_siding": {
        "unit": "sqft", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 1.85}],    # Stain per SF (Rider Edgehill rate)
    },
    "exterior_stain_trim": {
        "unit": "lf", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 2.50}],    # Stained trim bands (Rider Edgehill)
    },
    "exterior_stain_railing": {
        "unit": "lf", "markup": 0.05,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 32.00}],   # Stained wood railing (Rider Edgehill)
    },
    # ── Footprint-Based Interior Rate ── (for senior living / large residential)
    # When room-by-room extraction is incomplete, use footprint × rate as fallback.
    # $3.80/SF footprint is Rider's all-inclusive interior rate (Edgehill).
    "footprint_interior": {
        "unit": "sqft", "markup": 0.00,
        "tiers": [{"min_qty": 0, "max_qty": None, "rate": 3.80}],    # All-inclusive interior per SF footprint
    },
    # ── Interior Soffits (GYP drops above wall angle) ──
    "interior_soffit": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0,    "max_qty": 3499, "rate": 0.85},        # Same as GYP wall rate per Rider
            {"min_qty": 3500, "max_qty": None,  "rate": 0.80},
        ],
    },
}

# =============================================================================
# PCA Cost & Estimating Guide Vol. 2 — Industry Constants
# =============================================================================
# Source: PDCA/PCA Cost and Estimating Guide Volume 2: Rates and Tables
# (Third Edition). These constants are used for measurement validation,
# labor hour estimation, and cross-checking extracted quantities.
#
PCA_CONSTANTS = {
    # ── Section 4B: Common Estimating Multipliers ──
    "door_flush_sf": 42,              # Standard flush door surface area (SF)
    "door_frame_sf": 34,              # Standard doorframe surface area (SF)
    "door_paneled_sf": 50,            # Paneled door (estimated from PCA rates)
    "stair_sf_per_riser": 20,         # Steel stairs & railing: 20 SF per riser
    "stair_risers_per_section": 12,   # Typical risers per stair flight section
    "fire_escape_sf_per_riser": 15,

    # ── Section 3: Standards of Measurement — Opening Deduction Rules ──
    # Rule #8: Small openings disregarded. Floor-to-ceiling openings > 5ft wide
    #          deducted. All openings >= 100 SF deducted.
    # Rule #9: Cabinets, tubs, showers NOT deducted.
    "opening_deduct_min_sf": 100,     # Deduct openings >= 100 SF
    "opening_deduct_ftc_width": 5,    # Deduct floor-to-ceiling openings > 5ft wide
    "std_door_opening_sf": 21,        # Standard 3'0" x 7'0" door = 21 SF
    "std_window_opening_sf": 15,      # Standard 3'0" x 5'0" window = 15 SF (< 100 SF, NOT deducted per Rule #8)

    # ── Section 5C: Labor Production Rates (SF/HR) — mid-range values ──
    # Used for informational labor hour estimates, not pricing.
    # Rates assume airless spray unless noted. Include normal cut-in with brush.
    "labor_rates": {
        "gyp_walls_spray_1st": 650,   # Drywall smooth, spray, 1st coat
        "gyp_walls_spray_add": 750,   # Drywall smooth, spray, additional coat
        "gyp_walls_roll_1st": 337,    # Drywall smooth, roll, 1st coat
        "gyp_walls_roll_add": 375,    # Drywall smooth, roll, additional coat
        "gyp_ceilings_spray": 650,    # Ceiling spray (same as walls)
        "base_trim_brush": 100,       # Interior trim, brush (SF/HR, 1 SF/LF)
        "doors_steel_spray_1st": 200, # Steel doors flush, spray, 1st coat
        "doors_steel_spray_add": 250, # Steel doors flush, spray, additional
        "doors_wood_spray_1st": 200,  # Wood doors flush, spray, 1st coat
        "doors_wood_brush_1st": 84,   # Wood doors flush, brush, 1st coat
        "windows_brush_1st": 85,      # Window sash + trim, brush, 1st coat
        "cmu_spray": 488,             # CMU smooth, spray, average
        "cmu_roll": 300,              # CMU smooth, roll, average
        "dryfall_spray": 400,         # Acoustical/dryfall ceiling spray
        "stairs_brush_1st": 100,      # Metal stairs & railings, brush, 1st coat
        "wallcovering_54in": 45,      # 54" vinyl WC, hand (SF/HR)
    },

    # ── Section 5D: Production Rate Adjustments ──
    # Multi-story buildings: productivity diminishes 1-2% per floor due to
    # material handling, elevator waits, tool retrieval. Applied to area-based
    # rates only (walls, ceilings), not per-unit items (doors, windows).
    "height_productivity_loss_per_floor": 0.015,  # 1.5% per floor above threshold
    "height_productivity_max_loss": 0.20,          # Cap at 20% total loss
    "height_productivity_start_floor": 4,          # Apply starting at floor 5

    # ── Section 4C: Window Sash & Trim SF (non-operable, 1 side, 1 lite) ──
    # Key: "WxH" (width x height in feet), Value: total SF of sash + trim
    "window_sf_table": {
        "1x1": 8,  "1x2": 12, "1x3": 16, "1x4": 20,
        "2x2": 16, "2x3": 20, "2x4": 24, "2x5": 28, "2x6": 32, "2x7": 36,
        "3x3": 24, "3x4": 28, "3x5": 32, "3x6": 36, "3x7": 40,
        "4x4": 32, "4x5": 35, "4x6": 40, "4x8": 48,
    },
    "window_sf_default": 32,          # Default for unknown size (3x5 equivalent)
}

# =============================================================================
# Small Commercial / Renovation Rate Overrides
# =============================================================================
# Used when building is commercial but not large enough for large-commercial rates.
# Calibrated from BFCU Glenmont. Adjust here — no need to edit Takeoff_DIRECT.py.
SMALL_COMMERCIAL_RATES = {
    "wall_rate": 1.40,
    "ceil_rate": 1.40,
    "door_fp_rate": 155.00,
    "door_hm_rate": 110.00,
}

# =============================================================================
# Email Ingestion Settings (Gmail)
# =============================================================================
# Credentials come from .env file — never hardcode passwords here.
#
EMAIL_ADDRESS       = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD  = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_IMAP_SERVER   = os.environ.get("EMAIL_IMAP_SERVER", "imap.gmail.com")
EMAIL_IMAP_PORT     = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
EMAIL_SMTP_SERVER   = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT     = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
EMAIL_WATCH_FOLDER  = os.environ.get("EMAIL_WATCH_FOLDER", "INBOX")
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", "60"))
EMAIL_SUBJECT_FILTER = os.environ.get("EMAIL_SUBJECT_FILTER", "")

# Outbound transactional email (Resend HTTP API). Used by notifications.py
# for signup-approval emails. Distinct from the EMAIL_* IMAP credentials
# above (which are for inbound RFP ingestion only).
RESEND_API_KEY    = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "")
RESEND_FROM_NAME  = os.environ.get("RESEND_FROM_NAME", "Knight Shift")
MAX_PDF_SIZE_MB     = 200
MAX_PDFS_PER_EMAIL  = 10

# Image-Based Schedule Extraction
ENABLE_IMAGE_SCHEDULE_EXTRACTION = True   # Pre-scan PDFs for schedule pages & render as images
SCHEDULE_IMAGE_DPI = 200                  # DPI for rendering schedule pages (200 = stays within Claude 8000px limit)

# Image Fallback for Floor Plan Extraction
ENABLE_IMAGE_FALLBACK = True               # Render floor plan PDFs as images when native PDF returns 0 rooms
IMAGE_FALLBACK_DPI = 190                   # DPI for rendering floor plans (190 = ~7980px for 42" sheets, under 8000px limit)
IMAGE_FALLBACK_ENHANCE = True              # Apply contrast/sharpening to rendered images before sending

# Enhanced Extraction for Large-Format (DD-Scale) Architectural PDFs
# Uses PyMuPDF text-layer pre-extraction + page tiling to read dimensions
# that Claude can't see at native resolution (1568px downscale limit)
ENABLE_ENHANCED_EXTRACTION = True          # Use text-layer + tiling for large-format floor plans
ENHANCED_TILE_DPI = 150                    # DPI for tile rendering (150 → ~2800px, still above Claude's 1568px limit; saves ~44% RAM vs 200 DPI)
ENHANCED_TILE_GRID = (2, 2)                # Default tile grid (4 tiles per page)
ENHANCED_TILE_GRID_LARGE = (2, 2)          # Was (3, 3)=9 tiles; now (2, 2)=4 tiles to save memory. Still readable at 150 DPI.
LARGE_FORMAT_THRESHOLD_PT = 2000           # Page size threshold (~28") to trigger enhanced path
ENHANCED_TILE_OVERLAP_PCT = 0.05           # 5% overlap between tiles

# Schedule-Based Estimation (when floor plans are missing)
ENABLE_SCHEDULE_ESTIMATION = True          # Estimate wall/ceiling from Room Finish Schedules when no floor plans
SCHEDULE_ESTIMATION_CONFIDENCE = 0.85      # Apply 85% confidence factor to schedule-derived areas

# Building Multiplier (for identical buildings in multi-building projects)
ENABLE_BUILDING_MULTIPLIER = True          # Allow multiplying entire building estimates for identical buildings

# Building Inventory Scan (pre-scan index/TOC pages to detect building counts)
ENABLE_BUILDING_INVENTORY_SCAN = True      # Scan index pages with Claude to detect building inventory
INVENTORY_IMAGE_DPI = 150                  # DPI for rendering index pages for building inventory extraction
INVENTORY_IMAGE_QUALITY = 80               # JPEG quality for index page images (0-100)

# Web Form Settings
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")
MAX_SUBMISSIONS_PER_HOUR = int(os.environ.get("MAX_SUBMISSIONS_PER_HOUR", "5"))

# Job Queue (Redis + RQ)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Legacy single queue name — kept for backward compat with any external scripts
# that import RQ_QUEUE_NAME. Live traffic now routes to fast/heavy below.
RQ_QUEUE_NAME = os.environ.get("RQ_QUEUE_NAME", "nightshift")
RQ_QUEUE_FAST = os.environ.get("RQ_QUEUE_FAST", "nightshift-fast")
RQ_QUEUE_HEAVY = os.environ.get("RQ_QUEUE_HEAVY", "nightshift-heavy")
# Routing heuristic: a submission goes to the heavy queue if EITHER threshold
# is exceeded. Pages alone misses 100MB DD-scale decks with few pages; size
# alone misses moderate-size decks with many pages.
HEAVY_QUEUE_PAGE_THRESHOLD = int(os.environ.get("HEAVY_QUEUE_PAGE_THRESHOLD", "10"))
HEAVY_QUEUE_FILE_MB = int(os.environ.get("HEAVY_QUEUE_FILE_MB", "30"))
RQ_JOB_TIMEOUT = int(os.environ.get("RQ_JOB_TIMEOUT", "7200"))   # 2 hours per job
RQ_RESULT_TTL = int(os.environ.get("RQ_RESULT_TTL", "604800"))   # keep results 7 days

# Beta gate
BETA_DAILY_SUBMISSION_CAP_DEFAULT = int(
    os.environ.get("BETA_DAILY_SUBMISSION_CAP_DEFAULT", "5")
)

# Admins — comma-separated lowercase emails. Admins can prioritize jobs
# (jump to front of queue) on the /jobs page.
ADMIN_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
)

# Object Storage (Cloudflare R2 — S3-compatible)
R2_ACCOUNT_ID         = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID      = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY  = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET             = os.environ.get("R2_BUCKET", "")
R2_ENDPOINT_URL       = os.environ.get(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "",
)
R2_SIGNED_URL_EXPIRY  = int(os.environ.get("R2_SIGNED_URL_EXPIRY", "604800"))  # 7d

# Database (Postgres in prod, local dev expects a local Postgres too)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://localhost/nightshift_dev",
)
# Render's DATABASE_URL is `postgres://...`; SQLAlchemy 2.x wants `postgresql://...`.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

# Auth (Clerk)
CLERK_SECRET_KEY      = os.environ.get("CLERK_SECRET_KEY", "")
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
# Hosted sign-in URL — Clerk hosts this for free at accounts.<your-clerk-domain>.
# In Clerk dashboard, you can copy the exact URL; we accept it via env var.
CLERK_SIGN_IN_URL     = os.environ.get("CLERK_SIGN_IN_URL", "")
# Allowed origins for Clerk's CSRF check (comma-separated). For dev:
#   http://localhost:8080
CLERK_AUTHORIZED_PARTIES = [
    o.strip() for o in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",")
    if o.strip()
]

def validate_config():
    if not CLAUDE_API_KEY:
        print("⚠️  CLAUDE_API_KEY is not set")
        print("Edit config.py and add your API key")
        return False
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("⚠️  Email credentials not set (EMAIL_ADDRESS / EMAIL_APP_PASSWORD)")
        print("   Create a .env file — see .env.example for the template")
        print("   (Only needed if you want email ingestion)")
    print("✅ Configuration is valid!")
    return True

if __name__ == "__main__":
    validate_config()
