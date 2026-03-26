#!/usr/bin/env python3
"""
Nightshift AI - Painting Work Extractor with NY State Market Rate Research
===========================================================================
Analyzes ANY RFP and extracts ONLY the painting-related work
Researches current NY State market rates before generating estimates
Ignores all other trades (landscaping, plumbing, electrical, etc.)

Usage:
    python3 analyze_painting_rfp.py --rfp_file FILE --contact_name NAME --contact_email EMAIL
"""
import sys
import json
from config import CLAUDE_API_KEY
import anthropic
import PyPDF2
from datetime import datetime
import os

def extract_pdf_text(pdf_path):
    """Extract text from PDF"""
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

def research_ny_state_rates(client):
    """Research current NY State painting rates using web search"""
    
    print("\n💰 Researching current NY State market rates...")
    print("   (This may take 30-60 seconds...)")
    
    prompt = """Research current 2025-2026 painting contractor rates specifically for NEW YORK STATE.

Find and provide:

1. PAINT COSTS (per gallon in NY State):
   - Interior latex paint (standard/premium brands like Benjamin Moore, Sherwin-Williams)
   - Exterior acrylic paint
   - Primer
   - Specialty paints

2. LABOR RATES (per hour in NY State):
   - Lead/master painter hourly rate
   - Helper/apprentice hourly rate
   - Typical production rates (sqft painted per hour)

3. MATERIAL COSTS (in NY State):
   - Brushes, rollers, and applicators
   - Drop cloths and protection materials
   - Tape, masking supplies
   - Caulking and patching materials
   - Sundries

4. ALL-INCLUSIVE RATES (per sqft in NY State):
   - Interior walls (standard repaint)
   - Interior walls (heavy prep)
   - Ceilings
   - Trim and doors (per linear foot / per door)
   - Exterior painting

Use web search to find CURRENT 2025-2026 rates. Look for:
- HomeAdvisor data for NY
- Contractor associations
- Material supplier pricing (Benjamin Moore, Sherwin-Williams NY stores)
- NY State specific painting contractor data

Return JSON:
{
  "paint_costs_per_gallon": {
    "interior_standard": {"low": 40, "high": 50, "typical": 45},
    "interior_premium": {"low": 50, "high": 70, "typical": 60},
    "exterior_standard": {"low": 50, "high": 65, "typical": 57},
    "exterior_premium": {"low": 60, "high": 85, "typical": 72},
    "primer": {"low": 30, "high": 45, "typical": 38}
  },
  "labor_rates_ny": {
    "lead_painter_per_hour": {"low": 60, "high": 75, "typical": 67},
    "helper_per_hour": {"low": 30, "high": 40, "typical": 35},
    "production_rate_sqft_per_hour": {"walls": 175, "ceilings": 200, "trim": 25}
  },
  "material_costs": {
    "brushes_rollers_per_project": {"typical": 150},
    "drop_cloths": {"typical": 100},
    "tape_masking": {"typical": 75},
    "sundries": {"typical": 125}
  },
  "all_inclusive_rates_per_sqft": {
    "interior_walls_standard": {"low": 3.00, "high": 4.50, "typical": 3.75},
    "interior_walls_heavy_prep": {"low": 4.50, "high": 6.50, "typical": 5.50},
    "ceilings": {"low": 2.50, "high": 3.50, "typical": 3.00},
    "trim_per_linear_foot": {"low": 5.00, "high": 8.00, "typical": 6.50},
    "doors_each": {"low": 75, "high": 150, "typical": 110},
    "exterior_walls": {"low": 4.00, "high": 6.00, "typical": 5.00}
  },
  "sources": ["List your sources"],
  "last_updated": "2026-02-17"
}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search"
            }],
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Extract text from response
        response_text = ""
        for block in message.content:
            if block.type == "text":
                response_text += block.text
        
        # Parse JSON
        import re
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            rates = json.loads(json_match.group())
            print("✅ NY State market rates retrieved successfully")
            return rates
        else:
            print("⚠️  Using default NY State estimates")
            return get_default_ny_rates()
            
    except Exception as e:
        print(f"⚠️  Rate research unavailable, using defaults: {e}")
        return get_default_ny_rates()

def get_default_ny_rates():
    """Default NY State rates if research fails"""
    return {
        "paint_costs_per_gallon": {
            "interior_standard": {"low": 40, "high": 50, "typical": 45},
            "interior_premium": {"low": 50, "high": 70, "typical": 60},
            "exterior_standard": {"low": 50, "high": 65, "typical": 57},
            "exterior_premium": {"low": 60, "high": 85, "typical": 72},
            "primer": {"low": 30, "high": 45, "typical": 38}
        },
        "labor_rates_ny": {
            "lead_painter_per_hour": {"low": 60, "high": 75, "typical": 67},
            "helper_per_hour": {"low": 30, "high": 40, "typical": 35},
            "production_rate_sqft_per_hour": {"walls": 175, "ceilings": 200, "trim": 25}
        },
        "material_costs": {
            "brushes_rollers_per_project": {"typical": 150},
            "drop_cloths": {"typical": 100},
            "tape_masking": {"typical": 75},
            "sundries": {"typical": 125}
        },
        "all_inclusive_rates_per_sqft": {
            "interior_walls_standard": {"low": 3.00, "high": 4.50, "typical": 3.75},
            "interior_walls_heavy_prep": {"low": 4.50, "high": 6.50, "typical": 5.50},
            "ceilings": {"low": 2.50, "high": 3.50, "typical": 3.00},
            "trim_per_linear_foot": {"low": 5.00, "high": 8.00, "typical": 6.50},
            "doors_each": {"low": 75, "high": 150, "typical": 110},
            "exterior_walls": {"low": 4.00, "high": 6.00, "typical": 5.00}
        },
        "sources": ["Default NY State estimates"],
        "last_updated": "2026-02-17"
    }

def analyze_painting_only(client, rfp_text, ny_rates):
    """Extract ONLY painting work from RFP using NY State market rates"""
    
    # Format rates for the prompt
    rates_summary = f"""
