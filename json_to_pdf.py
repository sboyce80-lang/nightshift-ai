#!/usr/bin/env python3
"""Convert construction analysis JSON to formatted PDF"""
import json
import re
import sys
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    KeepTogether, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

# Knight Shift brand colors — blueprint blue + emerald green on dark
DARK_BLUE = HexColor('#0a2540')      # Deep navy (near-black from logo background)
MEDIUM_BLUE = HexColor('#1a6fb5')    # Blueprint blue (spartan helmet)
ACCENT_GREEN = HexColor('#2ecc71')   # Emerald green (helmet plume / tagline)
LIGHT_GRAY = HexColor('#f0f4f8')     # Cool gray tint
BORDER_GRAY = HexColor('#c0cad8')    # Blue-gray border
WHITE = HexColor('#ffffff')
WARN_BG = HexColor('#fff8e1')
AMBER_DARK = HexColor('#f59e0b')


def build_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        'DocTitle', parent=styles['Title'], fontSize=22,
        textColor=DARK_BLUE, spaceAfter=4, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        'DocSubtitle', parent=styles['Normal'], fontSize=11,
        textColor=MEDIUM_BLUE, spaceAfter=20, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        'SectionHead', parent=styles['Heading2'], fontSize=14,
        textColor=DARK_BLUE, spaceBefore=16, spaceAfter=8,
        borderPadding=(0, 0, 4, 0),
    ))
    styles.add(ParagraphStyle(
        'SubHead', parent=styles['Heading3'], fontSize=11,
        textColor=MEDIUM_BLUE, spaceBefore=10, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        'BodyText2', parent=styles['Normal'], fontSize=10,
        leading=14, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        'BulletItem', parent=styles['Normal'], fontSize=10,
        leading=14, leftIndent=20, bulletIndent=8, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        'SmallBullet', parent=styles['Normal'], fontSize=8,
        leading=11, leftIndent=20, bulletIndent=8, spaceAfter=1,
        textColor=HexColor('#555555'),
    ))
    styles.add(ParagraphStyle(
        'TableCell', parent=styles['Normal'], fontSize=9, leading=12,
    ))
    styles.add(ParagraphStyle(
        'TableHeader', parent=styles['Normal'], fontSize=9,
        leading=12, textColor=WHITE,
    ))
    styles.add(ParagraphStyle(
        'TableCellRight', parent=styles['Normal'], fontSize=9,
        leading=12, alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        'Note', parent=styles['Normal'], fontSize=8,
        leading=11, textColor=HexColor('#666666'), spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        'RFIClosing', parent=styles['Normal'], fontSize=10,
        leading=14, spaceBefore=8, spaceAfter=4,
        textColor=MEDIUM_BLUE, alignment=TA_CENTER,
    ))
    return styles


def fmt_currency(val):
    return f"${val:,.2f}"


