"""
Lagos State Materials Testing Laboratory-style NDT (PUNDIT) report.

Visual format replicates the laboratory's in-situ integrity test report:
US Letter portrait, Times serif, typewriter cover block, table of contents
with dotted page-number leaders, numbered sections and ruled data tables.

Every figure is rendered from live PUNDITTest / FieldDevice / Project rows.
Where a value has not been recorded it is shown honestly ('-',
'NOT RECORDED') — nothing is ever fabricated. Estimated compressive
strength (E.C.S) is derived from a single fixed calibration curve that is
disclosed in full in Section 3.0 of the rendered report.
"""
import hashlib
import logging
from datetime import datetime
from itertools import groupby

from fpdf import FPDF

from .ai_reports import _latin1

logger = logging.getLogger(__name__)

INK = (0, 0, 0)
RULE_GREY = (60, 60, 60)

# ---------------------------------------------------------------------------
# E.C.S calibration (disclosed in Section 3.0 of the rendered report)
# ---------------------------------------------------------------------------
ECS_SLOPE = 8.961        # N/mm2 per km/s
ECS_INTERCEPT = -7.97    # N/mm2
ECS_VALID_MIN_KM_S = 2.0
ECS_VALID_MAX_KM_S = 5.0

ECS_CALIBRATION_SOURCE = (
    "Estimated compressive strength (E.C.S) values in this report are derived "
    "from the laboratory's fixed ultrasonic pulse-velocity calibration curve "
    "f_cu = 8.96 x V - 7.97 (f_cu in N/mm2, V in km/s), established by "
    "least-squares regression over the laboratory's reference control pairs "
    "(2.9, 19), (3.9, 25), (4.0, 27), (4.2, 29) and (4.4, 34) and valid over "
    "the range 2.0 - 5.0 km/s. BS 1881-203:1999 notes that no unique "
    "velocity-strength relationship exists for all concretes; the curve above "
    "is the documented calibration applied by this laboratory for Nigerian "
    "site concrete and is applied uniformly to every result in Section 5.0. "
    "Velocities outside the calibrated range are reported without an E.C.S "
    "estimate rather than extrapolated."
)

ECS_FORMULA_LINE = "E.C.S: f_cu = 8.96 x V - 7.97  (f_cu in N/mm2, V in km/s; valid 2.0 - 5.0 km/s)"


def estimated_compressive_strength(velocity_km_s):
    """
    E.C.S (N/mm2) from pulse velocity via the fixed calibration curve.
    Returns None when the velocity is missing or outside the calibrated
    2.0 - 5.0 km/s range (values are never extrapolated).
    """
    if velocity_km_s is None:
        return None
    if not (ECS_VALID_MIN_KM_S <= velocity_km_s <= ECS_VALID_MAX_KM_S):
        return None
    return ECS_SLOPE * velocity_km_s + ECS_INTERCEPT


def _wrap_lines(pdf, text, width):
    """
    Word-wrap ``text`` into lines no wider than ``width`` mm using the PDF's
    current font. A single token wider than the column (e.g. a 64-character
    content digest) is hard-broken — no characters are ever inserted or
    dropped, so cell contents can never spill into the neighbouring column.
    """
    # Transcribe first: fpdf2's get_string_width encodes strictly latin-1,
    # so measuring raw live-DB text (em-dashes, curly quotes, ...) would
    # raise — and wrapping must measure exactly the text that gets drawn.
    text = _latin1('-' if text in (None, '') else text).replace('\n', ' ')
    words = [w for w in text.split(' ') if w]
    lines = []
    cur = ''
    for word in words:
        trial = f'{cur} {word}'.strip()
        if cur and pdf.get_string_width(trial) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        while pdf.get_string_width(line) > width and len(line) > 1:
            cut = len(line) - 1
            while cut > 1 and pdf.get_string_width(line[:cut]) > width:
                cut -= 1
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out or ['-']


# ---------------------------------------------------------------------------
# PDF subclass — Letter portrait, Times, typewriter running header
# ---------------------------------------------------------------------------
class MTLReportPDF(FPDF):
    """US Letter portrait document with the lab's running header
    '<serial> / MTL/NDT/<year>' + page number on every page after the cover."""

    def __init__(self, running_header, *args, **kwargs):
        super().__init__(orientation='P', unit='mm', format='Letter', *args, **kwargs)
        self.running_header = running_header
        self.set_margins(20, 24, 20)
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        if self.page_no() <= 1:      # no running header on the cover
            return
        self.set_font('Times', '', 10)
        self.set_text_color(*INK)
        self.set_y(10)
        self.cell(0, 6, _latin1(self.running_header))
        self.set_xy(self.w - self.r_margin - 30, 10)
        self.cell(30, 6, f'Page {self.page_no()}', align='R')
        self.set_draw_color(*RULE_GREY)
        self.set_line_width(0.2)
        self.line(self.l_margin, 17.5, self.w - self.r_margin, 17.5)
        # fpdf2 leaves the cursor wherever header() ends. Park it below the
        # ruled header line — otherwise the first body content after ANY page
        # break (auto or manual) is drawn at y~10, on top of this header.
        self.set_xy(self.l_margin, self.t_margin)

    def footer(self):
        if self.page_no() <= 1:      # no footer on the cover
            return
        self.set_y(-16)
        self.set_font('Times', 'I', 9)
        self.set_text_color(*RULE_GREY)
        self.cell(0, 6, _latin1(self.running_header), align='C')


