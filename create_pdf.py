#!/usr/bin/env python3
"""Convert text proposal to formatted PDF"""
import sys
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from datetime import datetime

def text_to_pdf(text_file, pdf_file):
    # Read text file
    with open(text_file, 'r') as f:
        content = f.read()
    
    # Create PDF
    doc = SimpleDocTemplate(pdf_file, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor='#1e3a8a',
        spaceAfter=30,
    )
    story.append(Paragraph("NIGHTSHIFT AI PROPOSAL", title_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Content
    for line in content.split('\n'):
        if line.strip():
            story.append(Paragraph(line, styles['Normal']))
            story.append(Spacer(1, 0.1*inch))
    
    # Build PDF
    doc.build(story)
    print(f"✅ Created: {pdf_file}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Find latest text file
        import glob
        files = glob.glob("output/proposal_*.txt")
        if files:
            latest = max(files, key=lambda x: x)
            text_file = latest
        else:
            print("No proposal files found")
            sys.exit(1)
    else:
        text_file = sys.argv[1]
    
    pdf_file = text_file.replace('.txt', '.pdf')
    text_to_pdf(text_file, pdf_file)
    print(f"Opening {pdf_file}...")
    import os
    os.system(f'open "{pdf_file}"')
