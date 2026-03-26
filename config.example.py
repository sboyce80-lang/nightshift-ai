"""
Nightshift AI — Configuration Template
========================================
Copy this file to config.py and fill in your values:
    cp config.example.py config.py

NEVER commit config.py — it contains your API key.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── API Key (required) ──
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "your-api-key-here")

# ── Painting Pricing ──
DEFAULT_LABOR_RATE = 65

PAINT_COSTS = {
    "interior_premium": 55,
    "interior_standard": 45,
    "exterior_premium": 65,
    "exterior_standard": 55,
    "primer": 38,
    "specialty": 75,
}

PAINTING_SQFT_RATES = {
    "interior_walls_new": 3.00,
    "interior_walls_repaint": 3.75,
    "interior_walls_heavy_prep": 5.50,
    "ceilings": 2.75,
    "trim_per_lf": 6.50,
    "exterior_walls": 4.50,
    "exterior_trim": 8.00,
}

MAX_COST_VARIANCE_PERCENT = 15
OVERHEAD_PERCENTAGE = 0.15
PROFIT_MARGIN = 0.20

# ── Company Info ──
COMPANY_NAME = "Your Painting Company"
COMPANY_EMAIL = "proposals@yourcompany.com"
COMPANY_PHONE = "(555) 123-4567"

# ── Email (for web_app.py notifications) ──
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_SMTP_SERVER = "smtp.gmail.com"
EMAIL_SMTP_PORT = 587

# ── Web Settings ──
MAX_PDF_SIZE_MB = 200
MAX_PDFS_PER_EMAIL = 10
WEB_PORT = int(os.environ.get("WEB_PORT", 8080))
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

# ── Volume-Tiered Pricing Model ──
# See config.py (your copy) for full PRICING_MODEL dictionary.
# Rates here are examples — calibrate to your actual project data.
PRICING_MODEL = {
    "gyp_walls": {
        "unit": "sqft", "markup": 0.06,
        "tiers": [
            {"min_qty": 0, "max_qty": 3499, "rate": 1.25},
            {"min_qty": 3500, "max_qty": None, "rate": 0.80},
        ],
    },
    # ... add remaining line items per your pricing structure
}