NY STATE CURRENT MARKET RATES (Use these for your estimate):

PAINT COSTS (per gallon):
- Interior Standard: ${ny_rates['paint_costs_per_gallon']['interior_standard']['typical']}/gal
- Interior Premium: ${ny_rates['paint_costs_per_gallon']['interior_premium']['typical']}/gal
- Exterior Standard: ${ny_rates['paint_costs_per_gallon']['exterior_standard']['typical']}/gal
- Exterior Premium: ${ny_rates['paint_costs_per_gallon']['exterior_premium']['typical']}/gal
- Primer: ${ny_rates['paint_costs_per_gallon']['primer']['typical']}/gal

LABOR RATES (per hour):
- Lead Painter: ${ny_rates['labor_rates_ny']['lead_painter_per_hour']['typical']}/hr
- Helper: ${ny_rates['labor_rates_ny']['helper_per_hour']['typical']}/hr
- Production: {ny_rates['labor_rates_ny']['production_rate_sqft_per_hour']['walls']} sqft/hr walls, {ny_rates['labor_rates_ny']['production_rate_sqft_per_hour']['ceilings']} sqft/hr ceilings

MATERIALS:
- Brushes/Rollers: ${ny_rates['material_costs']['brushes_rollers_per_project']['typical']}
- Drop Cloths: ${ny_rates['material_costs']['drop_cloths']['typical']}
- Tape/Masking: ${ny_rates['material_costs']['tape_masking']['typical']}
- Sundries: ${ny_rates['material_costs']['sundries']['typical']}

