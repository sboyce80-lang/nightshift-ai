#!/usr/bin/env python3
"""
Nightshift AI - Text-Based Construction Analyzer
================================================
Extracts text from PDF and analyzes for painting measurements
Works with ANY PDF file
"""

import sys
import json
from config import CLAUDE_API_KEY
import anthropic
import PyPDF2
from datetime import datetime
import os
import re

# Rider Painting pricing
PRICING_MODEL = {
    "gyp_walls": {"cost_per_sqft": 0.80, "markup": 0.06},
    "gyp_ceilings": {"cost_per_sqft": 0.80, "markup": 0.06},
    "base_trim": {"cost_per_lf": 1.15, "markup": 0.06},
    "doors": {"cost_per_door": 150.00, "markup": 0.06},
    "windows": {"cost_per_window": 425.00, "markup": 0.06}
}

def extract_pdf_text(pdf_path):
    """Extract all text from PDF"""
    try:
        print(f"\n📄 Extracting text from PDF...")
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            text = ""
            total_pages = len(reader.pages)
            
            print(f"   Found {total_pages} pages")
            
            for i, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                text += f"\n\n=== PAGE {i} ===\n\n{page_text}"
                
                if i % 5 == 0:
                    print(f"   Processed {i}/{total_pages} pages...")
            
            print(f"✅ Extracted {len(text):,} characters from {total_pages} pages")
            return text
    except Exception as e:
        print(f"❌ Error reading PDF: {e}")
        return ""

def find_measurements_in_text(text):
    """
    Find measurement patterns in text
    Look for things like: 20'-0", 15.5', 300 SF, etc.
    """
    
    patterns = {
        "dimensions": r"(\d+(?:\.\d+)?)'[\s-]*(\d+(?:\.\d+)?)?\"?",  # 20'-6" or 20'
        "square_feet": r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:SF|SQ\.?\s*FT\.?|SQFT)",
        "linear_feet": r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:LF|LIN\.?\s*FT\.?)",
        "ceiling_height": r"(?:CLG|CEILING)\s*(?:HT|HEIGHT)[\s:]*(\d+)'[\s-]*(\d+)?",
        "room_names": r"(?:ROOM|BEDROOM|KITCHEN|BATHROOM|LIVING|DINING|OFFICE|STORAGE|CLOSET|HALLWAY)"
    }
    
    findings = {
        "dimensions_found": [],
        "square_feet_found": [],
        "linear_feet_found": [],
        "ceiling_heights_found": [],
        "room_names_found": []
    }
    
    # Find dimensions
    for match in re.finditer(patterns["dimensions"], text, re.IGNORECASE):
        findings["dimensions_found"].append(match.group(0))
    
    # Find square feet
    for match in re.finditer(patterns["square_feet"], text, re.IGNORECASE):
        findings["square_feet_found"].append(match.group(0))
    
    # Find linear feet
    for match in re.finditer(patterns["linear_feet"], text, re.IGNORECASE):
        findings["linear_feet_found"].append(match.group(0))
    
    # Find ceiling heights
    for match in re.finditer(patterns["ceiling_height"], text, re.IGNORECASE):
        findings["ceiling_heights_found"].append(match.group(0))
    
    # Find room names
    for match in re.finditer(patterns["room_names"], text, re.IGNORECASE):
        findings["room_names_found"].append(match.group(0))
    
    return findings

