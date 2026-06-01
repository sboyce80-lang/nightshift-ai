#!/usr/bin/env python3
"""
Nightshift AI - Painting Work Extractor with NY State Market Rates
===================================================================
Extracts ONLY painting work from ANY RFP (mixed construction projects)
Uses NY State market rates for accurate pricing
Ignores all other trades (landscaping, plumbing, electrical, etc.)

Usage:
    python3 analyze_painting_rfp.py --rfp_file "rfp.pdf" --contact_name "Client Name" --contact_email "client@email.com"

Author: Nightshift AI
Version: 1.0
Date: February 2026
"""

import sys
import json
from config import CLAUDE_API_KEY
import anthropic
import PyPDF2
from datetime import datetime
import os
import time

def extract_pdf_text(pdf_path):
    """Extract text from PDF file"""
    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            text = ""
            for page in reader.pages:
                text += page.extract_text()
            return text
    except Exception as e:
        print(f"❌ Error reading PDF: {e}")
        return ""

def get_ny_state_rates():
    """
    NY State painting contractor rates (2026)
    These are industry-standard rates for the Hudson Valley region
    """
    return {
        "paint_costs_per_gallon": {
            "interior_standard": 45,
            "interior_premium": 60,
            "exterior_standard": 57,
            "exterior_premium": 72,
            "primer": 38
        },
        "labor_rates": {
            "lead_painter_per_hour": 67,
            "helper_per_hour": 35,
            "production_sqft_per_hour": 175
        },
        "material_costs": {
            "brushes_rollers": 150,
            "drop_cloths": 100,
            "tape_masking": 75,
            "sundries": 125
        },
        "all_inclusive_rates_per_sqft": {
            "interior_walls_standard": 3.75,
            "interior_walls_heavy_prep": 5.50,
            "ceilings": 3.00,
            "trim_per_linear_foot": 6.50,
            "door_each": 110,
            "exterior_walls": 5.00
        }
    }

