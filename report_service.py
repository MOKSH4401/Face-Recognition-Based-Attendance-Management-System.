"""
Attendance Reports export helpers (PDF / Excel / CSV).
Uses ReportLab, Pandas, and OpenPyXL. Does not alter attendance marking.
"""
import logging
from datetime import datetime
from io import BytesIO, StringIO

logger = logging.getLogger(__name__)


def records_to_dataframe(records):
    """Build a pandas DataFrame from attendance report dicts."""
    import pandas as pd

    rows = []
    for r in records:
        rows.append({
            'Student Name': r.get('student_name', ''),
            'Student ID': r.get('student_code', ''),
            'Course': r.get('course_name', ''),
            'Course Code': r.get('course_code', ''),
            'Faculty': r.get('faculty_names', '') or '—',
            'Department': r.get('departments', '') or '—',
            'Date': r.get('date', ''),
            'Time': r.get('time', ''),
        })
    if not rows:
        return pd.DataFrame(columns=[
            'Student Name', 'Student ID', 'Course', 'Course Code',
            'Faculty', 'Department', 'Date', 'Time'
        ])
    return pd.DataFrame(rows)


def generate_attendance_csv(records):
    """Return BytesIO containing CSV bytes."""
    df = records_to_dataframe(records)
    text_buf = StringIO()
    df.to_csv(text_buf, index=False)
    buffer = BytesIO(text_buf.getvalue().encode('utf-8-sig'))
    buffer.seek(0)
    return buffer


def generate_attendance_excel(records, filters_meta=None):
    """Return BytesIO containing .xlsx workbook (Pandas + OpenPyXL)."""
    import pandas as pd
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    df = records_to_dataframe(records)
    buffer = BytesIO()

    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Attendance', index=False, startrow=3)
        ws = writer.sheets['Attendance']

        ws['A1'] = 'Attendance Report'
        ws['A1'].font = Font(bold=True, size=14, color='1F4E79')
        meta = filters_meta or {}
        ws['A2'] = (
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
            f"Records: {len(records)} | "
            f"Date: {meta.get('date_from') or 'All'} to {meta.get('date_to') or 'All'}"
        )

        header_fill = PatternFill('solid', fgColor='1F4E79')
        header_font = Font(color='FFFFFF', bold=True)
        thin = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin')
        )
        header_row = 4
        for col in range(1, len(df.columns) + 1):
            cell = ws.cell(row=header_row, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')
            cell.border = thin

        for row in ws.iter_rows(min_row=header_row + 1, max_row=header_row + len(df),
                                min_col=1, max_col=len(df.columns)):
            for cell in row:
                cell.border = thin

        widths = [22, 14, 22, 12, 24, 18, 12, 10]
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = width

    buffer.seek(0)
    return buffer


def generate_attendance_pdf(records, filters_meta=None):
    """Return BytesIO containing PDF (ReportLab)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('RTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=4)
    meta_style = ParagraphStyle('RMeta', parent=styles['Normal'], fontSize=9, textColor=colors.grey)
    cell_style = ParagraphStyle('RCell', parent=styles['Normal'], fontSize=8, leading=10)

    meta = filters_meta or {}
    story = [
        Paragraph('Attendance Report', title_style),
        Paragraph(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} &nbsp;|&nbsp; "
            f"Records: {len(records)} &nbsp;|&nbsp; "
            f"Date: {meta.get('date_from') or 'All'} → {meta.get('date_to') or 'All'} &nbsp;|&nbsp; "
            f"Course: {meta.get('course_label') or 'All'} &nbsp;|&nbsp; "
            f"Student: {meta.get('student_label') or 'All'} &nbsp;|&nbsp; "
            f"Faculty: {meta.get('faculty_label') or 'All'} &nbsp;|&nbsp; "
            f"Department: {meta.get('department_label') or 'All'}",
            meta_style
        ),
        Spacer(1, 0.15 * inch),
    ]

    headers = ['Student', 'Course', 'Faculty', 'Department', 'Date', 'Time']
    data = [[Paragraph(f'<b>{h}</b>', cell_style) for h in headers]]

    for r in records:
        student = f"{r.get('student_name', '')}<br/><font size='7'>{r.get('student_code', '')}</font>"
        course = f"{r.get('course_name', '')}<br/><font size='7'>{r.get('course_code', '')}</font>"
        data.append([
            Paragraph(student, cell_style),
            Paragraph(course, cell_style),
            Paragraph(r.get('faculty_names') or '—', cell_style),
            Paragraph(r.get('departments') or '—', cell_style),
            Paragraph(r.get('date', ''), cell_style),
            Paragraph(r.get('time', ''), cell_style),
        ])

    if len(data) == 1:
        data.append([Paragraph('No records found.', cell_style), '', '', '', '', ''])

    table = Table(
        data,
        colWidths=[1.9 * inch, 1.9 * inch, 2.0 * inch, 1.5 * inch, 0.95 * inch, 0.8 * inch]
    )
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E79')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F7FA')]),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return buffer