def _safe_num(val):
    """Return a number from val, or 0 for None/strings like 'Various'."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        cleaned = val.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0
    return 0


def _extract_multiplier_from_notes_pdf(room):
    """Extract unit multiplier from room data (for PDF report)."""
    import re
    mult = room.get("unit_multiplier")
    if isinstance(mult, (int, float)) and mult > 1:
        return int(mult)
    notes = str(room.get("notes", ""))
    patterns = [
        r'multipli\w+\s+by\s+(\d+)\s+units?',
        r'[x\u00d7]\s*(\d+)\s+units?',
        r'(\d+)\s+(?:identical\s+)?units?\s+total',
        r'repeated\s+(?:across\s+)?(\d+)\s+units?',
    ]
    for pattern in patterns:
        match = re.search(pattern, notes, re.IGNORECASE)
        if match:
            val = int(match.group(1))
            if 1 < val <= 500:
                return val
    return 1


def _kv_table(rows, styles_list=None):
    """Build a simple 2-column key-value table with standard styling."""
    t = Table(rows, colWidths=[2.2 * inch, 2.2 * inch])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
    ]))
    return t


def _header_table(rows, col_widths):
    """Build a table with a dark-blue header row and alternating stripes."""
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
    ]))
    return t


def json_to_pdf(json_path, pdf_path):
    with open(json_path, 'r') as f:
        data = json.load(f)

    analysis = data.get('analysis', {})
    contact = data.get('contact', {})
    cost_est = data.get('cost_estimate', {})
    pricing = data.get('pricing_model', {})
    project = analysis.get('project_info', {})
    source_files = data.get('source_files') or project.get('source_files')

    doc = SimpleDocTemplate(
        pdf_path, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )
    styles = build_styles()
    story = []

    # ── Title ──
    story.append(Paragraph("KNIGHT SHIFT", styles['DocTitle']))
    story.append(Paragraph(
        '<font color="#2ecc71"><b>FORGED BY WILLPOWER</b></font>',
        ParagraphStyle('_tagline', parent=styles['Normal'], fontSize=9,
                       alignment=TA_CENTER, spaceAfter=12,
                       textColor=ACCENT_GREEN)
    ))
    subtitle_parts = []
    if project.get('project_name'):
        subtitle_parts.append(project['project_name'])
    if project.get('location'):
        subtitle_parts.append(project['location'])
    if subtitle_parts:
        story.append(Paragraph(" &mdash; ".join(subtitle_parts), styles['DocSubtitle']))
    story.append(HRFlowable(width="100%", thickness=2, color=MEDIUM_BLUE))
    story.append(Spacer(1, 12))

    # ── Contact & Document Info ──
    info_rows = []
    if contact.get('name'):
        info_rows.append(['Prepared For:', contact['name']])
    if contact.get('email'):
        info_rows.append(['Email:', contact['email']])
    if source_files:
        info_rows.append(['Source Files:', ", ".join(source_files)])
    elif data.get('document'):
        doc_label = data['document'].split('/')[-1] or data['document']
        info_rows.append(['Source Document:', doc_label])
    if data.get('generated'):
        info_rows.append(['Generated:', data['generated'][:10]])

    if info_rows:
        t = Table(info_rows, colWidths=[1.4 * inch, 5.0 * inch])
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), DARK_BLUE),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # ── Scope Notes (if provided) ──
    scope_notes = data.get('scope_notes', '')
    if scope_notes:
        scope_data = [[
            Paragraph(
                f'<b>SCOPE NOTES:</b>  {scope_notes}',
                ParagraphStyle('_scope', parent=styles['Normal'],
                               fontSize=10, textColor=DARK_BLUE)
            )
        ]]
        scope_tbl = Table(scope_data, colWidths=[6.5 * inch])
        scope_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), WARN_BG),
            ('BOX', (0, 0), (-1, -1), 1, AMBER_DARK),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ]))
        story.append(scope_tbl)
        story.append(Spacer(1, 12))

    # ── Project Summary (high-level overview from G-series / coversheet) ──
    overview = analysis.get('project_overview') or {}
    summary_text = (overview.get('scope_summary') or '').strip()
    scope_quote = (overview.get('scope_of_work') or '').strip()
    overview_notes = (overview.get('notes') or '').strip()
    stat_pairs = []
    if overview.get('total_gsf'):
        try:
            stat_pairs.append(('Total GSF', f"{int(overview['total_gsf']):,} sf"))
        except (TypeError, ValueError):
            pass
    if overview.get('building_count'):
        stat_pairs.append(('Buildings', str(overview['building_count'])))
    if overview.get('stories'):
        stat_pairs.append(('Stories', str(overview['stories'])))
    if overview.get('unit_count'):
        try:
            stat_pairs.append(('Units', f"{int(overview['unit_count']):,}"))
        except (TypeError, ValueError):
            pass
    if overview.get('occupancy_type'):
        stat_pairs.append(('Occupancy', str(overview['occupancy_type'])))
    if overview.get('construction_type'):
        stat_pairs.append(('Construction', str(overview['construction_type'])))

    if summary_text or stat_pairs or scope_quote or overview_notes:
        story.append(Paragraph("Project Summary", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))

        if summary_text:
            story.append(Paragraph(summary_text, styles['BodyText2']))
            story.append(Spacer(1, 6))

        if stat_pairs:
            # Lay out stats as a 3-column-wide bold-label / value strip,
            # filling row by row.
            cells = []
            for label, val in stat_pairs:
                cells.append(Paragraph(
                    f'<font color="#0a2540"><b>{label}:</b></font> {val}',
                    styles['BodyText2']
                ))
            # Pad to a multiple of 3 so the table renders evenly
            while len(cells) % 3 != 0:
                cells.append('')
            stat_rows = [cells[i:i + 3] for i in range(0, len(cells), 3)]
            stat_tbl = Table(
                stat_rows,
                colWidths=[2.17 * inch, 2.17 * inch, 2.16 * inch]
            )
            stat_tbl.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
                ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            story.append(stat_tbl)
            story.append(Spacer(1, 8))

        # Show the scope-of-work quote only if it adds something the
        # 1-3 sentence summary doesn't already cover.
        if scope_quote and scope_quote != summary_text and \
                scope_quote not in summary_text:
            story.append(Paragraph(
                f'<b>Scope of Work (from drawings):</b> {scope_quote}',
                styles['BodyText2']
            ))
            story.append(Spacer(1, 4))

        if overview_notes:
            story.append(Paragraph(
                f'<b>Notable:</b> {overview_notes}',
                styles['BodyText2']
            ))
            story.append(Spacer(1, 4))

        story.append(Spacer(1, 4))

    # ── Project Information ──
    proj_fields = [
        ('project_name', 'Project Name'),
        ('location', 'Location'),
        ('architect', 'Architect'),
        ('drawing_date', 'Drawing Date'),
        ('building_type', 'Building Type'),
        ('total_floors_analyzed', 'Floors Analyzed'),
        ('scale_notation', 'Scale'),
    ]
    proj_rows = []
    for key, label in proj_fields:
        val = project.get(key)
        if val is not None and val != '' and val != 0:
            proj_rows.append([label, str(val)])
    # Show template vs effective room counts when multiplication applied
    template_rooms = project.get('template_rooms')
    total_rooms = project.get('total_rooms_found')
    if template_rooms and total_rooms and template_rooms != total_rooms:
        proj_rows.append(['Template Rooms', str(template_rooms)])
        proj_rows.append(['Effective Rooms', str(total_rooms)])
    elif total_rooms:
        proj_rows.append(['Rooms Found', str(total_rooms)])
    if proj_rows:
        story.append(Paragraph("Project Information", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        t = Table(proj_rows, colWidths=[1.6 * inch, 4.8 * inch])
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(Spacer(1, 4))
        story.append(t)

    # ── Construction Details (if present from permit-only files) ──
    construction = analysis.get('construction_details_noted', {})
    if construction:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Construction Details", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        cd_rows = []
        for key, label in [('construction_type', 'Construction Type'), ('stories', 'Stories'),
                           ('height', 'Building Height'), ('sprinkler_system', 'Sprinkler System'),
                           ('fire_alarm_system', 'Fire Alarm System')]:
            val = construction.get(key)
            if val is not None:
                cd_rows.append([label, str(val)])
        if cd_rows:
            t = Table(cd_rows, colWidths=[1.6 * inch, 4.8 * inch])
            t.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
                ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(Spacer(1, 4))
            story.append(t)

    # ── Room Finish Schedule (architect's per-room finish table) ──
    rfs_rooms = analysis.get('room_finish_schedule') or []
    if rfs_rooms:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Room Finish Schedule", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"Extracted from architectural drawings — {len(rfs_rooms)} room types listed.",
            styles['Note']
        ))

        def _rfs_cell(v, width):
            s = '-' if v in (None, '') else str(v)
            return s if len(s) <= width else s[:width - 1] + '…'

        rfs_data = [['Room #', 'Room', 'Unit', 'Floor', 'Wall', 'Ceiling', 'Base', 'Floor Fin.']]
        for r in rfs_rooms:
            rfs_data.append([
                _rfs_cell(r.get('room_number'), 8),
                _rfs_cell(r.get('room_name'), 18),
                _rfs_cell(r.get('unit_type'), 11),
                _rfs_cell(r.get('floor_level'), 6),
                _rfs_cell(r.get('wall_finish'), 14),
                _rfs_cell(r.get('ceiling_finish'), 14),
                _rfs_cell(r.get('base_finish'), 12),
                _rfs_cell(r.get('floor_finish'), 14),
            ])

        rfs_cw = [0.55*inch, 1.25*inch, 0.7*inch, 0.5*inch,
                  0.95*inch, 1.05*inch, 0.85*inch, 0.85*inch]
        rfs_t = Table(rfs_data, colWidths=rfs_cw, repeatRows=1)
        rfs_t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('LEADING', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('ALIGN', (0, 1), (0, -1), 'CENTER'),
            ('ALIGN', (2, 1), (3, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
        ]))
        story.append(Spacer(1, 4))
        story.append(rfs_t)

    # ── Room-by-Room Breakdown ──
    floors = analysis.get('floors', [])
    if floors:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Room-by-Room Breakdown", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))

        for floor in floors:
            floor_name = floor.get('floor_name', 'Unknown Floor')
            rooms = floor.get('rooms', [])
            story.append(Spacer(1, 6))

            # Calculate effective room count for this floor
            effective = sum(
                _extract_multiplier_from_notes_pdf(r) for r in rooms
            )
            if effective != len(rooms):
                story.append(Paragraph(
                    f"{floor_name} ({len(rooms)} templates, {effective} effective rooms)",
                    styles['SubHead']
                ))
            else:
                story.append(Paragraph(
                    f"{floor_name} ({len(rooms)} rooms)", styles['SubHead']
                ))

            # ── Table 1: Surface Measurements (dimensions + areas + materials) ──
            surf_rows = [['Room', 'Dimensions', 'Wall Mat', 'Wall SF',
                          'Ceil Mat', 'Ceil SF', 'Trim LF', 'Mult', 'Sheet']]
            for room in rooms:
                dims = room.get('dimensions', {})
                elems = room.get('elements', {})
                mats = room.get('materials', {})
                name = room.get('room_name', room.get('room_id', '-'))
                if len(name) > 22:
                    name = name[:20] + '..'

                _l = _safe_num(dims.get('length_feet', 0))
                _w = _safe_num(dims.get('width_feet', 0))
                _h = _safe_num(dims.get('ceiling_height_feet', 0))
                if _l > 0 and _w > 0 and _h > 0:
                    dim_str = f"{_l:.0f}x{_w:.0f}x{_h:.0f}"
                elif _l > 0 and _w > 0:
                    dim_str = f"{_l:.0f}x{_w:.0f}"
                else:
                    dim_str = "-"

                wall_mat_str = str(mats.get('walls', '-'))[:6]
                ceil_mat_raw = str(mats.get('ceiling', '-'))
                ceil_ptd = mats.get('ceiling_painted', False)
                ceil_mat_str = ceil_mat_raw[:6]
                if not ceil_ptd and ceil_mat_str != '-':
                    ceil_mat_str = f"{ceil_mat_str}*"  # Asterisk = not painted

                mult = _extract_multiplier_from_notes_pdf(room)
                mult_str = f"x{mult}" if mult > 1 else "1"
                sheet = str(room.get('source_sheet', '-'))[:8]

                surf_rows.append([
                    name,
                    dim_str,
                    wall_mat_str,
                    f"{_safe_num(dims.get('wall_area_sqft')):,.0f}",
                    ceil_mat_str,
                    f"{_safe_num(dims.get('ceiling_area_sqft')):,.0f}",
                    f"{_safe_num(elems.get('base_trim_lf')):,.0f}",
                    mult_str,
                    sheet,
                ])

            surf_cw = [1.15*inch, 0.6*inch, 0.45*inch, 0.5*inch,
                        0.45*inch, 0.45*inch, 0.45*inch, 0.3*inch, 0.55*inch]
            t1 = Table(surf_rows, colWidths=surf_cw, repeatRows=1)
            t1.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('LEADING', (0, 0), (-1, -1), 9),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                ('ALIGN', (2, 0), (2, -1), 'CENTER'),
                ('ALIGN', (4, 0), (4, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ]))
            story.append(Spacer(1, 2))
            story.append(t1)

            # ── Table 2: Elements & Counts (doors, windows, specialty items) ──
            # Only show if any room on this floor has non-zero elements beyond trim
            _has_elements = False
            for room in rooms:
                elems = room.get('elements', {})
                dr_fp = _safe_num(elems.get('doors_full_paint', elems.get('doors', 0)))
                dr_hm = _safe_num(elems.get('doors_hm_panel', 0))
                dr_fr = _safe_num(elems.get('doors_frame_only', 0))
                win_p = _safe_num(elems.get('windows_painted_interior', elems.get('windows', 0)))
                stairs = _safe_num(elems.get('stair_sections', 0))
                wc = _safe_num(elems.get('wallcovering_sqft', 0))
                stwood = _safe_num(elems.get('stained_wood_sqft', 0))
                soffit = _safe_num(elems.get('soffit_sqft', 0))
                conc = _safe_num(elems.get('concrete_floor_sqft', 0))
                cols = _safe_num(elems.get('painted_columns_ea', 0))
                l5 = _safe_num(elems.get('level_5_finish_sqft', 0))
                if any(v > 0 for v in [dr_fp, dr_hm, dr_fr, win_p, stairs,
                                        wc, stwood, soffit, conc, cols, l5]):
                    _has_elements = True
                    break

            if _has_elements:
                elem_rows = [['Room', 'Dr FP', 'Dr HM', 'Dr Fr', 'Win P',
                              'Stairs', 'WC SF', 'Stained', 'Soffit', 'Conc', 'Cols', 'L5 SF']]
                for room in rooms:
                    elems = room.get('elements', {})
                    name = room.get('room_name', room.get('room_id', '-'))
                    if len(name) > 18:
                        name = name[:16] + '..'

                    dr_fp = _safe_num(elems.get('doors_full_paint', 0))
                    dr_hm = _safe_num(elems.get('doors_hm_panel', 0))
                    dr_fr = _safe_num(elems.get('doors_frame_only', 0))
                    if dr_fp == 0 and dr_hm == 0 and 'doors' in elems and 'doors_full_paint' not in elems:
                        dr_fp = _safe_num(elems.get('doors', 0))
                    win_p = _safe_num(elems.get('windows_painted_interior', 0))
                    if win_p == 0 and 'windows' in elems and 'windows_painted_interior' not in elems:
                        win_p = _safe_num(elems.get('windows', 0))

                    def _elem_str(val, is_sf=False):
                        if val == 0:
                            return '-'
                        return f"{val:,.0f}" if is_sf else str(int(val))

                    elem_rows.append([
                        name,
                        _elem_str(dr_fp),
                        _elem_str(dr_hm),
                        _elem_str(dr_fr),
                        _elem_str(win_p),
                        _elem_str(_safe_num(elems.get('stair_sections', 0))),
                        _elem_str(_safe_num(elems.get('wallcovering_sqft', 0)), True),
                        _elem_str(_safe_num(elems.get('stained_wood_sqft', 0)), True),
                        _elem_str(_safe_num(elems.get('soffit_sqft', 0)), True),
                        _elem_str(_safe_num(elems.get('concrete_floor_sqft', 0)), True),
                        _elem_str(_safe_num(elems.get('painted_columns_ea', 0))),
                        _elem_str(_safe_num(elems.get('level_5_finish_sqft', 0)), True),
                    ])

                elem_cw = [1.0*inch, 0.35*inch, 0.35*inch, 0.35*inch, 0.35*inch,
                           0.4*inch, 0.5*inch, 0.5*inch, 0.45*inch, 0.45*inch, 0.35*inch, 0.45*inch]
                t2 = Table(elem_rows, colWidths=elem_cw, repeatRows=1)
                t2.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), MEDIUM_BLUE),
                    ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 6.5),
                    ('LEADING', (0, 0), (-1, -1), 8),
                    ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                    ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                    ('TOPPADDING', (0, 0), (-1, -1), 2),
                    ('LEFTPADDING', (0, 0), (-1, -1), 2),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                    ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                    ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
                ]))
                story.append(Spacer(1, 2))
                story.append(t2)

            # Legend note for ceiling material asterisk
            story.append(Paragraph(
                '<i>* = ceiling not painted (ACT, exposed, etc.)</i>',
                styles['Note']
            ))

        # ── Unit Multiplication Summary ──
        unit_mult = analysis.get('unit_multiplication', {})
        if unit_mult.get('applied'):
            story.append(Spacer(1, 10))
            story.append(Paragraph("Unit Multiplication Applied", styles['SectionHead']))
            story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f"<i>{unit_mult['template_rooms']} template rooms expanded to "
                f"{unit_mult['effective_rooms']} effective rooms via unit multiplication.</i>",
                styles['Note']
            ))

            mult_rows = [['Unit Type', 'Room', 'Multiplier']]
            for detail in unit_mult.get('details', []):
                mult_rows.append([
                    detail.get('unit_type', '') or detail.get('floor', '-'),
                    detail.get('room_name', detail.get('room_id', '-')),
                    f"x{detail['unit_multiplier']}",
                ])
            mcw = [2.0*inch, 2.5*inch, 1.0*inch]
            mt = Table(mult_rows, colWidths=mcw, repeatRows=1)
            mt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), MEDIUM_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('LEADING', (0, 0), (-1, -1), 10),
                ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ]))
            story.append(Spacer(1, 2))
            story.append(mt)

    # ── Excluded from Scope ──
    scope_summary = analysis.get('scope_summary', {})
    excluded_rooms = scope_summary.get('excluded_rooms', [])
    if excluded_rooms:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Excluded from Scope", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        scope_notes_display = data.get('scope_notes', 'N/A')
        story.append(Paragraph(
            f'<i>Scope: "{scope_notes_display}"</i>', styles['Note']
        ))
        story.append(Paragraph(
            f'<i>{len(excluded_rooms)} room(s) excluded from estimate totals.</i>',
            styles['Note']
        ))
        story.append(Spacer(1, 6))

        excl_rows = [['Room ID', 'Room Name', 'Floor', 'Reason']]
        for excl in excluded_rooms:
            reason = excl.get('reason', '')
            if len(reason) > 60:
                reason = reason[:58] + '..'
            excl_rows.append([
                excl.get('room_id', ''),
                excl.get('room_name', ''),
                excl.get('floor', ''),
                reason,
            ])

        excl_col_widths = [1.2 * inch, 1.5 * inch, 1.2 * inch, 2.7 * inch]
        excl_tbl = Table(excl_rows, colWidths=excl_col_widths, repeatRows=1)
        excl_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#8B4513')),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [HexColor('#fff0e0'), HexColor('#ffe8d0')]),
        ]))
        story.append(excl_tbl)

    # ── Deduplication Report ──
    dedup_report = analysis.get('deduplication_report', [])
    if dedup_report:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Deduplication Report", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f'<i>{len(dedup_report)} duplicate room(s) resolved across source files.</i>',
            styles['Note']
        ))
        story.append(Spacer(1, 4))

        dedup_rows = [['Kept Room', 'Removed Room', 'Reason']]
        for entry in dedup_report[:20]:  # Cap at 20 rows
            reason = entry.get('reason', '')
            if len(reason) > 55:
                reason = reason[:53] + '..'
            dedup_rows.append([
                str(entry.get('kept', ''))[:20],
                str(entry.get('removed', ''))[:20],
                reason,
            ])
        if len(dedup_report) > 20:
            dedup_rows.append([f'...and {len(dedup_report) - 20} more', '', ''])

        dedup_cw = [1.5 * inch, 1.5 * inch, 3.6 * inch]
        dedup_tbl = Table(dedup_rows, colWidths=dedup_cw, repeatRows=1)
        dedup_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#6b21a8')),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('LEADING', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor('#f5f3ff')]),
        ]))
        story.append(dedup_tbl)

    # ── Aggregated Totals ──
    agg = analysis.get('aggregated_totals', {})
    if agg:
        story.append(Spacer(1, 10))
        agg_title = ("Aggregated Measurements (In-Scope Only)"
                     if scope_summary.get('rooms_excluded')
                     else "Aggregated Measurements")
        story.append(Paragraph(agg_title, styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))

        # New fields with backward compat fallbacks
        doors_fp = _safe_num(agg.get('total_doors_full_paint', agg.get('total_doors', 0)))
        doors_hm = _safe_num(agg.get('total_doors_hm_panel', 0))
        doors_frame = _safe_num(agg.get('total_doors_frame_only', 0))
        win_ptd = _safe_num(agg.get('total_windows_painted_interior', agg.get('total_windows', 0)))
        win_all = _safe_num(agg.get('total_windows_all', 0))
        stairs = _safe_num(agg.get('total_stair_sections', 0))

        agg_rows = [
            ['Paintable Walls', f"{_safe_num(agg.get('total_paintable_wall_sqft')):,.0f} sqft"],
            ['Paintable Ceilings', f"{_safe_num(agg.get('total_paintable_ceiling_sqft')):,.0f} sqft"],
            ['Base Trim', f"{_safe_num(agg.get('total_base_trim_lf')):,.0f} LF"],
            ['Doors — Full Paint', f"{int(doors_fp)}"],
            ['Doors — HM Panel', f"{int(doors_hm)}"],
            ['Doors — Frame Only', f"{int(doors_frame)}"],
            ['Doors — Total', f"{int(doors_fp + doors_hm + doors_frame)}"],
            ['Windows (Painted Interior)', f"{int(win_ptd)}"],
            ['Windows (All)', f"{int(win_all)}"],
            ['Stair Sections', f"{int(stairs)}"],
        ]
        story.append(Spacer(1, 4))
        story.append(_kv_table(agg_rows))
        story.append(Paragraph(
            '<i>Door types — Full Paint: panel + frame both field-painted. '
            'HM Panel: hollow-metal panel painted, frame factory-finished. '
            'Frame Only: frame field-painted, panel not painted.</i>',
            styles['Note']))

    # ── Traceability Summary ──
    # Shows which rooms contribute to each key metric for audit trail
    if floors:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Traceability Summary", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            '<i>Top contributors to each key metric — trace totals back to source sheets.</i>',
            styles['Note']
        ))

        # Collect all in-scope rooms with their contributions (applying unit multiplier)
        all_rooms_flat = []
        for floor in floors:
            for room in floor.get('rooms', []):
                if not room.get('in_scope', True):
                    continue
                dims = room.get('dimensions', {})
                elems = room.get('elements', {})
                mats = room.get('materials', {})
                is_paintable = str(mats.get('walls', '')).upper() in (
                    'GYP', 'GWB', '1HR GYP', 'GYPSUM', 'DRYWALL', '')
                multiplier = _extract_multiplier_from_notes_pdf(room)
                all_rooms_flat.append({
                    'name': room.get('room_name', room.get('room_id', '-')),
                    'sheet': str(room.get('source_sheet', '-'))[:8],
                    'src_file': str(room.get('source_file', '-'))[:20],
                    'floor': floor.get('floor_name', ''),
                    'multiplier': multiplier,
                    'walls': (_safe_num(dims.get('wall_area_sqft', 0)) if is_paintable else 0) * multiplier,
                    'ceilings': (_safe_num(dims.get('ceiling_area_sqft', 0)) if mats.get('ceiling_painted') else 0) * multiplier,
                    'trim': _safe_num(elems.get('base_trim_lf', 0)) * multiplier,
                    'doors_fp': _safe_num(elems.get('doors_full_paint', 0)) * multiplier,
                    'doors_hm': _safe_num(elems.get('doors_hm_panel', 0)) * multiplier,
                    'doors_frame': _safe_num(elems.get('doors_frame_only', 0)) * multiplier,
                    'windows': _safe_num(elems.get('windows_painted_interior', 0)) * multiplier,
                })

        trace_metrics = [
            ('Paintable Walls (sqft)', 'walls'),
            ('Paintable Ceilings (sqft)', 'ceilings'),
            ('Base Trim (LF)', 'trim'),
            ('Doors Full Paint', 'doors_fp'),
            ('Doors HM Panel', 'doors_hm'),
            ('Doors Frame Only', 'doors_frame'),
            ('Windows Painted', 'windows'),
        ]
        MAX_TRACE_ROWS = 10

        for metric_label, metric_key in trace_metrics:
            # Sort rooms by contribution to this metric (desc), skip zeros
            contributors = sorted(
                [(r, r[metric_key]) for r in all_rooms_flat if r[metric_key] > 0],
                key=lambda x: x[1], reverse=True
            )
            if not contributors:
                continue

            total_val = sum(c[1] for c in contributors)
            story.append(Spacer(1, 6))
            story.append(Paragraph(
                f"<b>{metric_label}</b> — Total: {total_val:,.0f} from {len(contributors)} rooms",
                styles['BodyText2']
            ))

            trace_rows = [['Room', 'Floor', 'Sheet', 'Source File', 'Value']]
            shown = contributors[:MAX_TRACE_ROWS]
            for r, val in shown:
                rname = r['name']
                if r.get('multiplier', 1) > 1:
                    rname = f"{rname} (x{r['multiplier']})"
                if len(rname) > 28:
                    rname = rname[:26] + '..'
                trace_rows.append([
                    rname,
                    r['floor'][:12],
                    r['sheet'],
                    r['src_file'],
                    f"{val:,.0f}",
                ])
            remaining = contributors[MAX_TRACE_ROWS:]
            if remaining:
                remaining_total = sum(c[1] for c in remaining)
                trace_rows.append([
                    f"...and {len(remaining)} more rooms",
                    '', '', '',
                    f"{remaining_total:,.0f}",
                ])

            tcw = [1.4*inch, 0.8*inch, 0.6*inch, 1.3*inch, 0.6*inch]
            tt = Table(trace_rows, colWidths=tcw, repeatRows=1)
            tt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), MEDIUM_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('LEADING', (0, 0), (-1, -1), 9),
                ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ]))
            story.append(tt)

            # ── Pipeline reconciliation (walls / trim) ──
            # The per-room sum above is the EXTRACTED total. The Cost Estimate
            # prices a figure derived from it through pipeline adjustments
            # (perimeter boost, sanity caps, senior-estimator review). Show
            # both, and name the adjustments that left a trace, so this table
            # does not look like it contradicts the priced number.
            _li_kw = {
                'walls': ('gyp. wall', 'gyp wall'),
                'trim': ('base trim',),
            }.get(metric_key)
            if _li_kw:
                extracted = total_val
                priced = None
                will_pct = None
                for li in (cost_est.get('line_items') or []):
                    _label = str(li.get('item', ''))
                    if any(kw in _label.lower() for kw in _li_kw):
                        priced = _safe_num(li.get('qty', 0))
                        _m = re.search(r'\[Will:\s*([+-]?\d+(?:\.\d+)?)\s*%\]', _label)
                        will_pct = float(_m.group(1)) if _m else None
                        break
                # Only explain a materially different priced number — small
                # gaps are routine door-opening deductions, not worth a note.
                if (priced is not None and extracted > 0
                        and abs(priced - extracted) > extracted * 0.04):
                    _notes_blob = " ".join(str(n) for n in (analysis.get('notes') or []))
                    _adj = []
                    if ('Perimeter Wall Boost' in _notes_blob
                            or '[Wall Boost]' in _notes_blob):
                        _adj.append('a perimeter boost')
                    if priced < extracted * 0.96:
                        # Priced dropped below the extracted total — an upstream
                        # sanity cap / manual-review reduction shrank it.
                        _adj.append('a sanity cap')
                    if will_pct:
                        _adj.append(f'a senior-estimator adjustment (Will {will_pct:+.0f}%)')
                    if len(_adj) > 1:
                        _adj_txt = ', '.join(_adj[:-1]) + ' and ' + _adj[-1]
                    elif _adj:
                        _adj_txt = _adj[0]
                    else:
                        _adj_txt = 'pipeline adjustments'
                    story.append(Paragraph(
                        f"<i>Reconciliation: per-room extraction sums to "
                        f"{extracted:,.0f}; the Cost Estimate prices {priced:,.0f} "
                        f"after {_adj_txt}. See the job notes for the adjustment "
                        f"trail.</i>",
                        styles['Note']
                    ))

    # ── Exterior Scope ──
    ext = analysis.get('exterior', {})
    if ext and (_safe_num(ext.get('cornice_lf', 0)) > 0
                or _safe_num(ext.get('window_trim_lf', 0)) > 0
                or _safe_num(ext.get('soffit_sqft', 0)) > 0
                or _safe_num(ext.get('railing_lf', 0)) > 0
                or ext.get('lift_required', False)):
        story.append(Spacer(1, 8))
        story.append(Paragraph("Exterior Scope", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        ext_rows = []
        if _safe_num(ext.get('cornice_lf', 0)) > 0:
            ext_rows.append(['Cornice / Brackets', f"{_safe_num(ext.get('cornice_lf')):,.0f} LF"])
        if _safe_num(ext.get('window_trim_lf', 0)) > 0:
            ext_rows.append(['Window Trim', f"{_safe_num(ext.get('window_trim_lf')):,.0f} LF"])
        if _safe_num(ext.get('soffit_sqft', 0)) > 0:
            ext_rows.append(['Soffits', f"{_safe_num(ext.get('soffit_sqft')):,.0f} sqft"])
        if _safe_num(ext.get('railing_lf', 0)) > 0:
            ext_rows.append(['Railings', f"{_safe_num(ext.get('railing_lf')):,.0f} LF"])
        ext_rows.append(['Lift Required', 'Yes' if ext.get('lift_required') else 'No'])
        if ext.get('notes'):
            ext_rows.append(['Notes', str(ext['notes'])[:80]])
        story.append(Spacer(1, 4))
        story.append(_kv_table(ext_rows))

    # ── Occupancy Data (permit-only files) ──
    occ = analysis.get('occupancy_data_from_code_analysis', {})
    if occ:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Occupancy Data (from Code Analysis)", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        occ_rows = [['Area', 'Details', 'Gross Area (sqft)']]
        basement = occ.get('basement_storage', {})
        if basement:
            occ_rows.append(['Basement', basement.get('occupancy', ''), f"{basement.get('gross_area_sqft', 0):,}"])
        first = occ.get('first_floor_commercial', {})
        if first:
            for key, val in first.items():
                label = key.replace('_', ' ').title()
                area = val.get('gross_area_sqft', 0) if isinstance(val, dict) else 0
                occ_rows.append(['1st Floor', label, f"{area:,}"])
        for floor_key, floor_label in [('second_floor_residential', '2nd Floor'), ('third_floor_residential', '3rd Floor')]:
            fl = occ.get(floor_key, {})
            if fl:
                occ_rows.append([floor_label, 'Residential', f"{fl.get('gross_area_sqft', 0):,}"])
        if len(occ_rows) > 1:
            story.append(Spacer(1, 4))
            story.append(_header_table(occ_rows, [1.2*inch, 2.8*inch, 1.6*inch]))

    # ── Corrections Applied ──
    corrections_applied = analysis.get('corrections_applied', [])
    if corrections_applied:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Corrections Applied", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))

        corr_data = [[Paragraph(
            f'<b>CORRECTIONS:</b>  {len(corrections_applied)} override(s) applied from corrections.json',
            styles['TableCell']
        )]]
        corr_tbl = Table(corr_data, colWidths=[6.5 * inch])
        corr_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), WARN_BG),
            ('BOX', (0, 0), (-1, -1), 1, AMBER_DARK),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ]))
        story.append(corr_tbl)
        story.append(Spacer(1, 4))

        for corr in corrections_applied[:15]:
            story.append(Paragraph(f"• {corr}", styles['SmallBullet']))
        if len(corrections_applied) > 15:
            story.append(Paragraph(
                f"<i>...and {len(corrections_applied) - 15} more correction(s)</i>",
                styles['SmallBullet']
            ))

    # ── Cost Estimate ──
    line_items = cost_est.get('line_items', [])
    if line_items:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Cost Estimate", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))

        est_rows = [['Item', 'Qty', 'Cost', 'Markup', 'Total']]
        for li in line_items:
            if _safe_num(li.get('qty', 0)) > 0:
                est_rows.append([
                    li.get('item', ''),
                    f"{_safe_num(li.get('qty')):,.0f}",
                    fmt_currency(li.get('cost', 0)),
                    fmt_currency(li.get('markup', 0)),
                    fmt_currency(li.get('total', 0)),
                ])
        est_rows.append([
            '', '', '', 'Subtotal:',
            fmt_currency(cost_est.get('subtotal', 0))
        ])

        t = Table(est_rows, colWidths=[2.4 * inch, 0.7 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (3, -1), (4, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -2), 0.25, BORDER_GRAY),
            ('LINEABOVE', (0, -1), (-1, -1), 1, DARK_BLUE),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [WHITE, LIGHT_GRAY]),
        ]))
        story.append(Spacer(1, 4))
        story.append(t)

    # ── Estimated Labor Hours (PCA Production Rates) ──
    labor_hours = data.get('labor_hours_estimate', {})
    lh_categories = labor_hours.get('categories', [])
    if lh_categories:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Estimated Labor Hours", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            '<i>Based on PCA (Painting Contractors Association) production rates. '
            'Assumes airless spray for walls/ceilings, brush for trim/windows. '
            'Actual hours may vary based on job conditions.</i>',
            styles['Note']
        ))

        lh_rows = [['Category', 'Surface (SF)', 'Rate (SF/HR)', 'Est. Hours']]
        for cat in lh_categories:
            if cat.get('hours', 0) > 0:
                lh_rows.append([
                    cat.get('category', ''),
                    f"{cat.get('surface_sf', 0):,.0f}",
                    f"{cat.get('rate_sf_hr', 0):,.0f}",
                    f"{cat.get('hours', 0):,.1f}",
                ])

        prod_hrs = labor_hours.get('production_hours', 0)
        setup_hrs = labor_hours.get('setup_cleanup_hours', 0)
        total_hrs = labor_hours.get('total_hours', 0)

        lh_rows.append(['', '', 'Production Hours:', f"{prod_hrs:,.1f}"])
        lh_rows.append(['', '', '+ Setup/Cleanup (15%):', f"{setup_hrs:,.1f}"])
        lh_rows.append(['', '', 'TOTAL HOURS:', f"{total_hrs:,.1f}"])

        lh_cw = [1.8*inch, 1.0*inch, 1.2*inch, 0.8*inch]
        lh_tbl = Table(lh_rows, colWidths=lh_cw, repeatRows=1)
        lh_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (2, -3), (3, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -4), 0.25, BORDER_GRAY),
            ('LINEABOVE', (0, -3), (-1, -3), 0.5, BORDER_GRAY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -4), [WHITE, LIGHT_GRAY]),
        ]))
        story.append(Spacer(1, 4))
        story.append(lh_tbl)

        crew_days = labor_hours.get('crew_days', 0)
        if crew_days > 0:
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f'<i>Equivalent to approximately {crew_days:.1f} person-days (8-hour days).</i>',
                styles['Note']
            ))

    # ── Pricing Model ──
    if pricing:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Pricing Model", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        pm_rows = [['Category', 'Unit Cost', 'Markup']]
        # Map: (config key, display label, unit label)
        pm_map = [
            ('gyp_walls',           'Gyp. Walls',           '/sqft'),
            ('gyp_ceilings',        'Gyp. Ceilings',        '/sqft'),
            ('base_trim',           'Base Trim',            '/LF'),
            ('crown_molding',       'Crown Molding',        '/LF'),
            ('doors_full_paint',    'Doors (Full Paint)',    '/door'),
            ('doors_hm_panel',      'Doors (HM Panel)',     '/door'),
            ('doors_refinish',      'Doors (Refinish)',     '/door'),
            ('windows',             'Windows',              '/window'),
            ('window_sash',         'Window Sash',          '/side'),
            ('window_sill_apron',   'Window Sill/Apron',    '/ea'),
            ('stairs',              'Stairs',               '/section'),
            ('gyp_between_stairs',  'Gyp Between Stairs',   '/sqft'),
            ('level_5_finish',      'Level 5 Finish',       '/sqft'),
            ('exterior_cornice',    'Exterior Cornice',     '/LF'),
            ('exterior_window_trim','Ext. Window Trim',     '/LF'),
            ('exterior_soffit_fascia', 'Ext. Soffit/Fascia', '/sqft'),
            ('exterior_lift_rental','Exterior Lift Rental',  '/unit'),
            ('cmu_walls_full',      'CMU Walls (Full)',     '/sqft'),
            ('cmu_walls_finish_only','CMU Walls (Finish)',  '/sqft'),
            ('exposed_ceiling',     'Exposed Ceiling',      '/sqft'),
            ('concrete_sealer',     'Concrete Sealer',      '/sqft'),
        ]
        seen_labels = set()
        for key, label, unit in pm_map:
            entry = pricing.get(key, {})
            if entry and label not in seen_labels:
                seen_labels.add(label)
                tiers = entry.get('tiers', [])
                markup_val = entry.get('markup', 0)
                if len(tiers) == 1:
                    # Single rate (flat)
                    rate_str = f"${tiers[0]['rate']:.2f}{unit}"
                elif len(tiers) > 1:
                    # Multi-tier: show range e.g. "$0.80–$1.10/sqft"
                    rates = sorted(t['rate'] for t in tiers)
                    rate_str = f"${rates[0]:.2f}–${rates[-1]:.2f}{unit}"
                else:
                    # Legacy flat-rate fallback (backward compat)
                    for legacy_key in ('cost_per_sqft', 'cost_per_lf', 'cost_per_door',
                                       'cost_per_window', 'cost_per_section',
                                       'cost_per_ea', 'cost_per_unit'):
                        if legacy_key in entry:
                            rate_str = f"${entry[legacy_key]:.2f}{unit}"
                            break
                    else:
                        continue
                pm_rows.append([label, rate_str, f"{markup_val:.0%}"])
        if len(pm_rows) > 1:
            story.append(Spacer(1, 4))
            story.append(_header_table(pm_rows, [2.0*inch, 2.0*inch, 1.2*inch]))

    # ── Material Legend ──
    legend = analysis.get('material_legend', [])
    if legend:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Material Legend", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        leg_rows = [['Code', 'Description', 'Paintable']]
        for entry in legend:
            paint_str = 'Yes' if entry.get('paintable') else 'No'
            leg_rows.append([entry.get('code', ''), entry.get('description', ''), paint_str])
        t = _header_table(leg_rows, [1.0*inch, 3.5*inch, 1.0*inch])
        # Center the paintable column
        t.setStyle(TableStyle([('ALIGN', (2, 0), (2, -1), 'CENTER')]))
        story.append(Spacer(1, 4))
        story.append(t)

    # ── Glossary ──
    story.append(Spacer(1, 8))
    story.append(Paragraph("Glossary", styles['SectionHead']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
    story.append(Spacer(1, 4))
    _glossary = [
        ("Doors — Full Paint",
         "Door panel and frame both field-painted (typically a wood door in a wood frame)."),
        ("Doors — HM Panel",
         "Hollow-metal door panel field-painted; the frame is factory-finished and not repainted."),
        ("Doors — Frame Only",
         "Only the door frame is field-painted; the panel (glass or prefinished) is not."),
        ("Base Trim",
         "Painted wall base / baseboard, measured in linear feet (LF)."),
        ("Gyp. Between Stairs",
         "Gypsum-board wall area in and around stair runs — the stepped drywall flanking the stairs."),
        ("Dryfall Ceiling",
         "Spray-applied coating for exposed structure / open ceilings; overspray dries to powder before it reaches the floor."),
        ("Level 5 Finish",
         "The smoothest drywall finish — a full skim coat over the surface — priced per square foot."),
        ("Wallcovering",
         "Vinyl wallcovering / wallpaper. Priced as install labor, not as paint."),
        ("CMU Walls",
         "Concrete masonry unit (block) walls — painted with a block-filler system; priced separately from gypsum."),
        ("Perimeter Wall Boost",
         "A pipeline adjustment that raises extracted wall area toward the perimeter-derived total when per-room extraction came in low."),
    ]
    for _term, _definition in _glossary:
        story.append(Paragraph(f"<b>{_term}</b> — {_definition}", styles['BodyText2']))
        story.append(Spacer(1, 2))

    # ── Missing Items for Painting Estimate ──
    missing = analysis.get('missing_for_painting_estimate', [])
    if missing:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Missing for Painting Estimate", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        for item in missing:
            story.append(Paragraph(f"&bull; {item}", styles['BulletItem']))

    # ── Drawings Referenced but Not Included ──
    drawings = analysis.get('drawings_referenced_but_not_included', [])
    if drawings:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Drawings Referenced but Not Included", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        for d in drawings:
            story.append(Paragraph(f"&bull; {d}", styles['BulletItem']))

    # ── Recommendation ──
    rec = analysis.get('recommendation')
    if rec:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Recommendation", styles['SectionHead']))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(rec, styles['BodyText2']))

    # ── Request For Information (RFI) — placed ABOVE Notes ──
    rfi_items = data.get('rfi_items') or []
    if rfi_items:
        story.append(Spacer(1, 16))

        # Amber warning header bar
        rfi_header_data = [[
            Paragraph(
                '<b>REQUEST FOR INFORMATION (RFI)</b>',
                ParagraphStyle('_rfi_hdr', parent=styles['Normal'],
                               fontSize=13, textColor=DARK_BLUE,
                               alignment=TA_CENTER)
            )
        ]]
        rfi_header_tbl = Table(rfi_header_data, colWidths=[6.5 * inch])
        rfi_header_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), WARN_BG),
            ('BOX', (0, 0), (-1, -1), 1.5, AMBER_DARK),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(rfi_header_tbl)
        story.append(Spacer(1, 4))

        intro_text = (
            "The following items could not be fully determined from the provided "
            "construction documents. Please provide the requested information so we "
            "can finalize your painting estimate."
        )
        story.append(Paragraph(intro_text, styles['BodyText2']))
        story.append(Spacer(1, 8))

        # RFI items table — Category uses Paragraph for proper wrapping
        rfi_table_rows = [['#', 'Category', 'Question / Action Required']]
        for rfi in rfi_items:
            num = str(rfi.get('number', ''))
            cat = str(rfi.get('category', ''))
            q = str(rfi.get('question', ''))
            action = str(rfi.get('action_required', ''))
            # Escape HTML entities
            cat_safe = cat.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            q_safe = q.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            action_safe = action.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            cat_cell = Paragraph(
                f"<b>{cat_safe}</b>", styles['TableCell']
            )
            q_cell = Paragraph(
                f"{q_safe}<br/><br/>"
                f"<i><font color='#555555'>Action Required: {action_safe}</font></i>",
                styles['TableCell']
            )
            rfi_table_rows.append([num, cat_cell, q_cell])

        rfi_col_widths = [0.35 * inch, 1.3 * inch, 4.95 * inch]
        rfi_tbl = Table(rfi_table_rows, colWidths=rfi_col_widths, repeatRows=1)
        rfi_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
            ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, BORDER_GRAY),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, WARN_BG]),
        ]))
        story.append(rfi_tbl)
        story.append(Spacer(1, 12))

        # Closing statement
        closing = (
            "Please respond to the above items so we can finalize your proposal. "
            "You may reply to this document or contact us directly."
        )
        story.append(Paragraph(f"<i>{closing}</i>", styles['RFIClosing']))

    # ── Notes (deduplicated against RFI items) ──
    notes = analysis.get('notes', [])
    if notes:
        # Build a set of note texts that are already covered by RFI questions
        rfi_note_texts = set()
        for rfi in rfi_items:
            q = rfi.get('question', '')
            # Extract the quoted note text from RFI questions like:
            #   'Our review noted: "XXXX". Can you provide ...'
            #   'Our analysis noted: "XXXX". Can you provide ...'
            for prefix in ('Our review noted: "', 'Our analysis noted: "'):
                if prefix in q:
                    start = q.index(prefix) + len(prefix)
                    end = q.find('"', start)
                    if end > start:
                        rfi_note_texts.add(q[start:end].lower())

        filtered_notes = []
        for note in notes:
            note_lower = str(note).lower()
            # Skip if this exact note text appears in an RFI question
            if note_lower in rfi_note_texts:
                continue
            # Also skip if a substantial portion matches (4+ word overlap)
            is_dup = False
            for rfi_text in rfi_note_texts:
                words = rfi_text.split()
                for i in range(len(words) - 3):
                    phrase = " ".join(words[i:i + 4])
                    if phrase in note_lower:
                        is_dup = True
                        break
                if is_dup:
                    break
            if not is_dup:
                filtered_notes.append(note)

        if filtered_notes:
            story.append(Spacer(1, 10))
            story.append(Paragraph("Notes", styles['SectionHead']))
            story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
            story.append(Spacer(1, 4))
            for note in filtered_notes:
                safe_note = str(note).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"&bull; {safe_note}", styles['SmallBullet']))

    # ── Pages Reviewed Note ──
    pages_note = analysis.get('pages_reviewed')
    if pages_note:
        story.append(Spacer(1, 16))
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY))
        story.append(Spacer(1, 4))
        safe = str(pages_note).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        story.append(Paragraph(f"<i>Note: {safe}</i>", styles['Note']))

    doc.build(story)
    return pdf_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        import glob
        files = glob.glob("output/construction_analysis_*.json")
        if files:
            json_path = max(files)
        else:
            print("Usage: python json_to_pdf.py <input.json> [output.pdf]")
            sys.exit(1)
    else:
        json_path = sys.argv[1]

    if len(sys.argv) >= 3:
        pdf_path = sys.argv[2]
    else:
        pdf_path = json_path.rsplit('.', 1)[0] + '.pdf'

    result = json_to_pdf(json_path, pdf_path)
    print(f"PDF created: {result}")
