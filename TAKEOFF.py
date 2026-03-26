#!/usr/bin/env python3
"""
Nightshift AI - Architectural Floor Plan Analyzer
==================================================
Analyzes architectural drawings (floor plans) as IMAGES
Identifies rooms, reads scales/legends, calculates measurements
Aggregates totals for painting takeoffs

Requirements:
    pip install anthropic PyPDF2 pdf2image pillow

Note: Requires poppler-utils on Mac:
    brew install poppler
"""

import sys
import json
from config import CLAUDE_API_KEY
import anthropic
import PyPDF2
from datetime import datetime
import os
import base64
from io import BytesIO

# Try to import pdf2image
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False
    print("⚠️  Warning: pdf2image not installed. Install with: pip install pdf2image")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("⚠️  Warning: Pillow not installed. Install with: pip install Pillow")

# Rider Painting pricing
PRICING_MODEL = {
    "gyp_walls": {"cost_per_sqft": 0.80, "markup": 0.06},
    "gyp_ceilings": {"cost_per_sqft": 0.80, "markup": 0.06},
    "base_trim": {"cost_per_lf": 1.15, "markup": 0.06},
    "doors": {"cost_per_door": 150.00, "markup": 0.06},
    "windows": {"cost_per_window": 425.00, "markup": 0.06}
}

def convert_pdf_to_images(pdf_path, max_pages=20):
    """Convert PDF pages to images for analysis"""
    
    if not PDF2IMAGE_AVAILABLE:
        print("❌ pdf2image not available. Cannot analyze floor plans as images.")
        print("Install with: pip install pdf2image")
        print("Also install poppler: brew install poppler")
        return []
    
    try:
        print(f"📄 Converting PDF to images (max {max_pages} pages)...")
        images = convert_from_path(pdf_path, first_page=1, last_page=max_pages, dpi=150)
        print(f"✅ Converted {len(images)} pages to images")
        return images
    except Exception as e:
        print(f"❌ Error converting PDF: {e}")
        print("Make sure poppler is installed: brew install poppler")
        return []

def image_to_base64(image):
    """Convert PIL Image to base64 string"""
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