def analyze_construction_text(client, text):
    """Analyze extracted text for painting measurements"""
    
    # Limit text to avoid token limits (use first 6000 chars)
    text_excerpt = text[:6000]
    
    prompt = f"""You are analyzing TEXT EXTRACTED from a CONSTRUCTION/ARCHITECTURAL PDF.

The text may contain:
- Room schedules with dimensions
- Floor plans with dimension callouts
- Material legends
- Specifications
- Door/window schedules

YOUR TASK: Extract ALL measurements for a PAINTING ESTIMATE.

TEXT FROM PDF:
{text_excerpt}

EXTRACT:

1. ROOMS & DIMENSIONS:
   - Find room names (Living Room, Bedroom, Kitchen, etc.)
   - Find room dimensions (20' × 15', 300 SF, etc.)
   - Find ceiling heights (9', 9'-6", etc.)
   - If you find a room schedule table, extract ALL rows

2. WALL INFORMATION:
   - Total wall square footage or linear footage
   - Wall heights
   - Wall types (GYP, CMU, etc.) - note which are paintable

3. CEILING INFORMATION:
   - Total ceiling square footage
   - Ceiling types - note which are paintable

4. DOORS:
   - Total door count
   - Door types (HM, wood, glass, etc.)

5. WINDOWS:
   - Total window count

6. TRIM:
   - Base trim linear footage

7. CALCULATE TOTALS:
   - Sum all room areas to get total floor area
   - Calculate wall area (perimeter × height) for each room
   - Sum ceiling areas
   - Count all doors and windows

Return JSON:
{{
  "measurements_found": true/false,
  
  "rooms": [
    {{
      "name": "Living Room",
      "dimensions": "20' × 15'",
      "floor_area_sqft": 300,
      "ceiling_height_feet": 9,
      "wall_area_sqft": 630,
      "ceiling_area_sqft": 300,
      "doors": 2,
      "windows": 3
    }}
  ],
  
  "totals": {{
    "total_wall_sqft": 0,
    "total_ceiling_sqft": 0,
    "total_base_trim_lf": 0,
    "total_doors": 0,
    "total_windows": 0
  }},
  
  "material_info": [
    "List any material/legend information found"
  ],
  
  "notes": [
    "Important notes or assumptions made"
  ],
  
  "missing_info": [
    "What information couldn't be found in the text"
  ]
}}

If NO measurements found:
{{
  "measurements_found": false,
  "reason": "why measurements weren't found",
  "text_contains": "brief description of what the text actually contains"
}}

IMPORTANT:
- Extract EXACT numbers from the text
- If you find partial information, note what's missing
- Be conservative - don't guess or estimate
- Calculate totals where possible"""

    print("\n🔍 Analyzing text for measurements...")
    
    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return message.content[0].text
        
    except Exception as e:
        print(f"❌ Error analyzing text: {e}")
        raise

def calculate_costs(totals):
    """Calculate costs using Rider Painting pricing"""
    
    wall_sqft = totals.get('total_wall_sqft', 0)
    ceiling_sqft = totals.get('total_ceiling_sqft', 0)
    trim_lf = totals.get('total_base_trim_lf', 0)
    doors = totals.get('total_doors', 0)
    windows = totals.get('total_windows', 0)
    
    # Walls
    wall_cost = wall_sqft * PRICING_MODEL['gyp_walls']['cost_per_sqft']
    wall_markup = wall_cost * PRICING_MODEL['gyp_walls']['markup']
    wall_total = wall_cost + wall_markup
    
    # Ceilings
    ceiling_cost = ceiling_sqft * PRICING_MODEL['gyp_ceilings']['cost_per_sqft']
    ceiling_markup = ceiling_cost * PRICING_MODEL['gyp_ceilings']['markup']
    ceiling_total = ceiling_cost + ceiling_markup
    
    # Trim
    trim_cost = trim_lf * PRICING_MODEL['base_trim']['cost_per_lf']
    trim_markup = trim_cost * PRICING_MODEL['base_trim']['markup']
    trim_total = trim_cost + trim_markup
    
    # Doors
    door_cost = doors * PRICING_MODEL['doors']['cost_per_door']
    door_markup = door_cost * PRICING_MODEL['doors']['markup']
    door_total = door_cost + door_markup
    
    # Windows
    window_cost = windows * PRICING_MODEL['windows']['cost_per_window']
    window_markup = window_cost * PRICING_MODEL['windows']['markup']
    window_total = window_cost + window_markup
    
    subtotal = wall_total + ceiling_total + trim_total + door_total + window_total
    
    return {
        "line_items": [
            {"item": f"Gyp. Walls - {wall_sqft:,.0f} sqft", "total": round(wall_total, 2)},
            {"item": f"Gyp. Ceilings - {ceiling_sqft:,.0f} sqft", "total": round(ceiling_total, 2)},
            {"item": f"Base Trim - {trim_lf:,.0f} LF", "total": round(trim_total, 2)},
            {"item": f"Doors - {doors} EA", "total": round(door_total, 2)},
            {"item": f"Windows - {windows} EA", "total": round(window_total, 2)}
        ],
        "subtotal": round(subtotal, 2)
    }