ALL-INCLUSIVE RATES (per sqft, includes labor + materials):
- Interior Walls (standard): ${ny_rates['all_inclusive_rates_per_sqft']['interior_walls_standard']['typical']}/sqft
- Interior Walls (heavy prep): ${ny_rates['all_inclusive_rates_per_sqft']['interior_walls_heavy_prep']['typical']}/sqft
- Ceilings: ${ny_rates['all_inclusive_rates_per_sqft']['ceilings']['typical']}/sqft
- Trim: ${ny_rates['all_inclusive_rates_per_sqft']['trim_per_linear_foot']['typical']}/LF
- Doors: ${ny_rates['all_inclusive_rates_per_sqft']['doors_each']['typical']} per door
- Exterior: ${ny_rates['all_inclusive_rates_per_sqft']['exterior_walls']['typical']}/sqft

USE THESE RATES for all cost calculations.
"""
    
    prompt = f"""You are a PAINTING CONTRACTOR in NEW YORK STATE reviewing a construction RFP.

Your job: Extract and estimate ONLY the PAINTING work from this RFP using CURRENT NY STATE MARKET RATES.

RFP TEXT:
{rfp_text[:5000]}

{rates_summary}

INSTRUCTIONS:
1. READ the entire RFP
2. IDENTIFY any painting-related work mentioned
3. IGNORE all other work (landscaping, plumbing, electrical, HVAC, site work, etc.)
4. Use the NY STATE RATES above for ALL cost calculations
5. If there's ANY painting work at all (even if it's only 1% of the total project), analyze it

PAINTING WORK INCLUDES:
- Painting walls, ceilings, trim, doors, windows
- Interior or exterior painting
- Surface preparation (patching, sanding, priming)
- Paint application (primer, finish coats)
- Staining or clear coating wood surfaces
- Touch-up work
- Repainting existing surfaces

IGNORE EVERYTHING ELSE:
- Do NOT include: landscaping, trees, plants, irrigation
- Do NOT include: parking lots, paving, asphalt, concrete work
- Do NOT include: plumbing, pipes, drainage systems
- Do NOT include: electrical work, wiring, lighting fixtures
- Do NOT include: HVAC, mechanical systems, ductwork
- Do NOT include: structural work, framing, foundations
- Do NOT include: site work, excavation, grading
- Do NOT include: architectural design fees, permits
- Do NOT include: specialty systems (Grasscrete, pet wash equipment, etc.)

COST CALCULATIONS - USE NY STATE RATES PROVIDED ABOVE:
- Calculate labor hours based on production rates provided
- Use NY State paint costs per gallon provided
- Use NY State labor rates provided
- Include 15% overhead and 20% profit margin