def analyze_floor_plan_page(client, image, page_num):
    """
    Analyze a single floor plan page as an image
    Identifies rooms, reads scales, extracts measurements
    """
    
    # Convert image to base64
    image_base64 = image_to_base64(image)
    
    prompt = """You are analyzing an ARCHITECTURAL FLOOR PLAN drawing.

Your task: IDENTIFY ALL ROOMS and CALCULATE their dimensions for a PAINTING ESTIMATE.

STEP 1: IDENTIFY THE SCALE
- Look for a scale bar (e.g., "1/4" = 1'-0"" or graphic scale)
- Note the scale so you can calculate real-world dimensions

STEP 2: IDENTIFY ALL ROOMS
- Label each room (e.g., "Floor 1 Room A", "Floor 1 Bedroom", "2nd Floor Living Room")
- Identify room names if labeled (bedroom, kitchen, bathroom, etc.)

STEP 3: READ THE LEGEND/MATERIALS
- Find the materials legend or key
- Identify wall types (e.g., "1HR GYP" = 1-hour gypsum, "CMU" = concrete block)
- Note which walls get painted vs not painted
- Identify ceiling types

STEP 4: MEASURE EACH ROOM
- Using the scale, measure length and width of each room
- Note ceiling height (often specified in room labels like "CLG HT: 9'-0"")
- Calculate: Floor Area = Length × Width
- Calculate: Wall Area = Perimeter × Height
- Calculate: Ceiling Area = Length × Width

STEP 5: COUNT ELEMENTS
- Count doors in each room
- Count windows in each room
- Measure linear feet of base trim (room perimeter)

Return JSON with this structure:
{
  "page_info": {
    "page_number": 1,
    "floor_level": "1st Floor / 2nd Floor / Basement",
    "scale": "scale notation found (e.g., 1/4 inch = 1 foot)",
    "scale_ratio": "numeric ratio for calculations"
  },
  
  "legend_materials": {
    "wall_types": [
      {"code": "1HR GYP", "description": "1-hour gypsum board", "paintable": true},
      {"code": "CMU", "description": "Concrete masonry unit", "paintable": false}
    ],
    "ceiling_types": [
      {"code": "GYP", "description": "Gypsum board ceiling", "paintable": true}
    ]
  },
  
  "rooms": [
    {
      "room_id": "Floor 1 Room A",
      "room_name": "Living Room (if labeled)",
      "dimensions": {
        "length_feet": 20,
        "width_feet": 15,
        "ceiling_height_feet": 9,
        "floor_area_sqft": 300,
        "wall_perimeter_lf": 70,
        "wall_area_sqft": 630,
        "ceiling_area_sqft": 300
      },
      "materials": {
        "walls": "1HR GYP (paintable)",
        "ceiling": "GYP (paintable)"
      },
      "elements": {
        "doors": 2,
        "windows": 3,
        "base_trim_lf": 70
      }
    }
  ],
  
  "notes": [
    "Any special notes, conditions, or things that affect painting"
  ]
}

IMPORTANT:
- If you cannot find a scale, say so in the notes
- If dimensions are unclear, estimate based on typical room sizes
- If this page is NOT a floor plan (title sheet, details, etc.), return:
  {"is_floor_plan": false, "page_type": "what this page shows"}
- Focus on PAINTABLE surfaces only (gypsum board walls/ceilings, wood trim/doors)
- Ignore mechanical, electrical, or structural elements"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_base64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }]
        )
        
        return message.content[0].text
        
    except Exception as e:
        print(f"❌ Error analyzing page {page_num}: {e}")
        raise

def aggregate_measurements(all_pages_data):
    """
    Aggregate measurements from all floor plan pages
    Calculate totals across all floors
    """
    
    all_rooms = []
    total_wall_sqft = 0
    total_ceiling_sqft = 0
    total_trim_lf = 0
    total_doors = 0
    total_windows = 0
    
    for page_data in all_pages_data:
        if page_data.get('is_floor_plan') == False:
            continue
        
        for room in page_data.get('rooms', []):
            all_rooms.append(room)
            
            dims = room.get('dimensions', {})
            total_wall_sqft += dims.get('wall_area_sqft', 0)
            total_ceiling_sqft += dims.get('ceiling_area_sqft', 0)
            
            elements = room.get('elements', {})
            total_trim_lf += elements.get('base_trim_lf', 0)
            total_doors += elements.get('doors', 0)
            total_windows += elements.get('windows', 0)
    
    return {
        "total_rooms": len(all_rooms),
        "rooms": all_rooms,
        "aggregated_totals": {
            "wall_sqft": round(total_wall_sqft, 2),
            "ceiling_sqft": round(total_ceiling_sqft, 2),
            "trim_linear_feet": round(total_trim_lf, 2),
            "doors_count": total_doors,
            "windows_count": total_windows
        }
    }

def calculate_costs(aggregated):
    """Calculate costs using Rider Painting pricing"""
    
    totals = aggregated['aggregated_totals']
    
    # Walls
    wall_cost = totals['wall_sqft'] * PRICING_MODEL['gyp_walls']['cost_per_sqft']
    wall_markup = wall_cost * PRICING_MODEL['gyp_walls']['markup']
    wall_total = wall_cost + wall_markup
    
    # Ceilings
    ceiling_cost = totals['ceiling_sqft'] * PRICING_MODEL['gyp_ceilings']['cost_per_sqft']
    ceiling_markup = ceiling_cost * PRICING_MODEL['gyp_ceilings']['markup']
    ceiling_total = ceiling_cost + ceiling_markup
    
    # Trim
    trim_cost = totals['trim_linear_feet'] * PRICING_MODEL['base_trim']['cost_per_lf']
    trim_markup = trim_cost * PRICING_MODEL['base_trim']['markup']
    trim_total = trim_cost + trim_markup
    
    # Doors
    door_cost = totals['doors_count'] * PRICING_MODEL['doors']['cost_per_door']
    door_markup = door_cost * PRICING_MODEL['doors']['markup']
    door_total = door_cost + door_markup
    
    # Windows
    window_cost = totals['windows_count'] * PRICING_MODEL['windows']['cost_per_window']
    window_markup = window_cost * PRICING_MODEL['windows']['markup']
    window_total = window_cost + window_markup
    
    subtotal = wall_total + ceiling_total + trim_total + door_total + window_total
    
    return {
        "line_items": [
            {
                "item": f"Gyp. Walls - {totals['wall_sqft']:,.0f} sqft",
                "cost": wall_cost,
                "markup": wall_markup,
                "total": wall_total
            },
            {
                "item": f"Gyp. Ceilings - {totals['ceiling_sqft']:,.0f} sqft",
                "cost": ceiling_cost,
                "markup": ceiling_markup,
                "total": ceiling_total
            },
            {
                "item": f"Base Trim - {totals['trim_linear_feet']:,.0f} LF",
                "cost": trim_cost,
                "markup": trim_markup,
                "total": trim_total
            },
            {
                "item": f"Doors - {totals['doors_count']} EA",
                "cost": door_cost,
                "markup": door_markup,
                "total": door_total
            },
            {
                "item": f"Windows - {totals['windows_count']} EA",
                "cost": window_cost,
                "markup": window_markup,
                "total": window_total
            }
        ],
        "subtotal": round(subtotal, 2)
    }

def print_estimate(aggregated, costs):
    """Print the final estimate"""
    
    print("\n" + "="*80)
    print("🎨 PAINTING ESTIMATE FROM FLOOR PLANS")
    print("="*80)
    
    print(f"\n📐 ROOMS ANALYZED: {aggregated['total_rooms']}")
    
    print("\n📊 AGGREGATED MEASUREMENTS:")
    totals = aggregated['aggregated_totals']
    print(f"  • Walls: {totals['wall_sqft']:,.0f} sqft")
    print(f"  • Ceilings: {totals['ceiling_sqft']:,.0f} sqft")
    print(f"  • Base Trim: {totals['trim_linear_feet']:,.0f} LF")
    print(f"  • Doors: {totals['doors_count']}")
    print(f"  • Windows: {totals['windows_count']}")
    
    print("\n💰 COST BREAKDOWN:")
    print(f"{'ITEM':<40} {'COST':>12} {'MARKUP':>10} {'TOTAL':>12}")
    print("-" * 80)
    
    for item in costs['line_items']:
        print(f"{item['item']:<40} ${item['cost']:>11,.2f} ${item['markup']:>9,.2f} ${item['total']:>11,.2f}")
    
    print("=" * 80)
    print(f"{'TOTAL PROJECT COST:':<63} ${costs['subtotal']:>11,.2f}")
    print("=" * 80)

def main():
    """Main entry point"""
    
    if len(sys.argv) < 4:
        print("Nightshift AI - Floor Plan Analyzer")
        print("\nUsage:")
        print('  python3 analyze_painting_rfp.py --rfp_file "plans.pdf" --contact_name "Name" --contact_email "email"')
        sys.exit(1)
    
    args = {}
    for i in range(1, len(sys.argv), 2):
        if sys.argv[i].startswith('--'):
            args[sys.argv[i][2:]] = sys.argv[i+1]
    
    pdf_file = args.get('rfp_file')
    
    print("🎨 NIGHTSHIFT AI - FLOOR PLAN ANALYZER")
    print("="*80)
    print("Analyzes architectural floor plans as images")
    print("Identifies rooms, reads scales, calculates measurements")
    print("="*80)
    
    # Convert PDF to images
    images = convert_pdf_to_images(pdf_file, max_pages=20)
    
    if not images:
        print("❌ Could not convert PDF to images")
        sys.exit(1)
    
    # Analyze each page
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    all_pages_data = []
    
    print(f"\n🔍 Analyzing {len(images)} pages...")
    
    for i, image in enumerate(images, 1):
        print(f"\n  Analyzing page {i}/{len(images)}...")
        
        try:
            result_text = analyze_floor_plan_page(client, image, i)
            
            import re
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            
            if json_match:
                page_data = json.loads(json_match.group())
                
                if page_data.get('is_floor_plan') == False:
                    print(f"    ⚠️  Page {i}: {page_data.get('page_type', 'Not a floor plan')}")
                else:
                    room_count = len(page_data.get('rooms', []))
                    print(f"    ✅ Page {i}: Found {room_count} rooms")
                    all_pages_data.append(page_data)
            
        except Exception as e:
            print(f"    ❌ Error on page {i}: {e}")
            continue
    
    if not all_pages_data:
        print("\n❌ No floor plans found in PDF")
        sys.exit(1)
    
    # Aggregate measurements
    print("\n📊 Aggregating measurements across all floors...")
    aggregated = aggregate_measurements(all_pages_data)
    
    # Calculate costs
    print("💰 Calculating costs...")
    costs = calculate_costs(aggregated)
    
    # Print estimate
    print_estimate(aggregated, costs)
    
    # Save results
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = f"output/floorplan_analysis_{timestamp}.json"
    
    with open(output_file, 'w') as f:
        json.dump({
            "contact": {"name": args.get('contact_name'), "email": args.get('contact_email')},
            "document": pdf_file,
            "generated": datetime.now().isoformat(),
            "pages_analyzed": len(all_pages_data),
            "all_pages": all_pages_data,
            "aggregated_measurements": aggregated,
            "cost_estimate": costs
        }, f, indent=2)
    
    print(f"\n📁 Saved to: {output_file}")

if __name__ == "__main__":
    main()