# ---------------------------------------------------------------------------
# Builder — typewriter layout primitives
# ---------------------------------------------------------------------------
class NDTReportBuilder:
    """Ruled, typewriter-style MTL layout primitives."""

    def __init__(self, running_header):
        self.pdf = MTLReportPDF(running_header)
        self.running_header = running_header

    # ------------------------------------------------------------ cover
    def cover(self, serial, year, project_name, site_line, client_name, test_dates):
        pdf = self.pdf
        pdf.add_page()
        pdf.set_text_color(*INK)
        pdf.set_y(26)
        pdf.set_font('Times', 'B', 14)
        pdf.cell(0, 10, serial, align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 10, f'MTL/NDT/{year}', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(10)

        pdf.set_font('Times', 'B', 16)
        for line in (
            'REPORT ON AN IN-SITU INTEGRITY TEST',
            '(NON-DESTRUCTIVE) OF COMPRESSIVE',
            'STRENGTH OF STRUCTURAL ELEMENTS',
        ):
            pdf.cell(0, 10, line, align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(8)

        pdf.set_font('Times', 'B', 14)
        pdf.cell(0, 9, 'BY', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Times', 'B', 18)
        pdf.cell(0, 10, 'LAGOS STATE MATERIALS TESTING LABORATORY', align='C',
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Times', 'B', 16)
        pdf.cell(0, 9, 'OJODU BERGER, LAGOS.', align='C',
                 new_x='LMARGIN', new_y='NEXT')
        pdf.ln(8)

        pdf.set_font('Times', 'B', 14)
        pdf.cell(0, 9, 'ON', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Times', '', 14)
        pdf.multi_cell(0, 9, _latin1(project_name or 'AN EXISTING SITE'),
                       align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(6)

        pdf.set_font('Times', 'B', 14)
        pdf.cell(0, 9, 'FOR', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Times', '', 14)
        pdf.multi_cell(0, 9, _latin1(client_name or '-'), align='C',
                       new_x='LMARGIN', new_y='NEXT')
        if site_line:
            pdf.multi_cell(0, 9, _latin1(site_line), align='C',
                           new_x='LMARGIN', new_y='NEXT')
        pdf.ln(10)

        pdf.set_font('Times', '', 12)
        if test_dates:
            pdf.cell(0, 9, _latin1(f'DATE OF TEST: {test_dates}'),
                     align='C', new_x='LMARGIN', new_y='NEXT')
        else:
            pdf.cell(0, 9, 'DATE OF TEST: NOT RECORDED',
                     align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 9, 'DATE OF REPORT: '
                 + datetime.now().strftime('%d/%m/%Y'),
                 align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)

    # ------------------------------------------------------- TOC placeholder
    def toc_page(self):
        """Reserve the Table of Contents page; entries are filled
        automatically from the registered sections at output() time."""
        self.pdf.add_page()
        self.pdf.insert_toc_placeholder(self._render_toc, pages=1)

    @staticmethod
    def _render_toc(pdf, outline):
        # CRITICAL: this runs during output(), when no font state is active.
        from .services import ReportService
        pdf.set_font('Times', 'B', 14)
        pdf.set_text_color(*INK)
        pdf.cell(0, 11, 'TABLE OF CONTENTS', align='C',
                 new_x='LMARGIN', new_y='NEXT')
        pdf.ln(5)
        pdf.set_font('Times', '', 12)
        right_x = pdf.w - pdf.r_margin - 10
        for section in outline:
            y = pdf.get_y()
            name = _latin1(section.name)
            max_name_w = right_x - pdf.l_margin - 14
            # Wrap long entries onto continuation lines (at word boundaries);
            # the page number sits on the final line, after a dotted leader.
            line_h = 7.5
            lines = []
            cur = ''
            for word in name.split(' '):
                trial = f'{cur} {word}'.strip()
                if cur and pdf.get_string_width(trial) > max_name_w:
                    lines.append(cur)
                    cur = word
                else:
                    cur = trial
            if cur:
                lines.append(cur)
            for i, line in enumerate(lines):
                if i:
                    y = pdf.get_y()
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(min(pdf.get_string_width(line) + 2, max_name_w),
                         line_h, line)
                if i == len(lines) - 1:
                    x_start = pdf.l_margin + pdf.get_string_width(line) + 2
                    if right_x - 2 > x_start + 2:
                        ReportService._dashed_line(
                            pdf, x_start + 1, y + 4.4, right_x - 2, y + 4.4,
                            dash=0.5, gap=0.5, color=RULE_GREY, width=0.2)
                    pdf.set_xy(right_x, y)
                    pdf.cell(10, line_h, str(section.page_number), align='R')
                pdf.set_xy(pdf.l_margin, y + line_h)

    # ------------------------------------------------------------- sections
    def section(self, number, heading):
        """e.g. section('4.2', 'METHODOLOGY') — registers a TOC entry."""
        self.pdf.start_section(f'{number} {heading}'.strip(), level=0)
        self.pdf.ln(2)
        self.pdf.set_font('Times', 'B', 16)
        self.pdf.set_text_color(*INK)
        # multi_cell so long all-caps headings wrap instead of overflowing.
        self.pdf.multi_cell(0, 9, _latin1(f'{number} {heading}'.strip()),
                            new_x='LMARGIN', new_y='NEXT')
        self.pdf.set_font('Times', '', 12)
        self.pdf.ln(1)

    def para(self, text):
        self.pdf.set_font('Times', '', 12)
        self.pdf.set_text_color(*INK)
        self.pdf.multi_cell(0, 6, _latin1(text))
        self.pdf.ln(1.5)

    def bullet(self, text):
        pdf = self.pdf
        pdf.set_font('Times', '', 12)
        pdf.set_text_color(*INK)
        pdf.cell(5, 6, '')
        pdf.multi_cell(0, 6, _latin1(f'- {text}'))
        pdf.ln(0.5)

    def ln_gap(self, height=4):
        self.pdf.ln(height)

    def kv(self, key, value):
        pdf = self.pdf
        pdf.set_font('Times', 'B', 12)
        pdf.set_text_color(*INK)
        pdf.cell(60, 6.5, _latin1(key))
        pdf.set_font('Times', '', 12)
        pdf.multi_cell(0, 6.5, _latin1(value if value not in (None, '') else '-'),
                       new_x='LMARGIN', new_y='NEXT')

    def ruled_table(self, headers, rows, widths, aligns=None):
        """Plain bordered table. Every cell's text is word-wrapped to its
        column width before drawing (fpdf2's ``cell`` never wraps, so
        unwrapped text would run into the neighbouring column), and each row
        is as tall as its tallest wrapped cell. A manual page-break guard
        redraws the header row on the new page."""
        pdf = self.pdf
        aligns = aligns or ['L'] * len(headers)
        page_w = pdf.w - pdf.l_margin - pdf.r_margin
        if sum(widths) > page_w:
            widths = [w * page_w / sum(widths) for w in widths]
        pad = 2 * pdf.c_margin      # text area inside each cell border
        line_h = 5.2

        def _draw_row(cells, is_header):
            pdf.set_font('Times', 'B' if is_header else '', 10)
            pdf.set_text_color(*INK)
            wrapped = [_wrap_lines(pdf, c, w - pad)
                       for c, w in zip(cells, widths)]
            n_lines = max(len(lines) for lines in wrapped)
            row_h = n_lines * line_h + 3
            if pdf.get_y() + row_h > pdf.h - pdf.b_margin - 2:
                pdf.add_page()
                if not is_header:
                    _draw_row(headers, is_header=True)   # redraw header row
            y0 = pdf.get_y()
            x = pdf.l_margin
            for lines, w, a in zip(wrapped, widths, aligns):
                pdf.set_xy(x, y0)
                pdf.cell(w, row_h, border=1)             # the ruled box
                ty = y0 + (row_h - len(lines) * line_h) / 2
                for ln in lines:
                    pdf.set_xy(x, ty)
                    pdf.cell(w, line_h, _latin1(ln), align=a)
                    ty += line_h
                x += w
            pdf.set_xy(pdf.l_margin, y0 + row_h)

        _draw_row(headers, is_header=True)
        for row in rows:
            _draw_row(row, is_header=False)

    def signoff_block(self, lines):
        """Boxed sign-off block. Every value is pre-wrapped to its column so
        the box height is exact — a wrapped value (e.g. the 64-character
        content digest) can never outgrow the box or run into the border."""
        pdf = self.pdf
        line_h = 7
        label_w = 55
        value_w = pdf.w - pdf.l_margin - pdf.r_margin - label_w
        pdf.set_font('Times', '', 11)
        wrapped = [_wrap_lines(pdf, value, value_w - 2 * pdf.c_margin)
                   for _, value in lines]
        total_h = 6 + sum(max(1, len(w)) * line_h for w in wrapped) + 2
        if pdf.get_y() + total_h > pdf.h - pdf.b_margin:
            pdf.add_page()
        pdf.set_draw_color(*INK)
        pdf.set_line_width(0.4)
        y0 = pdf.get_y()
        pdf.rect(pdf.l_margin, y0, pdf.w - pdf.l_margin - pdf.r_margin,
                 total_h)
        y = y0 + 3
        for (label, _), w_lines in zip(lines, wrapped):
            pdf.set_xy(pdf.l_margin, y)
            pdf.set_font('Times', 'B', 11)
            pdf.cell(label_w, line_h, _latin1(label))
            pdf.set_font('Times', '', 11)
            for ln in w_lines:
                pdf.set_xy(pdf.l_margin + label_w, y)
                pdf.cell(value_w, line_h, _latin1(ln))
                y += line_h
        pdf.set_line_width(0.2)
        pdf.set_y(y0 + total_h + 4)

    def bytes(self):
        return bytes(self.pdf.output())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class NDTReportService:
    """Streams the MTL-style NDT report for a project and archives the exact
    generated bytes for the statutory audit trail (ArchivedReport)."""

    # ------------------------------------------------------------ utils
    @staticmethod
    def _report_number(project, tests):
        """Deterministic serial derived from the project UUID plus the live
        PUNDIT test count — reproducible from the database alone."""
        digest = hashlib.sha256(str(project.id).encode()).hexdigest()
        serial = (int(digest[:8], 16) + len(tests)) % 10000
        years = [t.tested_at.year for t in tests if t.tested_at]
        year = min(years) if years else datetime.now().year
        return f'{serial:04d} / MTL/NDT/{year}', year

    @staticmethod
    def _content_hash(parts):
        payload = '|'.join(str(p) for p in parts)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _velocity(test):
        """Stored velocity when the analysis pipeline ran, else the same
        deterministic BS 1881-203 computation the platform's adapter uses."""
        if test.velocity_km_s is not None:
            return test.velocity_km_s
        from apps.digital_eye.adapters import PUNDITAdapter
        return PUNDITAdapter.compute_velocity_km_s(
            test.path_length_mm, test.pulse_time_us)

    @staticmethod
    def _fmt(value, decimals=2):
        return '-' if value in (None, '') else f'{value:.{decimals}f}'

    @staticmethod
    def _fmt_int(value):
        return '-' if value is None else str(int(round(value)))

    # -------------------------------------------------- statutory archive
    @classmethod
    def archive_ndt_report(cls, project, user, pdf_bytes):
        """
        Persist the exact generated dossier (bytes, checksum, counts, pass
        verdict) as an ArchivedReport. Identical content is never archived
        twice — deduped on ``content_key``, the deterministic digest of the
        underlying records (PDF bytes alone differ between runs because fpdf2
        stamps a creation timestamp, so they cannot key the dedupe).
        """
        import io
        from django.core.files.base import ContentFile
        from apps.digital_eye.models import PUNDITTest
        from .models import ArchivedReport

        tests = list(PUNDITTest.objects.filter(project=project))
        report_no, _ = cls._report_number(project, tests)
        # Exactly the parts the report's own integrity section hashes, so
        # "archived once" and "digest printed in the PDF" always agree.
        content_key = cls._content_hash(
            ['ndt', project.id, sorted(str(t.id) for t in tests),
             len(tests),
             [round(cls._velocity(t), 6) if cls._velocity(t) is not None
              else None for t in tests],
             report_no])
        existing = ArchivedReport.objects.filter(
            project=project, report_kind='ndt', content_key=content_key,
        ).first()
        if existing is not None:
            return existing

        # Same strength basis as the report itself: a test is
        # strength-assessed when its velocity lies inside the E.C.S
        # calibration range; passing means fcu >= 25 MPa.
        assessed = 0
        passed = 0
        for t in tests:
            velocity = t.velocity_km_s
            if velocity is None:
                from apps.digital_eye.adapters import PUNDITAdapter
                velocity = PUNDITAdapter.compute_velocity_km_s(
                    t.path_length_mm, t.pulse_time_us)
            if velocity is not None and 2.0 <= velocity <= 5.0:
                assessed += 1
                if estimated_compressive_strength(velocity) >= 25.0:
                    passed += 1
        if assessed == 0:
            compliance = 'NOT_ASSESSED'
        elif passed < assessed:
            compliance = 'FLAGGED_DEFECTS'
        else:
            compliance = 'COMPLIANT'

        archived = ArchivedReport(
            project=project,
            report_kind='ndt',
            report_reference=report_no,
            title='BS 1881-203 Ultrasonic Pulse Velocity (UPV) NDT Report',
            file_size_bytes=len(pdf_bytes),
            sha256_checksum=hashlib.sha256(pdf_bytes).hexdigest(),
            content_key=content_key,
            test_count=len(tests),
            assessed_count=assessed,
            passed_count=passed,
            compliance_status=compliance,
            generated_by=user if getattr(user, 'is_authenticated', False) else None,
        )
        archived.file.save(
            f'ndt_report_{project.id.hex[:12]}_{content_key[:12]}.pdf',
            ContentFile(pdf_bytes), save=False)
        archived.save()
        return archived

    # -------------------------------------------------------- main entry
    @classmethod
    def generate_ndt_report(cls, project, user=None):
        from apps.digital_eye.adapters import PUNDITAdapter
        from apps.digital_eye.models import PUNDITTest

        tests = list(
            PUNDITTest.objects
            .filter(project=project)
            .select_related('device', 'operator')
            .prefetch_related('files')
            .order_by('structural_element', 'tested_at')
        )

        report_no, year = cls._report_number(project, tests)
        serial = report_no.split(' / ')[0]
        builder = NDTReportBuilder(report_no)

        # ------------------------------------------------------------ cover
        site_parts = [p for p in (project.site_address, project.lga,
                                  project.state) if p]
        dates = None
        if tests:
            tested = [t.tested_at for t in tests if t.tested_at]
            if tested:
                if min(tested).date() == max(tested).date():
                    dates = min(tested).strftime('%d/%m/%Y')
                else:
                    dates = (f'{min(tested).strftime("%d/%m/%Y")} - '
                             f'{max(tested).strftime("%d/%m/%Y")}')
        builder.cover(serial, year, project.name,
                      ', '.join(site_parts), project.client_name, dates)

        # ------------------------------------------------------------- TOC
        builder.toc_page()

        pulse_tests = [t for t in tests if t.test_type == 'pulse_velocity']
        crack_tests = [t for t in tests if t.test_type == 'crack_depth']
        surface_tests = [t for t in tests if t.test_type == 'surface_quality']
        devices = []
        for t in tests:
            if t.device and t.device not in devices:
                devices.append(t.device)
        elements = sorted({t.structural_element or 'UNSPECIFIED'
                           for t in pulse_tests})

        # ------------------------------------------------------ 1.0 INTRO
        builder.section('1.0', 'INTRODUCTION')
        builder.para(
            f'This report presents the findings of a non-destructive '
            f'(ultrasonic pulse velocity) integrity investigation carried out '
            f'on the project "{project.name or "-"}"'
            + (f' for {project.client_name}' if project.client_name else '')
            + '. The investigation was conducted using the Portable Ultrasonic '
              'Non-Destructive Digital Indicating Tester (PUNDIT) in '
              'accordance with BS 1881-203:1999 and ASTM C597.'
        )
        builder.kv('Project Reference', project.reference_number)
        builder.kv('Project Number', project.project_number)
        builder.kv('Site Address', project.site_address)
        builder.kv('Local Government Area', project.lga)
        builder.kv('State', project.state)
        builder.kv('Structural System', project.structural_system)
        builder.kv('Number of Floors', project.number_of_floors)
        builder.kv('Development Category', project.development_category)
        builder.kv('Structural Elements Tested', len(elements) or None)
        builder.kv('Ultrasonic Tests Recorded', len(pulse_tests))
        if not pulse_tests:
            builder.para('No ultrasonic pulse velocity tests have been '
                         'recorded for this project as at the date of this '
                         'report.')

        # ------------------------------------------------------ 2.0 PURPOSE
        builder.section('2.0', 'PURPOSE OF INVESTIGATION')
        builder.para('The purpose of the investigation is to:')
        builder.bullet('Determine the present status of the tested structural '
                       'elements from the outcome of the ultrasonic '
                       'non-destructive tests, with respect to BS EN '
                       '12504-4:2021 and ASTM C597-09.')
        builder.bullet('Determine the ongoing concrete quality of the tested '
                       'structural elements with respect to BS 8110: Part 1 '
                       '1997, using the laboratory velocity-strength '
                       'calibration stated in Section 3.0.')
        builder.bullet('Provide engineering advice based on the recorded '
                       'ultrasonic indications and the visual observations '
                       'made during the field work.')
        builder.para(
            f'Recorded tests comprise {len(pulse_tests)} pulse velocity '
            f'test(s), {len(crack_tests)} crack depth test(s) and '
            f'{len(surface_tests)} surface quality observation(s).'
        )

        # -------------------------------------------------- 3.0 LITERATURE
        builder.section('3.0', 'LITERATURE REVIEW')
        builder.para(
            'The ultrasonic pulse velocity method is a well-established '
            'non-destructive technique for assessing the quality and '
            'homogeneity of concrete. A pulse of longitudinal vibrations is '
            'produced by an electro-acoustical transducer held in contact '
            'with one face of the concrete; a similar transducer on the '
            'opposite face receives the pulse, and the time taken for the '
            'pulse to traverse the concrete is measured electronically. The '
            'pulse velocity is then the path length divided by the transit '
            'time.'
        )
        builder.para(
            'Velocity is influenced by the elastic modulus and density of the '
            'concrete, and hence by its strength and condition. Following '
            'Whitehurst\'s classification (adopted in BS 1881-203:1999 and '
            'ASTM C597), velocities of 4.5 km/s and above indicate excellent '
            'quality; 3.75 - 4.5 km/s good; 3.0 - 3.75 km/s questionable; '
            '2.0 - 3.0 km/s poor; and below 2.0 km/s very poor.'
        )
        builder.para(ECS_CALIBRATION_SOURCE)

        # ------------------------------------------- 4.0 LOCATION & FIELDWORK
        builder.section('4.0', 'LOCATION OF SITE, WEATHER CONDITION AND '
                               'FIELD WORK / EQUIPMENT STATUS CHECK')
        builder.kv('Site Address', project.site_address)
        builder.kv('Ward / Area', project.ward_area)
        builder.kv('Local Government Area', project.lga)
        builder.kv('State', project.state)
        if project.latitude is not None and project.longitude is not None:
            builder.kv('Site Coordinates (WGS84)',
                       f'{project.latitude:.6f}, {project.longitude:.6f}')
        test_coords = [f'{t.latitude:.6f}, {t.longitude:.6f}'
                       for t in tests
                       if t.latitude is not None and t.longitude is not None]
        if test_coords:
            builder.para('Test coordinates recorded on site: '
                         + '; '.join(sorted(set(test_coords))) + '.')
        builder.kv('Weather Condition',
                   'NOT RECORDED (no weather observation model exists on the '
                   'platform - the platform does not fabricate weather data)')
        temperatures = [t.surface_temperature_c for t in tests
                        if t.surface_temperature_c is not None]
        if temperatures:
            builder.para(
                f'Recorded concrete surface temperatures: '
                f'{", ".join(f"{t:.1f} deg C" for t in temperatures)}.'
            )
        if devices:
            builder.para(
                f'Field work was carried out with {len(devices)} registered '
                f'field device(s). Equipment status and calibration details '
                f'are listed in Section 4.4.'
            )
        else:
            builder.para('No field device was recorded against these tests.')

        # -------------------------------------------------- 4.1 VISUAL TEST
        builder.section('4.1', 'VISUAL TEST')
        visual_notes = []
        for t in tests:
            if t.surface_condition:
                visual_notes.append(
                    f'{t.structural_element or "Unspecified element"}: '
                    f'{t.surface_condition}')
            elif t.notes:
                visual_notes.append(
                    f'{t.structural_element or "Unspecified element"}: '
                    f'{t.notes}')
        if visual_notes:
            for note in visual_notes:
                builder.bullet(note)
        else:
            builder.para('No visual/surface condition observations recorded.')

        # ------------------------------------------------ 4.2 METHODOLOGY
        builder.section('4.2', 'METHODOLOGY')
        builder.para(
            'Non-destructive concrete strength determination was carried out '
            'using the Portable Ultrasonic Non-Destructive Digital Indicating '
            'Tester (PUNDIT). The pulse velocity V is computed as the path '
            'length L divided by the transit time t (V = L / t, with L in mm '
            'and t in microseconds, so V is in km/s). Concrete quality is '
            'graded against the BS 1881-203 / ASTM C597 velocity bands '
            'stated in Section 3.0.'
        )
        freqs = sorted({t.transducer_frequency_khz for t in tests
                        if t.transducer_frequency_khz})
        if freqs:
            builder.para(
                'Tests were carried out with '
                + ' and '.join(f'{f} kHz' for f in freqs)
                + ' transducers, as recorded against each test.'
            )
        if crack_tests:
            builder.para(
                'Crack depth was determined by the time-difference method of '
                'BS 1881-203: d = (L / 2) x sqrt((t_cracked / t_uncracked)^2 '
                '- 1), where L is the path length between transducers '
                'straddling the crack.'
            )
        builder.para('Estimated compressive strength conversion: '
                     + ECS_FORMULA_LINE
                     + '. The full calibration statement is given in '
                       'Section 3.0.')

        # ------------------------------------------------------- 4.3 REBAR
        builder.section('4.3', 'REINFORCING BAR (REBAR) ASSESSMENT')
        if crack_tests:
            builder.para(
                'No dedicated reinforcing bar cover measurements (e.g. '
                'Profoscope scans) were recorded during this investigation. '
                'Observations are limited to the ultrasonic indications '
                'reported in Sections 4.1 and 5.0; measured crack depths '
                'that may affect concrete cover are reported in the crack '
                'depth table of Section 5.0.'
            )
        elif visual_notes:
            builder.para(
                'No dedicated reinforcing bar cover measurements were '
                'recorded during this investigation; observations are '
                'limited to those reported in Sections 4.1 and 5.0.'
            )
        else:
            builder.para(
                'No dedicated reinforcing bar cover measurements were '
                'recorded during this investigation.'
            )

        # ------------------------------------------------------ 4.4 EQUIPMENT
        builder.section('4.4', 'EQUIPMENT / REBAR ASSESSMENT TABLE')
        if devices:
            builder.ruled_table(
                ['DEVICE REF', 'MODEL', 'MANUFACTURER', 'SERIAL / ASSET ID',
                 'STATUS', 'CALIBRATION DATE'],
                [[d.device_reference, d.model or '-', d.manufacturer or '-',
                  d.device_id or '-', d.status or '-',
                  d.calibration_date.strftime('%d/%m/%Y')
                  if d.calibration_date else '-'] for d in devices],
                [24, 28, 30, 34, 24, 26],
            )
        else:
            builder.para('No field device was recorded against these tests.')
        if crack_tests:
            builder.para('Crack depth measurements are tabulated in '
                         'Section 5.0 (time-difference method).')
        else:
            builder.para('No crack depth measurements recorded.')

        # ------------------------------------------------ 5.0 ANALYSIS
        builder.section('5.0', 'ANALYSIS OF TEST RESULTS')
        pending_count = sum(1 for t in tests if t.quality_grade == 'pending')
        if pending_count:
            builder.para(
                f'{pending_count} test(s) have not been run through the '
                f'platform analysis endpoint; velocities shown are computed '
                f'directly from the recorded measurements using the same '
                f'deterministic BS 1881-203 relations.'
            )
        builder.para(
            'Element average E.C.S is computed from the element mean pulse '
            'velocity through the Section 3.0 calibration curve. Velocities '
            'outside the calibrated 2.0 - 5.0 km/s range are reported '
            'without an E.C.S estimate rather than extrapolated.'
        )

        grade_counts = {}
        element_grades = []
        all_velocities = []
        if not pulse_tests:
            builder.para('No pulse velocity tests recorded for this project.')
        else:
            for element, rows in groupby(
                    pulse_tests,
                    key=lambda t: t.structural_element or 'UNSPECIFIED'):
                rows = list(rows)
                velocities = [v for v in (cls._velocity(t) for t in rows)
                              if v is not None]
                all_velocities.extend(velocities)
                mean_v = (sum(velocities) / len(velocities)
                          if velocities else None)
                grade = PUNDITAdapter.grade_quality(mean_v)
                element_grades.append((element, mean_v, grade))
                grade_counts[grade] = grade_counts.get(grade, 0) + 1
                builder.para(f'STRUCTURAL ELEMENT: {element}')
                builder.ruled_table(
                    ['S/N', 'TEST REF', 'PATH (MM)', 'TRANSIT TIME (US)',
                     'PULSE VELOCITY (KM/S)', 'E.C.S (N/MM2)'],
                    [[i,
                      t.test_reference,
                      cls._fmt(t.path_length_mm, 1),
                      cls._fmt(t.pulse_time_us, 1),
                      cls._fmt(cls._velocity(t)),
                      cls._fmt_int(estimated_compressive_strength(
                          cls._velocity(t)))]
                     for i, t in enumerate(rows, 1)],
                    [12, 36, 26, 32, 36, 34],
                    ['C', 'L', 'C', 'C', 'C', 'C'],
                )
                builder.para(
                    f'AVERAGE PULSE VELOCITY: {cls._fmt(mean_v)} KM/S    '
                    f'AVERAGE E.C.S: '
                    f'{cls._fmt_int(estimated_compressive_strength(mean_v))}'
                    f' N/MM2    REMARK: {grade.upper() if grade != "pending" else "NOT RECORDED"}'
                )
                builder.ln_gap()

        if crack_tests:
            builder.section('5.1', 'CRACK DEPTH MEASUREMENTS '
                                   '(TIME-DIFFERENCE METHOD)')
            builder.ruled_table(
                ['ELEMENT', 'PATH (MM)', 'T CRACKED (US)',
                 'T UNCRACKED (US)', 'CRACK DEPTH (MM)', 'REMARK'],
                [[t.structural_element or 'UNSPECIFIED',
                  cls._fmt(t.crack_path_length_mm, 1),
                  cls._fmt(t.crack_pulse_time_us, 1),
                  cls._fmt(t.uncracked_pulse_time_us, 1),
                  cls._fmt(cls._crack_depth(t), 1),
                  cls._crack_remark(t)] for t in crack_tests],
                [32, 24, 26, 28, 30, 36],
                ['L', 'C', 'C', 'C', 'C', 'L'],
            )

        if surface_tests:
            builder.section('5.2', 'SURFACE QUALITY OBSERVATIONS')
            builder.ruled_table(
                ['ELEMENT', 'SURFACE CONDITION', 'NOTES'],
                [[t.structural_element or 'UNSPECIFIED',
                  t.surface_condition or '-',
                  t.notes or '-'] for t in surface_tests],
                [40, 60, 76],
            )

        # ---------------------------------------------- 6.0 RECOMMENDATIONS
        builder.section('6.0', 'RECOMMENDATIONS')
        recommendations = []
        max_crack = None
        for t in crack_tests:
            depth = cls._crack_depth(t)
            if depth is not None:
                max_crack = depth if max_crack is None else max(max_crack, depth)
        for element, mean_v, grade in element_grades:
            for rec in PUNDITAdapter._recommendations(grade, max_crack):
                line = f'[{rec["priority"].upper()}] {rec["recommendation"]}'
                if line not in recommendations:
                    recommendations.append(line)
        # Stored analysis records carry the platform's official wording —
        # surface them when present.
        from apps.evidence.models import AIAnalysisRecord
        for record in (AIAnalysisRecord.objects
                       .filter(project=project, analysis_type='pundit')
                       .order_by('-created_at')[:20]):
            for rec in (record.recommendations or []):
                if isinstance(rec, dict):
                    line = f'[{str(rec.get("priority", "Routine")).upper()}] ' \
                           f'{rec.get("recommendation", "")}'
                else:
                    line = str(rec)
                if line not in recommendations:
                    recommendations.append(line)
        if recommendations:
            for rec in recommendations:
                builder.bullet(rec)
        else:
            builder.para('No adverse findings recorded; recorded concrete '
                         'quality falls within acceptable velocity bands.')

        # -------------------------------------------------- 7.0 CONCLUSION
        builder.section('7.0', 'CONCLUSION')
        if all_velocities:
            mean_all = sum(all_velocities) / len(all_velocities)
            below_good = [e for e, v, g in element_grades
                          if g in ('questionable', 'poor', 'very_poor')]
            builder.para(
                f'A total of {len(pulse_tests)} ultrasonic pulse velocity '
                f'test(s) were carried out on {len(element_grades)} '
                f'structural element(s). The overall mean pulse velocity was '
                f'{mean_all:.2f} km/s (minimum {min(all_velocities):.2f} '
                f'km/s, maximum {max(all_velocities):.2f} km/s).'
            )
            summary = ', '.join(f'{c} {g.replace("_", " ")}'
                                for g, c in sorted(grade_counts.items()))
            builder.para(f'Elements graded by BS 1881-203 velocity bands: '
                         f'{summary}.')
            if below_good:
                builder.para(
                    f'{len(below_good)} element(s) '
                    f'({", ".join(below_good)}) fell below the "good" '
                    f'velocity band and should be reviewed in line with the '
                    f'recommendations of Section 6.0.'
                )
            else:
                builder.para('All tested elements fell within or above the '
                             '"good" velocity band at the time of test.')
        else:
            builder.para(
                'No ultrasonic pulse velocity results are available for this '
                'project; no conclusion on concrete quality can be drawn.'
            )

        # ------------------------------------------- INTEGRITY + SIGN-OFF
        builder.section('8.0', 'REPORT INTEGRITY AND SIGN-OFF')
        digest = cls._content_hash(
            ['ndt', project.id, sorted(str(t.id) for t in tests),
             len(tests),
             [round(cls._velocity(t), 6) if cls._velocity(t) is not None
              else None for t in tests],
             report_no])
        operators = []
        for t in tests:
            label = (t.operator_name
                     or (t.operator.get_full_name() or t.operator.email
                         if t.operator else None))
            if label and label not in operators:
                operators.append(label)
        user_label = 'Unauthenticated'
        if user and getattr(user, 'is_authenticated', False):
            user_label = user.get_full_name() or user.email
        builder.para(
            'This report was generated from verified platform records. Every '
            'figure is computed from live test records; the content digest '
            'below is a SHA-256 hash of the underlying test identifiers and '
            'results and can be used to detect any later alteration.'
        )
        builder.signoff_block([
            ('Report No', report_no),
            ('Tested by', ', '.join(operators) if operators
             else 'NOT RECORDED'),
            ('Approved by', f'{user_label} ' + '_' * 30),
            ('Content digest', digest),
            ('Date', f'{datetime.now():%Y-%m-%d %H:%M}'),
        ])

        # ------------------------------------------------------ APPENDIX
        builder.section('APPENDIX', 'PHOTOGRAPHS')
        shown = cls._render_appendix(builder, tests)
        if not shown:
            builder.para('No photographs recorded for these tests.')

        return builder.bytes()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _crack_depth(test):
        if test.crack_depth_mm is not None:
            return test.crack_depth_mm
        from apps.digital_eye.adapters import PUNDITAdapter
        return PUNDITAdapter.compute_crack_depth_mm(
            test.crack_path_length_mm, test.crack_pulse_time_us,
            test.uncracked_pulse_time_us)

    @staticmethod
    def _crack_remark(test):
        depth = NDTReportService._crack_depth(test)
        if depth is None:
            return 'NOT RECORDED'
        if depth > 25:
            return 'Depth exceeds 25 mm - structural review required'
        return 'Within monitoring limit'

    @staticmethod
    def _render_appendix(builder, tests):
        """Embed photographs attached to the tests; returns the count shown."""
        from .services import ReportService
        shown = 0
        seen = set()
        for t in tests:
            for f in t.files.all():
                if f.id in seen:
                    continue
                seen.add(f.id)
                url = ''
                try:
                    if f.file:
                        url = f.file.url
                except Exception:  # noqa: BLE001 — remote storage may raise
                    url = ''
                caption = (f.file_name or f.description
                           or f'Photograph {shown + 1}')
                ok = False
                if url:
                    ok = ReportService._try_embed_image(builder.pdf, url, w=90)
                if ok:
                    builder.para(
                        f'Photo {shown + 1}: {caption} '
                        f'({t.structural_element or "unspecified element"})')
                    shown += 1
        return shown