def analyze_painting_work(client, rfp_text):
    """
    Analyze RFP and extract ONLY painting-related work
    Ignores all other construction trades
    """
    
    # Limit text to 3000 chars to avoid rate limits
    rfp_excerpt = rfp_text[:3000]
    
    ny_rates = get_ny_state_rates()
    
    prompt = f"""You are a PAINTING CONTRACTOR analyzing a construction RFP.

Extract ONLY the PAINTING work from this RFP. IGNORE everything else.

RFP TEXT (first 3000 characters):
{rfp_excerpt}

PAINTING WORK INCLUDES:
- Painting walls, ceilings, trim, doors, windows
- Surface preparation (patching, sanding, priming)
- Interior or exterior painting
- Staining wood surfaces

IGNORE (do NOT include in estimate):
- Landscaping, trees, plants, irrigation
- Parking lots, paving, asphalt
- Plumbing, pipes, drainage
- Electrical, wiring, lighting fixtures
- HVAC, mechanical systems
- Structural work, framing
- Site work, excavation, grading
- Architectural design, permits
- Any specialty systems

NY STATE PRICING TO USE:
- Paint: ${ny_rates['paint_costs_per_gallon']['interior_premium']}/gal
- Labor: ${ny_rates['labor_rates']['lead_painter_per_hour']}/hr (lead), ${ny_rates['labor_rates']['helper_per_hour']}/hr (helper)
- Interior walls: ${ny_rates['all_inclusive_rates_per_sqft']['interior_walls_standard']}/sqft
- Ceilings: ${ny_rates['all_inclusive_rates_per_sqft']['ceilings']}/sqft
- Trim: ${ny_rates['all_inclusive_rates_per_sqft']['trim_per_linear_foot']}/LF
- Doors: ${ny_rates['all_inclusive_rates_per_sqft']['door_each']} each

Return this exact JSON structure:
{{
  "painting_work_found": true or false,
  "painting_scope_description": "Describe ONLY the painting work",
  
  "surfaces_to_paint": {{
    "interior_walls": {{
      "sqft": 0,
      "location": "where",
      "condition": "new/repaint/heavy_prep",
      "coats": 2
    }},
    "ceilings": {{
      "sqft": 0,
      "coats": 2
    }},
    "trim_baseboards": {{
      "linear_feet": 0,
      "coats": 2
    }},
    "doors": {{
      "count": 0,
      "type": "interior/exterior"
    }},
    "windows": {{
      "count": 0
    }},
    "exterior_walls": {{
      "sqft": 0,
      "material": "siding/stucco/brick"
    }}
  }},
  
  "cost_breakdown": {{
    "prep_work": {{
      "hours": 0,
      "cost": 0
    }},
    "primer": {{
      "gallons": 0,
      "material_cost": 0,
      "labor_hours": 0,
      "labor_cost": 0,
      "total": 0
    }},
    "paint": {{
      "interior_walls": {{
        "sqft": 0,
        "gallons": 0,
        "material_cost": 0,
        "labor_hours": 0,
        "labor_cost": 0,
        "total": 0
      }},
      "ceilings": {{
        "sqft": 0,
        "total": 0
      }},
      "trim_doors": {{
        "total": 0
      }},
      "exterior": {{
        "total": 0
      }}
    }},
    "supplies": {{
      "total": 450
    }},
    "labor_total": 0,
    "materials_total": 0,
    "subtotal": 0,
    "overhead_15_percent": 0,
    "profit_20_percent": 0,
    "total_project_cost": 0
  }},
  
  "timeline": {{
    "total_days": 0,
    "crew_size": 2
  }},
  
  "clarifying_questions": [
    "Question 1 about painting?",
    "Question 2?",
    "Question 3?"
  ],
  
  "assumptions": [
    "Assumption 1",
    "Assumption 2"
  ],
  
  "exclusions": [
    "What painting work is NOT included"
  ],
  
  "non_painting_work_noted": [
    "List other work in RFP that we are NOT bidding on"
  ]
}}

If NO painting work found:
{{
  "painting_work_found": false,
  "reason": "This RFP contains no painting work",
  "non_painting_work_noted": ["What the RFP is actually for"]
}}

Calculate costs using NY State rates provided above. Include 15% overhead and 20% profit margin."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return message.content[0].text
        
    except anthropic.RateLimitError:
        print("\n⚠️  Rate limit hit. Wait 60 seconds and try again.")
        raise
    except Exception as e:
        print(f"\n❌ Error calling Claude API: {e}")
        raise

def format_currency(amount):
    """Format number as currency"""
    return f"${amount:,.2f}"

def print_proposal(result):
    """Print the painting proposal to console"""
    
    if not result.get('painting_work_found'):
        print("\n" + "="*70)
        print("❌ NO PAINTING WORK FOUND IN THIS RFP")
        print("="*70)
        print(f"\nReason: {result.get('reason', 'No painting work identified')}")
        
        non_painting = result.get('non_painting_work_noted', [])
        if non_painting:
            print(f"\n📋 This RFP appears to be for:")
            for item in non_painting[:5]:
                print(f"   • {item}")
        
        print(f"\n⚠️  No painting estimate generated.")
        print(f"⚠️  This project is outside our scope of work.")
        return
    
    # Painting work was found!
    print("\n" + "="*70)
    print("🎨 PAINTING WORK ESTIMATE (NY STATE RATES)")
    print("="*70)
    
    # Scope
    print(f"\n📋 PAINTING SCOPE:")
    print(f"   {result.get('painting_scope_description', 'N/A')}")
    
    # Surfaces
    surfaces = result.get('surfaces_to_paint', {})
    print(f"\n📐 SURFACES TO PAINT:")
    
    total_sqft = 0
    if surfaces.get('interior_walls', {}).get('sqft', 0) > 0:
        sqft = surfaces['interior_walls']['sqft']
        total_sqft += sqft
        print(f"   • Interior Walls: {sqft:,} sqft")
        print(f"     Condition: {surfaces['interior_walls'].get('condition', 'standard')}")
        print(f"     Coats: {surfaces['interior_walls'].get('coats', 2)}")
    
    if surfaces.get('ceilings', {}).get('sqft', 0) > 0:
        sqft = surfaces['ceilings']['sqft']
        total_sqft += sqft
        print(f"   • Ceilings: {sqft:,} sqft")
    
    if surfaces.get('trim_baseboards', {}).get('linear_feet', 0) > 0:
        lf = surfaces['trim_baseboards']['linear_feet']
        print(f"   • Trim/Baseboards: {lf:,} linear feet")
    
    if surfaces.get('doors', {}).get('count', 0) > 0:
        count = surfaces['doors']['count']
        print(f"   • Doors: {count} ({surfaces['doors'].get('type', 'interior')})")
    
    if surfaces.get('windows', {}).get('count', 0) > 0:
        count = surfaces['windows']['count']
        print(f"   • Windows: {count}")
    
    if surfaces.get('exterior_walls', {}).get('sqft', 0) > 0:
        sqft = surfaces['exterior_walls']['sqft']
        total_sqft += sqft
        print(f"   • Exterior Walls: {sqft:,} sqft")
    
    if total_sqft > 0:
        print(f"\n   TOTAL: {total_sqft:,} sqft to paint")
    
    # Cost breakdown
    costs = result.get('cost_breakdown', {})
    
    print(f"\n" + "="*70)
    print("💰 DETAILED COST BREAKDOWN (NY STATE RATES)")
    print("="*70)
    
    if costs.get('prep_work', {}).get('cost', 0) > 0:
        prep = costs['prep_work']
        print(f"\n1️⃣  PREP WORK: {format_currency(prep['cost'])}")
        if prep.get('hours', 0) > 0:
            print(f"    {prep['hours']} hours")
    
    if costs.get('primer', {}).get('total', 0) > 0:
        primer = costs['primer']
        print(f"\n2️⃣  PRIMER: {format_currency(primer['total'])}")
        if primer.get('gallons', 0) > 0:
            print(f"    Material: {primer['gallons']} gal = {format_currency(primer.get('material_cost', 0))}")
            print(f"    Labor: {primer.get('labor_hours', 0)} hrs = {format_currency(primer.get('labor_cost', 0))}")
    
    paint = costs.get('paint', {})
    if paint:
        print(f"\n3️⃣  PAINT:")
        
        for surface_name, surface_data in paint.items():
            if isinstance(surface_data, dict) and surface_data.get('total', 0) > 0:
                print(f"\n    {surface_name.replace('_', ' ').title()}: {format_currency(surface_data['total'])}")
                if surface_data.get('sqft', 0) > 0:
                    print(f"      {surface_data['sqft']:,} sqft")
                if surface_data.get('gallons', 0) > 0:
                    print(f"      {surface_data['gallons']} gallons paint")
    
    if costs.get('supplies', {}).get('total', 0) > 0:
        print(f"\n4️⃣  SUPPLIES: {format_currency(costs['supplies']['total'])}")
    
    # Totals
    print(f"\n" + "="*70)
    print(f"SUBTOTAL: {format_currency(costs.get('subtotal', 0))}")
    print(f"  Materials: {format_currency(costs.get('materials_total', 0))}")
    print(f"  Labor: {format_currency(costs.get('labor_total', 0))}")
    print(f"\nOverhead (15%): {format_currency(costs.get('overhead_15_percent', 0))}")
    print(f"Profit (20%): {format_currency(costs.get('profit_20_percent', 0))}")
    print(f"\n" + "="*70)
    print(f"TOTAL PAINTING COST: {format_currency(costs.get('total_project_cost', 0))}")
    print(f"" + "="*70)
    
    # Timeline
    timeline = result.get('timeline', {})
    print(f"\n⏱️  TIMELINE: {timeline.get('total_days', 0)} working days")
    print(f"   Crew: {timeline.get('crew_size', 2)} painters")
    
    # Questions
    questions = result.get('clarifying_questions', [])
    if questions:
        print(f"\n❓ CLARIFYING QUESTIONS:")
        for i, q in enumerate(questions, 1):
            print(f"   {i}. {q}")
    
    # Assumptions
    assumptions = result.get('assumptions', [])
    if assumptions:
        print(f"\n📝 ASSUMPTIONS:")
        for assumption in assumptions:
            print(f"   • {assumption}")
    
    # Exclusions
    exclusions = result.get('exclusions', [])
    if exclusions:
        print(f"\n🚫 EXCLUSIONS:")
        for exclusion in exclusions:
            print(f"   • {exclusion}")
    
    # Other work
    non_painting = result.get('non_painting_work_noted', [])
    if non_painting:
        print(f"\n📋 OTHER WORK IN RFP (NOT INCLUDED IN THIS ESTIMATE):")
        for item in non_painting[:10]:
            print(f"   • {item}")
        if len(non_painting) > 10:
            print(f"   ... and {len(non_painting) - 10} more items")

def main():
    """Main entry point"""
    
    # Parse command line arguments
    if len(sys.argv) < 4:
        print("Nightshift AI - Painting Work Extractor")
        print("\nUsage:")
        print('  python3 analyze_painting_rfp.py --rfp_file "file.pdf" --contact_name "Name" --contact_email "email@example.com"')
        print("\nExample:")
        print('  python3 analyze_painting_rfp.py --rfp_file "rfp.pdf" --contact_name "John Smith" --contact_email "john@email.com"')
        sys.exit(1)
    
    args = {}
    for i in range(1, len(sys.argv), 2):
        if sys.argv[i].startswith('--'):
            args[sys.argv[i][2:]] = sys.argv[i+1]
    
    rfp_file = args.get('rfp_file')
    contact_name = args.get('contact_name')
    contact_email = args.get('contact_email')
    
    # Display header
    print("🎨 NIGHTSHIFT AI - PAINTING WORK EXTRACTOR")
    print("="*70)
    print("Extracts ONLY painting work from ANY RFP")
    print("Uses NY State market rates for accurate pricing")
    print("Ignores: landscaping, plumbing, electrical, site work, etc.")
    print("="*70)
    
    # Initialize Claude client
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    # Extract PDF text
    print(f"\n📄 Reading RFP: {rfp_file}")
    rfp_text = extract_pdf_text(rfp_file)
    
    if not rfp_text:
        print("❌ Could not extract text from PDF")
        sys.exit(1)
    
    print(f"✅ Extracted {len(rfp_text):,} characters")
    
    # Analyze for painting work
    print("\n🔍 Analyzing RFP for painting work...")
    print("   (Using Claude AI with NY State pricing)")
    
    try:
        result_text = analyze_painting_work(client, rfp_text)
        
        # Parse JSON response
        import re
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        
        if json_match:
            result = json.loads(json_match.group())
        else:
            print("\n⚠️  Could not parse response from AI")
            print("Raw response:")
            print(result_text[:500])
            sys.exit(1)
        
        # Print the proposal
        print_proposal(result)
        
        # Save to file if painting work was found
        if result.get('painting_work_found'):
            os.makedirs("output", exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"output/painting_extract_{timestamp}.json"
            
            output_data = {
                "contact": {
                    "name": contact_name,
                    "email": contact_email
                },
                "rfp_file": rfp_file,
                "generated_at": datetime.now().isoformat(),
                "ny_state_rates": get_ny_state_rates(),
                "analysis": result
            }
            
            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            print(f"\n📁 Complete results saved to: {output_file}")
            print(f"\n✅ Ready to generate formal Rider Painting estimate!")
        
    except anthropic.RateLimitError:
        print("\n❌ API Rate Limit Exceeded")
        print("   Your API key has hit the rate limit (30,000 tokens/minute)")
        print("   Wait 60 seconds and try again")
        sys.exit(1)
        
    except Exception as e:
        print(f"\n❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