def print_estimate(analysis, costs):
    """Print estimate"""
    
    print("\n" + "="*80)
    print("🎨 PAINTING ESTIMATE")
    print("="*80)
    
    print(f"\n📐 ROOMS FOUND: {len(analysis.get('rooms', []))}")
    
    for room in analysis.get('rooms', []):
        print(f"\n  • {room.get('name', 'Unknown')}")
        print(f"    Dimensions: {room.get('dimensions', 'N/A')}")
        print(f"    Floor: {room.get('floor_area_sqft', 0):,.0f} sqft")
        print(f"    Walls: {room.get('wall_area_sqft', 0):,.0f} sqft")
        print(f"    Ceiling: {room.get('ceiling_area_sqft', 0):,.0f} sqft")
    
    totals = analysis.get('totals', {})
    print(f"\n📊 TOTALS:")
    print(f"  • Walls: {totals.get('total_wall_sqft', 0):,.0f} sqft")
    print(f"  • Ceilings: {totals.get('total_ceiling_sqft', 0):,.0f} sqft")
    print(f"  • Base Trim: {totals.get('total_base_trim_lf', 0):,.0f} LF")
    print(f"  • Doors: {totals.get('total_doors', 0)}")
    print(f"  • Windows: {totals.get('total_windows', 0)}")
    
    print(f"\n💰 COST BREAKDOWN:")
    for item in costs['line_items']:
        if item['total'] > 0:
            print(f"  {item['item']:<40} ${item['total']:>11,.2f}")
    
    print("=" * 80)
    print(f"{'TOTAL:':<52} ${costs['subtotal']:>11,.2f}")
    print("=" * 80)
    
    missing = analysis.get('missing_info', [])
    if missing:
        print(f"\n⚠️  MISSING INFORMATION:")
        for info in missing:
            print(f"  • {info}")
    
    notes = analysis.get('notes', [])
    if notes:
        print(f"\n📝 NOTES:")
        for note in notes:
            print(f"  • {note}")

def main():
    """Main entry point"""
    
    if len(sys.argv) < 4:
        print("Usage: python3 analyze_painting_rfp.py --rfp_file FILE --contact_name NAME --contact_email EMAIL")
        sys.exit(1)
    
    args = {}
    for i in range(1, len(sys.argv), 2):
        if sys.argv[i].startswith('--'):
            args[sys.argv[i][2:]] = sys.argv[i+1]
    
    print("🎨 NIGHTSHIFT AI - TEXT-BASED ANALYZER")
    print("="*80)
    
    # Extract text
    text = extract_pdf_text(args.get('rfp_file'))
    if not text:
        sys.exit(1)
    
    # Quick scan for measurement patterns
    print("\n🔍 Scanning for measurement patterns...")
    patterns = find_measurements_in_text(text)
    
    print(f"  • Found {len(patterns['dimensions_found'])} dimension callouts")
    print(f"  • Found {len(patterns['square_feet_found'])} square foot measurements")
    print(f"  • Found {len(patterns['room_names_found'])} room references")
    
    # Analyze with Claude
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    try:
        result_text = analyze_construction_text(client, text)
        
        import re
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        
        if json_match:
            analysis = json.loads(json_match.group())
        else:
            print("\n⚠️  Could not parse analysis")
            sys.exit(1)
        
        if not analysis.get('measurements_found'):
            print(f"\n❌ NO MEASUREMENTS FOUND")
            print(f"Reason: {analysis.get('reason', 'Unknown')}")
            print(f"Text contains: {analysis.get('text_contains', 'Unknown')}")
            sys.exit(0)
        
        # Calculate costs
        costs = calculate_costs(analysis.get('totals', {}))
        
        # Print estimate
        print_estimate(analysis, costs)
        
        # Save
        os.makedirs("output", exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = f"output/text_analysis_{timestamp}.json"
        
        with open(output_file, 'w') as f:
            json.dump({
                "contact": {"name": args.get('contact_name'), "email": args.get('contact_email')},
                "document": args.get('rfp_file'),
                "generated": datetime.now().isoformat(),
                "analysis": analysis,
                "costs": costs
            }, f, indent=2)
        
        print(f"\n📁 Saved to: {output_file}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
