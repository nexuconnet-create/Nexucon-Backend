"""
PUNDIT results export to Excel (7 Sep 2026 meeting — "{Update Excel}:
spreadsheet data consistency matching generated reports").

The workbook mirrors the Section 5.0 result tables of the official NDT
report EXACTLY: both consume ``NDTReportService._element_data``, so an
exported cell and the printed report cell are the same number by
construction — they cannot diverge. Nothing is computed in the sheet; the
platform's server-computed values are written as numbers.

Velocities are exported in m/s (client unit standard): 4285.71, never
"4,285.71".
"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from apps.reports.ndt_reports import NDTReportService

RESULT_COLUMNS = [
    'STRUCTURAL ELEMENT', 'FLOOR', 'POINT', 'PATH LENGTH (MM)',
    'TRANSIT TIME (µS)', 'PULSE VELOCITY (M/S)', 'E.C.S (MPA)',
    'AVERAGE COMPRESSIVE STRENGTH (MPA)', 'REMARK',
]

CRACK_COLUMNS = [
    'STRUCTURAL ELEMENT', 'FLOOR', 'POINT', 'SPACING L (MM)',
    'T CRACKED (µS)', 'T UNCRACKED (µS)', 'CRACK DEPTH (MM)',
    'MEAN DEPTH (MM)', 'REMARK',
]

_COLUMN_WIDTHS = {
    'STRUCTURAL ELEMENT': 38, 'FLOOR': 16, 'POINT': 7,
    'PATH LENGTH (MM)': 16, 'TRANSIT TIME (µS)': 16,
    'PULSE VELOCITY (M/S)': 18, 'E.C.S (MPA)': 12,
    'AVERAGE COMPRESSIVE STRENGTH (MPA)': 32, 'REMARK': 14,
    'SPACING L (MM)': 14, 'T CRACKED (µS)': 14, 'T UNCRACKED (µS)': 15,
    'CRACK DEPTH (MM)': 15, 'MEAN DEPTH (MM)': 14,
}


def _write_header(sheet, columns, title, subtitle=None):
    """Title block + bold centred header row; returns the first data row."""
    from openpyxl.utils import get_column_letter

    sheet.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    if subtitle:
        sheet.cell(row=2, column=1, value=subtitle).font = Font(italic=True)
    header_row = 4 if subtitle else 3
    for col, header in enumerate(columns, start=1):
        cell = sheet.cell(row=header_row, column=col, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center', vertical='center')
        sheet.column_dimensions[get_column_letter(col)].width = \
            _COLUMN_WIDTHS.get(header, 14)
    sheet.freeze_panes = f'A{header_row + 1}'
    return header_row + 1


def _policy_note(element_data):
    """The standard-error policy statement to append to the sheet header, or
    '' when no curve policy is in force.

    The AVERAGE COMPRESSIVE STRENGTH column carries the same figure the report
    prints, which the report also discloses the derivation of. The header
    therefore has to carry that disclosure too: a spreadsheet whose header
    claims parity with the report, while showing a strength the curve's own
    arithmetic does not produce, would be the one deliverable that hides it.

    The disclosure comes from ``_element_data``'s own ``se_adjustment``, so
    the sheet states exactly what the report states.
    """
    for element in element_data:
        disclosure = element.get('se_adjustment')
        if not isinstance(disclosure, dict):
            continue
        if not (disclosure.get('applied')
                or (disclosure.get('method') or 'none') != 'none'):
            continue
        if disclosure.get('detail'):
            return (' Standard-error policy applied to the average strength '
                    f"column — {disclosure['detail']}")
    return ''


def build_results_workbook(project):
    """
    .xlsx of the project's PUNDIT results, grouped floor -> member ->
    element, matching the report's Section 5.0 tables value-for-value.
    """
    from apps.digital_eye.models import PUNDITTest

    pulse_tests = list(
        PUNDITTest.objects
        .filter(project=project, test_type='pulse_velocity')
        .select_related('device', 'operator')
        .prefetch_related('readings')
        .order_by('structural_element', 'tested_at'))
    crack_tests = list(
        PUNDITTest.objects
        .filter(project=project, test_type='crack_depth')
        .prefetch_related('readings')
        .order_by('structural_element', 'tested_at'))

    # The SAME verdict blocks the report prints — this is the consistency
    # guarantee (means, remarks and spreads are computed once, here).
    element_data = NDTReportService._element_data(pulse_tests)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'RESULTS'
    row = _write_header(
        sheet, RESULT_COLUMNS,
        f'PUNDIT Test Results — {project.name}',
        'Values match the official NDT report Section 5.0 tables exactly '
        '(velocities in m/s).' + _policy_note(element_data))

    floors_present = sorted({e['floor_label'] for e in element_data})
    for floor in floors_present:
        floor_elements = [e for e in element_data
                          if e['floor_label'] == floor]
        for e in sorted(floor_elements, key=lambda x: x['member_type']):
            rows = e['rows']
            remark = e['remark']
            if (e['spread_pct'] is not None and e['spread_pct'] > 2.0):
                remark = (f"{remark} (POINT SPREAD "
                          f"{e['spread_km_s'] * 1000:.0f} M/S, "
                          f"±{e['spread_pct'] / 2:.1f}%)")
            for i, r in enumerate(rows):
                sheet.cell(row=row, column=1,
                           value=e['element'] if i == 0 else None)
                sheet.cell(row=row, column=2, value=floor)
                sheet.cell(row=row, column=3, value=r['label'] or '-')
                sheet.cell(row=row, column=4, value=r['path_mm'])
                sheet.cell(row=row, column=5, value=r['transit_us'])
                sheet.cell(row=row, column=6,
                           value=None if r['velocity_km_s'] is None
                           else round(r['velocity_km_s'] * 1000, 2))
                sheet.cell(row=row, column=7,
                           value=None if r['ecs_mpa'] is None
                           else round(r['ecs_mpa'], 1))
                sheet.cell(row=row, column=8,
                           value=None if e['mean_ecs'] is None
                           else round(e['mean_ecs'], 1))
                sheet.cell(row=row, column=9, value=remark)
                row += 1

    if crack_tests:
        crack_sheet = workbook.create_sheet('CRACK DEPTHS')
        row = _write_header(
            crack_sheet, CRACK_COLUMNS,
            f'Crack Depth Measurements — {project.name}',
            'Time-difference method (BS 1881-203), matching report '
            'Section 5.1.')
        for t in crack_tests:
            rows = t.reading_rows()
            mean_depth = NDTReportService._crack_depth(t)
            remark = NDTReportService._crack_remark(t)
            for i, r in enumerate(rows):
                crack_sheet.cell(row=row, column=1,
                                 value=t.structural_element or 'UNSPECIFIED'
                                 if i == 0 else None)
                crack_sheet.cell(row=row, column=2, value=t.floor or '')
                crack_sheet.cell(row=row, column=3, value=r['label'] or '-')
                crack_sheet.cell(row=row, column=4, value=r['path_mm'])
                crack_sheet.cell(row=row, column=5, value=r['transit_us'])
                crack_sheet.cell(row=row, column=6, value=r['uncracked_us'])
                crack_sheet.cell(row=row, column=7,
                                 value=None if r['crack_depth_mm'] is None
                                 else round(r['crack_depth_mm'], 1))
                crack_sheet.cell(row=row, column=8,
                                 value=None if mean_depth is None
                                 else round(mean_depth, 1))
                crack_sheet.cell(row=row, column=9, value=remark)
                row += 1

    return workbook


def build_results_response_bytes(project):
    """Serialised results workbook (.xlsx bytes) for the download endpoint."""
    import io
    buffer = io.BytesIO()
    build_results_workbook(project).save(buffer)
    return buffer.getvalue()
