#!/usr/bin/env python3
"""
Nightshift AI - Rider Painting Custom Proposal Generator
Matches the exact format of Rider Painting, Inc. estimates
"""

def generate_rider_painting_proposal(analysis_data, contact_info):
    """
    Generate HTML proposal matching Rider Painting format
    
    Args:
        analysis_data: Dictionary with painting analysis
        contact_info: Dictionary with client contact information
    """
    
    # Extract data
    client_name = contact_info.get('name', '')
    client_address = contact_info.get('address', '')
    client_phone = contact_info.get('phone', '')
    project_address = contact_info.get('project_address', '')
    
    scope = analysis_data.get('scope_description', {})
    surfaces = scope.get('surfaces', {})
    costs = analysis_data.get('detailed_cost_breakdown', {})
    project_costs = costs.get('project_costs', {})
    
    from datetime import datetime
    import random
    
    estimate_number = random.randint(3000, 9999)
    today = datetime.now().strftime('%m/%d/%Y')
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Estimate #{estimate_number} - Rider Painting</title>
    <style>
        @page {{
            size: letter;
            margin: 0.5in;
        }}
        
        body {{
            font-family: Arial, sans-serif;
            font-size: 11pt;
            line-height: 1.4;
            color: #000;
            margin: 0;
            padding: 20px;
        }}
        
        .header {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 30px;
            padding-bottom: 15px;
            border-bottom: 2px solid #000;
        }}
        
        .title {{
            text-align: center;
            font-size: 24pt;
            color: #666;
            margin-bottom: 20px;
            font-weight: normal;
            letter-spacing: 2px;
        }}
        
        .company-info {{
            width: 45%;
        }}
        
        .company-info h2 {{
            margin: 0 0 10px 0;
            font-size: 14pt;
            font-weight: bold;
        }}
        
        .company-info p {{
            margin: 3px 0;
            font-size: 10pt;
        }}
        
        .client-info {{
            width: 45%;
            text-align: right;
        }}
        
        .client-info h3 {{
            margin: 0 0 10px 0;
            font-size: 12pt;
            font-weight: bold;
        }}
        
        .client-info p {{
            margin: 3px 0;
            font-size: 10pt;
        }}
        
        .estimate-details {{
            text-align: right;
            margin-top: 15px;
        }}
        
        .estimate-details p {{
            margin: 3px 0;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        
        table th {{
            background: #f0f0f0;
            padding: 10px;
            text-align: left;
            border-bottom: 2px solid #000;
            font-size: 11pt;
        }}
        
        table th.amount {{
            text-align: right;
        }}
        
        table td {{
            padding: 8px 10px;
            border-bottom: 1px solid #ddd;
            vertical-align: top;
        }}
        
        table td.amount {{
            text-align: right;
            font-weight: bold;
        }}
        
        .scope-section {{
            margin: 15px 0 25px 20px;
            page-break-inside: avoid;
        }}
        
        .scope-title {{
            font-weight: bold;
            margin-bottom: 5px;
        }}
        
        .scope-address {{
            font-size: 10pt;
            margin-bottom: 10px;
        }}
        
        .scope-header {{
            font-weight: bold;
            margin: 10px 0 5px 0;
        }}
        
        .scope-section ul {{
            margin: 5px 0;
            padding-left: 15px;
            list-style-type: none;
        }}
        
        .scope-section li {{
            margin: 2px 0;
            padding-left: 10px;
            position: relative;
        }}
        
        .scope-section li:before {{
            content: "•";
            position: absolute;
            left: 0;
        }}
        
        .subtotal-row td {{
            border-top: 2px solid #000;
            font-weight: bold;
            padding-top: 10px;
        }}
        
        .total-row td {{
            border-top: 3px double #000;
            font-size: 12pt;
            font-weight: bold;
            padding-top: 10px;
        }}
        
        .notes-section {{
            margin-top: 30px;
            page-break-inside: avoid;
        }}
        
        .notes-section h3 {{
            font-size: 12pt;
            margin-bottom: 10px;
            border-bottom: 1px solid #000;
            padding-bottom: 5px;
        }}
        
        .notes-section ul {{
            list-style-type: none;
            padding-left: 15px;
        }}
        
        .notes-section li {{
            margin: 8px 0;
            padding-left: 10px;
            position: relative;
        }}
        
        .notes-section li:before {{
            content: "•";
            position: absolute;
            left: 0;
        }}
        
        .page-number {{
            text-align: center;
            margin-top: 30px;
            font-size: 9pt;
            color: #666;
        }}
        
        @media print {{
            body {{
                padding: 0;
            }}
        }}
    </style>
</head>
<body>
    <div class="title">ESTIMATE</div>
    
    <div class="header">
        <div class="company-info">
            <h2>Rider Painting, Inc.</h2>
            <p>388 Upper North Road</p>
            <p>Highland, New York 12528</p>
            <p>Phone: (845) 728-4770</p>
            <p>Email: info@RiderPaintingNY.com</p>
            <p>Web: RiderPaintingNY.com</p>
        </div>
        
        <div class="client-info">
            <h3>Prepared For</h3>
            <p><strong>{client_name}</strong></p>
            <p>{client_address}</p>
            <p>{client_phone}</p>
            
            <div class="estimate-details">
                <p><strong>Estimate #</strong> {estimate_number}</p>
                <p><strong>Date</strong> {today}</p>
                <p><strong>Business / Tax #</strong> 83-2287389</p>
            </div>
        </div>
    </div>
    
    <table>
        <thead>
            <tr>
                <th>Description</th>
                <th class="amount">Total</th>
            </tr>
        </thead>
        <tbody>
"""
    
    # Add Interior Painting line item
    interior_total = project_costs.get('total_project_cost', 0)
    
    html += f"""
            <tr>
                <td>
                    <strong>{project_address} - Interior Painting</strong>
                    
                    <div class="scope-section">
                        <div class="scope-address">{project_address}</div>
                        <div class="scope-header">Scope of Work: Interior Painting</div>
"""
    
    # Add Gyp. Wall Board section if applicable
    if surfaces.get('interior_walls', {}).get('sqft', 0) > 0:
        wall_data = surfaces['interior_walls']
        coats = wall_data.get('coats', 2)
        html += f"""
                        <div class="scope-header">Gyp. Wall Board: Walls, Ceilings, Soffits</div>
                        <ul>
                            <li>Prime</li>
                            <li>Finish: {coats} coat{'s' if coats > 1 else ''} applied</li>
                            <li>Light sand between coats</li>
                        </ul>
"""
    
    # Add Trim section if applicable
    if surfaces.get('trim_baseboards', {}).get('linear_feet', 0) > 0:
        html += """
                        <div class="scope-header">Trim: Base, Window Casing, Door Frames</div>
                        <ul>
                            <li>Price is based on trims being factory primed</li>
                            <li>Gaps caulked</li>
                            <li>Holes and seams filled</li>
                            <li>Finish: two (2) coats applied</li>
                            <li>Light sand prior to each coat</li>
                        </ul>
"""
    
    # Add Doors section if applicable
    if surfaces.get('doors', {}).get('count', 0) > 0:
        html += """
                        <div class="scope-header">Doors: HM/WD Panels and HM/WD Frames</div>
                        <ul>
                            <li>Price based on items being factory primed</li>
                            <li>Gaps caulked; Holes and seams filled</li>
                            <li>Light sand before each coat</li>
                            <li>Finish: two (2) coats applied</li>
                        </ul>
"""
    
    # Add Windows section
    html += """
                        <div class="scope-header">Windows: Trim, Sills, Aprons, Sashes - Interior Side</div>
                        <ul>
                            <li>Price based on items being factory primed or previously painted</li>
                            <li>Gaps caulked</li>
                            <li>Holes and seams filled</li>
                            <li>Finish: two (2) coats applied</li>
                            <li>Light sand prior to each coat</li>
                        </ul>
                    </div>
                </td>
                <td class="amount">${interior_total:,.2f}</td>
            </tr>
"""
    
    # Add Exterior if applicable
    exterior_sqft = surfaces.get('exterior_walls', {}).get('sqft', 0)
    if exterior_sqft > 0:
        exterior_costs = costs.get('paint_by_surface', {}).get('exterior', {}).get('subtotal', 0)
        html += f"""
            <tr>
                <td>
                    <strong>{project_address} - Exterior Painting</strong>
                    
                    <div class="scope-section">
                        <div class="scope-address">{project_address}</div>
                        <div class="scope-header">Scope of Work: Exterior Painting</div>
                        
                        <div class="scope-header">Exterior Surfaces:</div>
                        <ul>
                            <li>Gaps caulked; Holes and seams filled</li>
                            <li>Light sand before each coat</li>
                            <li>Prime</li>
                            <li>Finish: two (2) coats applied</li>
                        </ul>
                    </div>
                </td>
                <td class="amount">${exterior_costs:,.2f}</td>
            </tr>
"""
    
    # Subtotal and Total
    html += f"""
            <tr class="subtotal-row">
                <td>Subtotal</td>
                <td class="amount">${interior_total:,.2f}</td>
            </tr>
            <tr class="total-row">
                <td>Total</td>
                <td class="amount">${interior_total:,.2f}</td>
            </tr>
        </tbody>
    </table>
    
    <div class="notes-section">
        <h3>Notes:</h3>
        <ul>
            <li>Above pricing includes all labor and materials for fire caulking and sealant, where required on above scope(s)</li>
        </ul>
    </div>
    
    <div class="notes-section" style="margin-top: 40px;">
        <h3>Important Notes & Exclusions</h3>
        <ul>
            <li>Rider Painting, Inc. is a New York State MBE-certified contractor (File #72204).</li>
            <li>A late fee of 1.5% will be applied to any unpaid balance remaining 30 days after the invoice date.</li>
            <li>Pricing is based on the use of Sherwin-Williams products, pending approved submittals. These products meet or exceed the performance of the specified materials at a lower cost.</li>
            <li>All labor and materials necessary to complete the scope of work described above are included.</li>
            <li>Prime coat and first finish coat will be applied via spray application prior to the installation of ACT and wall angle. If application is required after installation, pricing is subject to change.</li>
            <li>Any alterations or deviations from the above scope that incur additional costs will only be executed upon written approval of a revised estimate or signed change order.</li>
        </ul>
    </div>
    
    <div class="page-number">Page 1 of 1</div>
</body>
</html>"""
    
    return html


if __name__ == "__main__":
    # Test with sample data
    test_analysis = {
        "scope_description": {
            "overview": "Interior painting of residential property",
            "surfaces": {
                "interior_walls": {"sqft": 2000, "coats": 2},
                "trim_baseboards": {"linear_feet": 300},
                "doors": {"count": 8}
            }
        },
        "detailed_cost_breakdown": {
            "project_costs": {
                "total_project_cost": 8500.00
            }
        }
    }
    
    test_contact = {
        "name": "Test Client",
        "address": "123 Main Street\nBeacon, NY 12508",
        "phone": "(845) 555-1234",
        "project_address": "123 Main Street, Beacon, NY 12508"
    }
    
    html = generate_rider_painting_proposal(test_analysis, test_contact)
    
    with open("test_rider_proposal.html", "w") as f:
        f.write(html)
    
    print("✅ Test proposal generated: test_rider_proposal.html")
