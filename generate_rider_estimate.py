#!/usr/bin/env python3
"""
Complete Rider Painting Estimate Generator
Analyzes RFP and generates estimate in Rider Painting format
"""

import sys
from analyze_painting_rfp import main as analyze_main, extract_pdf_text, analyze_painting_rfp
from rider_proposal_generator import generate_rider_painting_proposal
from config import CLAUDE_API_KEY
import anthropic
import json
from datetime import datetime

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 generate_rider_estimate.py --rfp_file FILE --contact_name NAME --contact_email EMAIL")
        sys.exit(1)
    
    # Parse arguments
    args = {}
    for i in range(1, len(sys.argv), 2):
        if sys.argv[i].startswith('--'):
            args[sys.argv[i][2:]] = sys.argv[i+1]
    
    rfp_file = args.get('rfp_file')
    contact_name = args.get('contact_name')
    contact_email = args.get('contact_email')
    project_address = args.get('project_address', 'Project Address TBD')
    client_address = args.get('client_address', '')
    client_phone = args.get('client_phone', '')
    
    print("🎨 RIDER PAINTING - ESTIMATE GENERATOR")
    print("="*70)
    
    # Step 1: Extract RFP text
    print("\n📄 Reading RFP...")
    rfp_text = extract_pdf_text(rfp_file)
    if not rfp_text:
        sys.exit(1)
    
    # Step 2: Analyze with Claude
    print("🤖 Analyzing painting scope...")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    result_text = analyze_painting_rfp(client, rfp_text)
    
    import re
    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if json_match:
        analysis = json.loads(json_match.group())
    else:
        print("❌ Could not parse analysis")
        sys.exit(1)
    
    # Check if painting project
    if not analysis.get('is_painting_project'):
        print("\n⚠️  NOT A PAINTING PROJECT")
        print(f"Reason: {analysis.get('reason')}")
        sys.exit(0)
    
    print("✅ Painting project confirmed!")
    
    # Step 3: Generate Rider Painting format proposal
    print("\n📝 Generating Rider Painting estimate...")
    
    contact_info = {
        "name": contact_name,
        "address": client_address,
        "phone": client_phone,
        "project_address": project_address
    }
    
    html = generate_rider_painting_proposal(analysis, contact_info)
    
    # Save HTML
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    html_file = f"output/rider_estimate_{timestamp}.html"
    
    with open(html_file, 'w') as f:
        f.write(html)
    
    print(f"✅ Estimate generated: {html_file}")
    print("\n📄 To convert to PDF:")
    print(f"   1. Open {html_file} in Safari")
    print("   2. Press Cmd+P")
    print("   3. Save as PDF")

if __name__ == "__main__":
    main()