Return this EXACT JSON structure:
{{
  "painting_work_found": true/false,
  "painting_scope_description": "Detailed description of ONLY the painting work",
  "ny_state_rates_used": true,
  
  "surfaces_to_paint": {{
    "interior_walls": {{
      "sqft": 0,
      "location": "where (if specified)",
      "condition": "new/repaint/heavy_prep",
      "coats": 2
    }},
    "ceilings": {{"sqft": 0, "location": "", "coats": 2}},
    "trim_baseboards": {{"linear_feet": 0, "location": "", "coats": 2}},
    "doors": {{"count": 0, "type": "interior/exterior/both", "sides": 2}},
    "windows": {{"count": 0, "includes": "sashes/trim/both"}},
    "exterior_walls": {{"sqft": 0, "material": "siding/stucco/brick/etc", "coats": 2}},
    "other_surfaces": "any other paintable surfaces mentioned"
  }},
  
  "prep_requirements": ["List specific prep work needed"],
  
  "paint_specifications": {{
    "type": "interior latex/exterior acrylic/oil-based/etc",
    "sheen": "flat/eggshell/satin/semi-gloss/gloss",
    "brand_specified": "if mentioned",
    "colors": "specified or TBD"
  }},
  
  "detailed_cost_breakdown": {{
    "prep_work": {{
      "surface_repair": {{"description": "Patch holes, cracks", "hours": 0, "cost": 0}},
      "sanding_cleaning": {{"hours": 0, "cost": 0}},
      "caulking": {{"hours": 0, "cost": 0}},
      "masking_protection": {{"cost": 0}},
      "subtotal": 0
    }},
    "primer": {{
      "gallons_needed": 0,
      "ny_cost_per_gallon": 0,
      "material_cost": 0,
      "labor_hours": 0,
      "ny_labor_rate": 0,
      "labor_cost": 0,
      "subtotal": 0
    }},
    "paint_by_surface": {{
      "interior_walls": {{
        "sqft": 0,
        "gallons_needed": 0,
        "ny_cost_per_gallon": 0,
        "material_cost": 0,
        "labor_hours": 0,
        "ny_labor_rate": 0,
        "labor_cost": 0,
        "subtotal": 0
      }},
      "ceilings": {{"sqft": 0, "gallons_needed": 0, "material_cost": 0, "labor_hours": 0, "labor_cost": 0, "subtotal": 0}},
      "trim_doors_windows": {{"linear_feet_doors": 0, "gallons_needed": 0, "material_cost": 0, "labor_hours": 0, "labor_cost": 0, "subtotal": 0}},
      "exterior": {{"sqft": 0, "gallons_needed": 0, "material_cost": 0, "labor_hours": 0, "labor_cost": 0, "subtotal": 0}}
    }},
    "supplies": {{
      "brushes_rollers": 0,
      "drop_cloths": 0,
      "tape_masking": 0,
      "sundries": 0,
      "subtotal": 0
    }},
    "labor_summary": {{
      "total_hours": 0,
      "lead_painter_hours": 0,
      "helper_hours": 0,
      "ny_lead_rate": 0,
      "ny_helper_rate": 0,
      "lead_painter_cost": 0,
      "helper_cost": 0,
      "subtotal": 0
    }},
    "project_costs": {{
      "materials_total": 0,
      "labor_total": 0,
      "subtotal": 0,
      "overhead_15_percent": 0,
      "profit_20_percent": 0,
      "total_project_cost": 0
    }}
  }},
  
  "timeline": {{"total_days": 0, "crew_size": 2, "schedule_notes": ""}},
  "clarifying_questions": ["5 questions about painting scope"],
  "assumptions": ["List assumptions"],
  "exclusions": ["What painting work is NOT included"],
  "non_painting_work_noted": ["List other work we are NOT bidding on"]
}}

If NO painting work is found:
{{
  "painting_work_found": false,
  "reason": "No painting work identified in this RFP",
  "non_painting_work_noted": ["List what the RFP is actually for"]
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return message.content[0].text

def format_currency(amount):
    """Format number as currency"""
    return f"${amount:,.2f}"

def print_painting_proposal(result, ny_rates):
    """Print detailed painting proposal with NY State rates"""
    
    if not result.get('painting_work_found'):
        print("\n" + "="*70)
        print("❌ NO PAINTING WORK FOUND")
        print("="*70)
        print(f"\nReason: {result.get('reason', 'Unknown')}")
        
        if result.get('non_painting_work_noted'):
            print(f"\n📋 This RFP is for:")
            for item in result['non_painting_work_noted']:
                print(f"   • {item}")
        
        print(f"\n⚠️  This RFP does not include any painting work.")
        print(f"⚠️  No painting estimate will be generated.")
        return
    
    print("\n" + "="*70)
    print("🎨 PAINTING WORK PROPOSAL")
    print("   (Using CURRENT NY STATE MARKET RATES)")
    print("="*70)
    
    # Show NY rates used
    print(f"\n💰 NY STATE RATES APPLIED:")
    print(f"   • Paint: ${ny_rates['paint_costs_per_gallon']['interior_premium']['typical']}/gal (premium)")
    print(f"   • Labor: ${ny_rates['labor_rates_ny']['lead_painter_per_hour']['typical']}/hr (lead)")
    print(f"   • Helper: ${ny_rates['labor_rates_ny']['helper_per_hour']['typical']}/hr")
    
    # Scope
    print(f"\n📋 PAINTING SCOPE:")
    print(f"   {result.get('painting_scope_description', 'N/A')}")
    
    # Surfaces
    surfaces = result.get('surfaces_to_paint', {})
    print(f"\n📐 SURFACES TO PAINT:")
    
    if surfaces.get('interior_walls', {}).get('sqft', 0) > 0:
        wall_data = surfaces['interior_walls']
        print(f"   • Interior Walls: {wall_data.get('sqft', 0):,} sqft")
        if wall_data.get('location'):
            print(f"     Location: {wall_data['location']}")
        print(f"     Condition: {wall_data.get('condition', 'standard')}")
        print(f"     Coats: {wall_data.get('coats', 2)}")
    
    if surfaces.get('ceilings', {}).get('sqft', 0) > 0:
        ceiling_data = surfaces['ceilings']
        print(f"   • Ceilings: {ceiling_data.get('sqft', 0):,} sqft")
        print(f"     Coats: {ceiling_data.get('coats', 2)}")
    
    if surfaces.get('trim_baseboards', {}).get('linear_feet', 0) > 0:
        print(f"   • Trim/Baseboards: {surfaces['trim_baseboards']['linear_feet']:,} linear feet")
        print(f"     Coats: {surfaces['trim_baseboards'].get('coats', 2)}")
    
    if surfaces.get('doors', {}).get('count', 0) > 0:
        door_data = surfaces['doors']
        print(f"   • Doors: {door_data['count']} doors ({door_data.get('type', 'interior')})")
        print(f"     Sides: {door_data.get('sides', 2)}")
    
    if surfaces.get('windows', {}).get('count', 0) > 0:
        window_data = surfaces['windows']
        print(f"   • Windows: {window_data['count']} windows")
        print(f"     Includes: {window_data.get('includes', 'trim')}")
    
    if surfaces.get('exterior_walls', {}).get('sqft', 0) > 0:
        ext_data = surfaces['exterior_walls']
        print(f"   • Exterior Walls: {ext_data['sqft']:,} sqft")
        print(f"     Material: {ext_data.get('material', 'siding')}")
        print(f"     Coats: {ext_data.get('coats', 2)}")
    
    if surfaces.get('other_surfaces'):
        print(f"   • Other: {surfaces['other_surfaces']}")
    
    # Prep requirements
    prep_reqs = result.get('prep_requirements', [])
    if prep_reqs:
        print(f"\n🔨 PREP WORK REQUIRED:")
        for prep in prep_reqs:
            print(f"   • {prep}")
    
    # Paint specs
    paint_specs = result.get('paint_specifications', {})
    print(f"\n🎨 PAINT SPECIFICATIONS:")
    print(f"   • Type: {paint_specs.get('type', 'TBD')}")
    print(f"   • Sheen: {paint_specs.get('sheen', 'TBD')}")
    if paint_specs.get('brand_specified'):
        print(f"   • Brand: {paint_specs['brand_specified']}")
    print(f"   • Colors: {paint_specs.get('colors', 'TBD')}")
    
    # DETAILED COST BREAKDOWN
    costs = result.get('detailed_cost_breakdown', {})
    project_costs = costs.get('project_costs', {})
    
    print(f"\n" + "="*70)
    print("💰 DETAILED PAINTING COST BREAKDOWN")
    print("   (NY STATE RATES - PAINTING WORK ONLY)")
    print("="*70)
    
    # Prep work
    prep = costs.get('prep_work', {})
    if prep.get('subtotal', 0) > 0:
        print(f"\n1️⃣  PREP WORK: {format_currency(prep['subtotal'])}")
        for key, item in prep.items():
            if key != 'subtotal' and isinstance(item, dict):
                if item.get('cost', 0) > 0:
                    desc = item.get('description', key.replace('_', ' ').title())
                    cost = item.get('cost', 0)
                    if item.get('hours', 0) > 0:
                        print(f"      • {desc}: {item['hours']} hrs = {format_currency(cost)}")
                    else:
                        print(f"      • {desc}: {format_currency(cost)}")
    
    # Primer
    primer = costs.get('primer', {})
    if primer.get('subtotal', 0) > 0:
        print(f"\n2️⃣  PRIMER: {format_currency(primer['subtotal'])}")
        gal = primer.get('gallons_needed', 0)
        cost_per = primer.get('ny_cost_per_gallon', 0)
        mat_cost = primer.get('material_cost', 0)
        labor_hrs = primer.get('labor_hours', 0)
        labor_cost = primer.get('labor_cost', 0)
        print(f"      • Material: {gal} gal @ {format_currency(cost_per)}/gal = {format_currency(mat_cost)}")
        print(f"      • Labor: {labor_hrs} hrs = {format_currency(labor_cost)}")
    
    # Paint by surface
    paint_surfaces = costs.get('paint_by_surface', {})
    if any(s.get('subtotal', 0) > 0 for s in paint_surfaces.values()):
        print(f"\n3️⃣  PAINT BY SURFACE:")
        
        for surface_name, surface_data in paint_surfaces.items():
            if surface_data.get('subtotal', 0) > 0:
                print(f"\n      {surface_name.replace('_', ' ').title()}: {format_currency(surface_data['subtotal'])}")
                if surface_data.get('sqft', 0) > 0:
                    print(f"         - Square footage: {surface_data['sqft']:,} sqft")
                if surface_data.get('linear_feet_doors', 0) > 0:
                    print(f"         - Linear feet/doors: {surface_data['linear_feet_doors']:,}")
                if surface_data.get('gallons_needed', 0) > 0:
                    gal = surface_data['gallons_needed']
                    cost_per = surface_data.get('ny_cost_per_gallon', 0)
                    mat = surface_data.get('material_cost', 0)
                    print(f"         - Paint: {gal} gal @ {format_currency(cost_per)}/gal = {format_currency(mat)}")
                if surface_data.get('labor_hours', 0) > 0:
                    hrs = surface_data['labor_hours']
                    rate = surface_data.get('ny_labor_rate', 0)
                    labor = surface_data.get('labor_cost', 0)
                    print(f"         - Labor: {hrs} hrs @ {format_currency(rate)}/hr = {format_currency(labor)}")
    
    # Supplies
    supplies = costs.get('supplies', {})
    if supplies.get('subtotal', 0) > 0:
        print(f"\n4️⃣  SUPPLIES & MATERIALS: {format_currency(supplies['subtotal'])}")
        if supplies.get('brushes_rollers', 0) > 0:
            print(f"      • Brushes/Rollers: {format_currency(supplies['brushes_rollers'])}")
        if supplies.get('drop_cloths', 0) > 0:
            print(f"      • Drop Cloths: {format_currency(supplies['drop_cloths'])}")
        if supplies.get('tape_masking', 0) > 0:
            print(f"      • Tape/Masking: {format_currency(supplies['tape_masking'])}")
        if supplies.get('sundries', 0) > 0:
            print(f"      • Sundries: {format_currency(supplies['sundries'])}")
    
    # Labor summary
    labor = costs.get('labor_summary', {})
    if labor.get('subtotal', 0) > 0:
        print(f"\n5️⃣  LABOR SUMMARY: {format_currency(labor['subtotal'])}")
        print(f"      • Total hours: {labor.get('total_hours', 0)} hrs")
        if labor.get('lead_painter_hours', 0) > 0:
            hrs = labor['lead_painter_hours']
            rate = labor.get('ny_lead_rate', 0)
            cost = labor.get('lead_painter_cost', 0)
            print(f"      • Lead painter: {hrs} hrs @ {format_currency(rate)}/hr = {format_currency(cost)}")
        if labor.get('helper_hours', 0) > 0:
            hrs = labor['helper_hours']
            rate = labor.get('ny_helper_rate', 0)
            cost = labor.get('helper_cost', 0)
            print(f"      • Helper: {hrs} hrs @ {format_currency(rate)}/hr = {format_currency(cost)}")
    
    # Project totals
    print(f"\n" + "="*70)
    print(f"PAINTING SUBTOTAL: {format_currency(project_costs.get('subtotal', 0))}")
    print(f"   • Materials: {format_currency(project_costs.get('materials_total', 0))}")
    print(f"   • Labor: {format_currency(project_costs.get('labor_total', 0))}")
    print(f"\nOverhead (15%): {format_currency(project_costs.get('overhead_15_percent', 0))}")
    print(f"Profit (20%): {format_currency(project_costs.get('profit_20_percent', 0))}")
    print(f"\n{'='*70}")
    print(f"TOTAL PAINTING COST: {format_currency(project_costs.get('total_project_cost', 0))}")
    print(f"   (Based on current NY State market rates)")
    print(f"{'='*70}")
    
    # Timeline
    timeline = result.get('timeline', {})
    print(f"\n⏱️  TIMELINE:")
    print(f"   • Duration: {timeline.get('total_days', 0)} working days")
    print(f"   • Crew size: {timeline.get('crew_size', 2)} painters")
    if timeline.get('schedule_notes'):
        print(f"   • Notes: {timeline['schedule_notes']}")
    
    # Clarifying questions
    questions = result.get('clarifying_questions', [])
    if questions:
        print(f"\n❓ CLARIFYING QUESTIONS (Painting-Specific):")
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
        print(f"\n🚫 EXCLUSIONS (NOT INCLUDED):")
        for exclusion in exclusions:
            print(f"   • {exclusion}")
    
    # Non-painting work
    non_painting = result.get('non_painting_work_noted', [])
    if non_painting:
        print(f"\n📋 OTHER WORK IN RFP (NOT IN THIS ESTIMATE):")
        print(f"   This RFP includes other construction work we are NOT bidding on:")
        for item in non_painting[:10]:  # Show first 10
            print(f"   • {item}")
        if len(non_painting) > 10:
            print(f"   • ... and {len(non_painting)-10} additional items")

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 analyze_painting_rfp.py --rfp_file FILE --contact_name NAME --contact_email EMAIL")
        print("\nExample:")
        print('  python3 analyze_painting_rfp.py --rfp_file "rfp.pdf" --contact_name "John Smith" --contact_email "john@email.com"')
        sys.exit(1)
    
    # Parse arguments
    args = {}
    for i in range(1, len(sys.argv), 2):
        if sys.argv[i].startswith('--'):
            args[sys.argv[i][2:]] = sys.argv[i+1]
    
    rfp_file = args.get('rfp_file')
    contact_name = args.get('contact_name')
    contact_email = args.get('contact_email')
    
    print("🎨 NIGHTSHIFT AI - PAINTING WORK EXTRACTOR")
    print("   with NY STATE MARKET RATE RESEARCH")
    print("="*70)
    print("Extracts ONLY painting work from any RFP")
    print("Ignores: landscaping, plumbing, electrical, site work, etc.")
    print("="*70)
    
    # Initialize Claude client
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    # Step 1: Research NY State rates
    ny_rates = research_ny_state_rates(client)
    
    # Step 2: Extract text from RFP
    print("\n📄 Reading RFP...")
    rfp_text = extract_pdf_text(rfp_file)
    if not rfp_text:
        sys.exit(1)
    
    print(f"✅ Extracted {len(rfp_text)} characters")
    
    # Step 3: Analyze for painting work only
    print("\n🔍 Extracting painting work using NY State rates...")
    
    try:
        result_text = analyze_painting_only(client, rfp_text, ny_rates)
        
        # Parse JSON response
        import re
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
        else:
            print(f"\n⚠️  Could not parse response")
            print("Raw response:")
            print(result_text[:500])
            sys.exit(1)
        
        # Print the proposal
        print_painting_proposal(result, ny_rates)
        
        # Save results to JSON file
        if result.get('painting_work_found'):
            os.makedirs("output", exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"output/painting_ny_rates_{timestamp}.json"
            
            output_data = {
                "contact": {
                    "name": contact_name,
                    "email": contact_email
                },
                "rfp_file": rfp_file,
                "generated": datetime.now().isoformat(),
                "ny_state_rates": ny_rates,
                "analysis": result
            }
            
            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            print(f"\n📁 Complete results saved to: {output_file}")
            print(f"\n✅ Ready to generate Rider Painting format estimate!")
        
    except Exception as e:
        print(f"\n❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
