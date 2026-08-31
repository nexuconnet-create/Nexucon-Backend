import logging
from apps.reports.models import QualityReport
from apps.scans.models import ScanSession, Defect, ThermalAnomaly, BIMAlignmentResult, ProgressValidationResult
try:
    from apps.storage.services import StorageService
except ImportError:
    StorageService = None
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DESIGN SYSTEM - "Technical / Blueprint" theme
# Brand colours kept (navy + cyan) but the entire chrome is redrawn to read
# like an engineering drawing set: hairline rulework, corner registration
# marks, drawing-number title blocks, dimension-style callouts, and
# monospace figures for anything numeric.
# ---------------------------------------------------------------------------
BRAND_NAVY = (2, 44, 79)
BRAND_BLUE = (3, 95, 180)
BRAND_CYAN = (0, 180, 216)
BRAND_DARK = (15, 24, 31)
WHITE = (255, 255, 255)
LIGHT_GREY = (245, 247, 250)
PAPER = (250, 251, 253)          # near-white "drawing sheet" background
GRID_LINE = (222, 232, 240)      # faint blueprint grid on light pages
GRID_LINE_DARK = (30, 50, 72)    # grid on the dark cover page
MID_GREY = (180, 190, 200)
DARK_GREY = (100, 116, 139)
HAIRLINE = (150, 170, 190)

SEV_CRITICAL = (200, 30, 30)
SEV_HIGH = (198, 100, 3)
SEV_MEDIUM = (172, 122, 5)
SEV_LOW = (21, 128, 61)

ALERT_RED_BG = (253, 235, 235)
ALERT_RED_TXT = (150, 25, 25)
ALERT_AMB_BG = (254, 246, 224)
ALERT_AMB_TXT = (120, 78, 5)
ALERT_GRN_BG = (231, 247, 237)
ALERT_GRN_TXT = (18, 90, 50)

SLA_DAYS = {'critical': 2, 'high': 7, 'medium': 10, 'low': 14}

# Which PDF sections each report template includes, beyond the always-present
# cover, quick summary (§1), final health check (§9) and sign-off (§10).
# None = every section (the full QA/QC drawing set).
TEMPLATE_SECTIONS = {
    'qaqc': None,
    'progress': {'progress'},
    'deviation': {'bim'},
    'earthworks': {'progress'},
    'compliance': set(),
}
ALL_SECTIONS = {'sitemap', 'defects', 'progress', 'thermal', 'bim', 'clash', 'recommendations'}
NIGERIAN_STANDARDS = {'crack': 'NIS 87 (Structural Concrete); Nigeria Building Code §5.3 - Crack Width Limits', 'concrete_crack': 'NIS 87 (Structural Concrete); Nigeria Building Code §5.3 - Crack Width Limits', 'spalling': 'NIS 87; SON ICS 91.080.40 - Durability of Reinforced Concrete', 'corrosion': 'NIS 439 - Steel for Reinforcement; Nigeria Building Code §7.6', 'thermal_anomaly': 'SON NIS 412 - Thermal Performance of Buildings', 'deformation': 'Nigeria Building Code §6.4 - Deflection Limits; NIS 87 Appendix B', 'delamination': 'NIS 87 §10 - Bond Strength; Nigeria Building Code §5.4', 'bim_deviation': 'ISO 19650 BIM Standards; FMB Nigeria Project Documentation Guidelines', 'general': 'Standards Organisation of Nigeria (SON); Nigeria National Building Code 2006'}
ACCOUNTABILITY = {'critical': 'Site Surveyor / Project Manager', 'high': 'Site Supervisor / Contractor', 'medium': 'Site Supervisor', 'low': 'Design Team / Site Supervisor'}
TRAFFIC_LIGHT = {'critical': ('CRITICAL', SEV_CRITICAL), 'high': ('URGENT', SEV_HIGH), 'medium': ('ROUTINE', SEV_MEDIUM), 'low': ('ROUTINE', SEV_LOW)}


class ReportService:
    """
    Business logic layer for Quality & Compliance QA/QC report generation.

    Visual language: an engineering "drawing set" rather than a corporate
    brochure - hairline grids, corner registration marks, a real title
    block on the cover, dimension-style callouts for every measurement, and
    monospaced (Courier) figures throughout so numbers read like they came
    off a survey instrument.

    PDF structure (per the client review document):
      1. Branded Cover Page (drawing-sheet title block)
      2. SITE HEALTH DASHBOARD - traffic-light quick summary (review §1 & §8)
      3. Scan Overview & Site Map of Issues (review §2)
      4. Defect Findings - Structured Action Cards (review §3)
      5. 3D Point Cloud & Progress Validation
      6. Thermal Analysis - Heat-Loss Map (review §2)
      7. BIM Comparison - Red-Alert for deviations > threshold (review §4)
      8. Clash Detection - Conflict Map (review §5)
      9. Engineering Recommendations - Prioritised To-Do with Accountability (review §6)
     10. Observations, Conclusions & Recommendations - detailed written narrative
     11. Final Site Health Check / Conclusion (review §7)
     12. Engineer Sign-Off
    """

    # ------------------------------------------------------------------
    # Low-level drafting primitives
    # ------------------------------------------------------------------

    @staticmethod
    def _severity_color(severity: str):
        sv = (severity or 'low').lower()
        return {'critical': SEV_CRITICAL, 'high': SEV_HIGH, 'medium': SEV_MEDIUM, 'low': SEV_LOW}.get(sv, SEV_LOW)

    @staticmethod
    def _plain_english(defect_type: str, severity: str) -> str:
        """Plain-English explanation of a defect for non-technical readers."""
        sv = (severity or 'low').lower()
        dt = (defect_type or 'defect').lower()
        urgency = {'critical': 'This is a serious safety concern. DO NOT proceed with construction until resolved.', 'high': 'This is significant and should be repaired as a matter of urgency.', 'medium': 'This requires attention to prevent further deterioration.', 'low': 'This is minor. Monitor it during the next routine site visit.'}.get(sv, 'A qualified engineer should investigate this issue.')
        explanations = {'crack': 'A thin split or break in the surface layer of the building. Small cracks can allow water to enter and slowly damage the steel reinforcement inside. If it grows wider than a pencil line, report it immediately.', 'concrete_crack': 'A thin split or break in the concrete surface. Small cracks can allow water to enter and corrode the steel reinforcing bars inside. Monitor it and report immediately if it widens.', 'spalling': 'Pieces of concrete are chipping or flaking off the surface. This typically happens when moisture causes the internal steel to rust and expand, pushing the concrete off.', 'corrosion': "Rust is forming on the steel reinforcing bars. As steel rusts it expands and can crack the surrounding concrete, significantly reducing the structure's strength.", 'thermal_anomaly': 'An unusual temperature difference detected by our thermal camera. This can signal hidden moisture, missing insulation, or gaps around windows and doors - like finding a draught without needing to feel it physically.', 'deformation': 'A structural element has moved or bent beyond acceptable limits. This could mean beams sagging, columns leaning, or foundations settling.', 'delamination': 'Different layers of material are separating from each other, similar to how layers of plywood peel apart. Creates a hollow, weakened section.'}
        base = explanations.get(dt, 'The AI has detected an anomaly in this structural element. A qualified engineer should inspect this area.')
        return f'{base} {urgency}'

    @staticmethod
    def _nigerian_standard(defect_type: str) -> str:
        dt = (defect_type or 'general').lower()
        return NIGERIAN_STANDARDS.get(dt, NIGERIAN_STANDARDS['general'])

    @staticmethod
    def _try_embed_image(pdf, img_url: str, w: float=80):
        if not img_url:
            return False
        try:
            if img_url.startswith('/media/'):
                from django.conf import settings
                import os
                rel = img_url.replace('/media/', '', 1)
                local = os.path.join(settings.MEDIA_ROOT, rel)
                if os.path.exists(local):
                    pdf.image(local, w=w)
                    return True
            elif img_url.startswith('http'):
                import urllib.request
                from io import BytesIO
                req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    pdf.image(BytesIO(r.read()), w=w)
                return True
        except Exception as exc:
            logger.debug('Image embed failed (%s): %s', img_url, exc)
        return False

    @staticmethod
    def _dashed_line(pdf, x1, y1, x2, y2, dash=1.6, gap=1.2, color=None, width=0.25):
        """Manual dashed line built from short `line()` segments (no reliance
        on any fpdf2 version having a native dashed_line method)."""
        import math
        if color is None:
            color = HAIRLINE
        pdf.set_draw_color(*color)
        pdf.set_line_width(width)
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            return
        step = dash + gap
        n = int(length // step) + 1
        ux = (x2 - x1) / length
        uy = (y2 - y1) / length
        pos = 0.0
        while pos < length:
            seg_end = min(pos + dash, length)
            pdf.line(x1 + ux * pos, y1 + uy * pos, x1 + ux * seg_end, y1 + uy * seg_end)
            pos += step

    @staticmethod
    def _tick_ruler(pdf, x, y, length, vertical=False, spacing=6, size=1.4, color=None):
        """A row of small graduation ticks, like the edge of a scale ruler."""
        if color is None:
            color = HAIRLINE
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.15)
        n = int(length // spacing)
        for i in range(n + 1):
            pos = i * spacing
            major = (i % 5 == 0)
            t = size * (1.8 if major else 1.0)
            if vertical:
                pdf.line(x, y + pos, x + t, y + pos)
            else:
                pdf.line(x + pos, y, x + pos, y + t)

    @staticmethod
    def _corner_ticks(pdf, x, y, w, h, size=3.2, color=None, width=0.35):
        """Four L-shaped registration marks at the corners of a rectangle -
        the classic printer's-crop-mark / drafting registration motif."""
        if color is None:
            color = BRAND_CYAN
        pdf.set_draw_color(*color)
        pdf.set_line_width(width)
        corners = [
            (x, y, 1, 1), (x + w, y, -1, 1),
            (x, y + h, 1, -1), (x + w, y + h, -1, -1),
        ]
        for cx, cy, dx, dy in corners:
            pdf.line(cx, cy, cx + size * dx, cy)
            pdf.line(cx, cy, cx, cy + size * dy)

    @staticmethod
    def _swatch(pdf, x, y, color, size=3.0):
        """Small filled + outlined square, legend-key style."""
        pdf.set_fill_color(*color)
        pdf.set_draw_color(*BRAND_DARK)
        pdf.set_line_width(0.2)
        pdf.rect(x, y, size, size, style='FD')

    @staticmethod
    def _status_tag(pdf, x, y, label, color, pad=2.2, font_size=8):
        """A bracket-style status tag, e.g. '[ PASS ]', in bold monospace."""
        pdf.set_font('courier', 'B', font_size)
        text = f'[ {label} ]'
        w = pdf.get_string_width(text) + pad * 2
        pdf.set_xy(x, y)
        pdf.set_text_color(*color)
        pdf.cell(w, 5.5, text)
        return w

    @classmethod
    def _blueprint_grid(cls, pdf, x, y, w, h, spacing=8, color=None, width=0.12):
        if color is None:
            color = GRID_LINE
        pdf.set_draw_color(*color)
        pdf.set_line_width(width)
        gx = x
        while gx <= x + w:
            pdf.line(gx, y, gx, y + h)
            gx += spacing
        gy = y
        while gy <= y + h:
            pdf.line(x, gy, x + w, gy)
            gy += spacing

    @classmethod
    def _section_heading(cls, pdf, number: str, title: str, color=None):
        """Section header styled as a drawing 'detail marker': a circled
        number-bubble on a hairline rule, followed by a bold uppercase
        title, with a tick-ruler running beneath the full width."""
        from fpdf.enums import XPos, YPos
        if color is None:
            color = BRAND_NAVY
        start_x = pdf.l_margin
        y = pdf.get_y()
        bubble_d = 8.5

        # bubble
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.5)
        pdf.set_fill_color(*color)
        pdf.ellipse(start_x, y, bubble_d, bubble_d, style='F')
        pdf.set_xy(start_x, y + 1.1)
        pdf.set_font('courier', 'B', 10)
        pdf.set_text_color(*WHITE)
        pdf.cell(bubble_d, bubble_d - 2, str(number), align='C')

        # leader rule
        rule_y = y + bubble_d / 2
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.5)
        pdf.line(start_x + bubble_d, rule_y, start_x + bubble_d + 4, rule_y)

        # title
        pdf.set_xy(start_x + bubble_d + 7, y + 0.3)
        pdf.set_font('helvetica', 'B', 12.5)
        pdf.set_text_color(*color)
        title_w = pdf.get_string_width(title.upper()) + 3
        pdf.cell(title_w, bubble_d, title.upper())

        # rule continues to the right margin
        content_right = pdf.w - pdf.r_margin
        pdf.line(start_x + bubble_d + 7 + title_w, rule_y, content_right, rule_y)

        pdf.set_y(y + bubble_d + 2)
        cls._tick_ruler(pdf, start_x, pdf.get_y(), content_right - start_x, spacing=6, size=1.3, color=MID_GREY)
        pdf.ln(4.5)
        pdf.set_text_color(*BRAND_DARK)
        pdf.set_draw_color(*BRAND_DARK)
        pdf.set_line_width(0.2)

    @classmethod
    def _subsection_bar(cls, pdf, title: str, color=None):
        """An outlined (not filled) bar with a small tick swatch - reads as
        a dimension-line label rather than a corporate banner."""
        from fpdf.enums import XPos, YPos
        if color is None:
            color = BRAND_BLUE
        x = pdf.l_margin
        y = pdf.get_y()
        w = pdf.w - pdf.l_margin - pdf.r_margin
        h = 6.5
        pdf.set_draw_color(*color)
        pdf.set_line_width(0.35)
        pdf.line(x, y, x + w, y)
        pdf.line(x, y + h, x + w, y + h)
        cls._swatch(pdf, x, y + h / 2 - 1.5, color, size=3)
        pdf.set_xy(x + 6, y + 1)
        pdf.set_font('courier', 'B', 8.5)
        pdf.set_text_color(*color)
        pdf.cell(0, h - 2, title.upper())
        pdf.set_y(y + h + 2.5)
        pdf.set_text_color(*BRAND_DARK)
        pdf.set_draw_color(*BRAND_DARK)
        pdf.set_line_width(0.2)

    @classmethod
    def _dim_row(cls, pdf, label, value, x=None, w_label=52, font_size=8.5, value_color=None, label_color=None):
        """A single 'dimension line' style label/value row - label in small
        caps helvetica, value in bold Courier (like a instrument readout)."""
        if x is None:
            x = pdf.l_margin
        if value_color is None:
            value_color = BRAND_NAVY
        if label_color is None:
            label_color = DARK_GREY
        pdf.set_x(x)
        pdf.set_font('helvetica', '', font_size)
        pdf.set_text_color(*label_color)
        pdf.cell(w_label, 5, label.upper())
        pdf.set_font('courier', 'B', font_size)
        pdf.set_text_color(*value_color)
        pdf.cell(0, 5, str(value))

    @classmethod
    def _drafting_frame(cls, pdf, margin_extra=3):
        """Draws a thin double-line 'drawing sheet' frame just inside the
        page edge with corner registration ticks - call once per page after
        header content, before the body begins, OR at end of page."""
        x = margin_extra
        y = margin_extra
        w = pdf.w - margin_extra * 2
        h = pdf.h - margin_extra * 2
        pdf.set_draw_color(*HAIRLINE)
        pdf.set_line_width(0.25)
        pdf.rect(x, y, w, h)
        cls._corner_ticks(pdf, x, y, w, h, size=3.5, color=BRAND_CYAN, width=0.4)

    # ------------------------------------------------------------------
    # Cover page - now a genuine drafting title-block sheet
    # ------------------------------------------------------------------

    @classmethod
    def _draw_cover_page(cls, pdf, scan, project, cover=None):
        from fpdf.enums import XPos, YPos
        from datetime import datetime
        cover = cover or {}
        W = pdf.w
        H = pdf.h

        # Deep blueprint background
        pdf.set_fill_color(8, 20, 34)
        pdf.rect(0, 0, W, H, style='F')

        # Fine blueprint grid (minor) + heavier grid (major) for depth
        pdf.set_draw_color(*GRID_LINE_DARK)
        pdf.set_line_width(0.12)
        gx = 0
        while gx <= W:
            pdf.line(gx, 0, gx, H)
            gx += 6
        gy = 0
        while gy <= H:
            pdf.line(0, gy, W, gy)
            gy += 6
        pdf.set_draw_color(40, 66, 92)
        pdf.set_line_width(0.25)
        gx = 0
        while gx <= W:
            pdf.line(gx, 0, gx, H)
            gx += 30
        gy = 0
        while gy <= H:
            pdf.line(0, gy, W, gy)
            gy += 30

        # Outer drafting frame with corner registration marks
        frame_m = 8
        pdf.set_draw_color(*BRAND_CYAN)
        pdf.set_line_width(0.5)
        pdf.rect(frame_m, frame_m, W - frame_m * 2, H - frame_m * 2)
        cls._corner_ticks(pdf, frame_m, frame_m, W - frame_m * 2, H - frame_m * 2, size=6, color=BRAND_CYAN, width=0.6)

        # Left index accent
        pdf.set_fill_color(*BRAND_BLUE)
        pdf.rect(0, 0, 4, H, style='F')

        content_x = frame_m + 14
        content_w = W - (frame_m + 14) * 2

        # Drawing number, top-right
        scan_ref = str(getattr(scan, 'scanner_id', '') or 'UNSET')[:12]
        date_ref = scan.created_at.strftime('%Y%m%d') if getattr(scan, 'created_at', None) else datetime.now().strftime('%Y%m%d')
        dwg_no = f'NXC-{date_ref}-{scan_ref}'
        pdf.set_xy(W - frame_m - 90, frame_m + 6)
        pdf.set_font('courier', 'B', 9)
        pdf.set_text_color(*BRAND_CYAN)
        pdf.cell(90 - 6, 5, f'DWG NO. {dwg_no}', align='R')
        pdf.set_xy(W - frame_m - 90, frame_m + 11)
        pdf.set_font('courier', '', 8)
        pdf.set_text_color(150, 175, 195)
        pdf.cell(90 - 6, 5, 'REV. A   SCALE: NTS', align='R')

        # Wordmark
        pdf.set_xy(content_x, 34)
        pdf.set_text_color(*WHITE)
        pdf.set_font('helvetica', 'B', 40)
        pdf.cell(0, 15, 'NEXUCON')

        pdf.set_xy(content_x, 51)
        pdf.set_font('courier', '', 9.5)
        pdf.set_text_color(*BRAND_CYAN)
        pdf.cell(0, 6, '/// ADVANCED ENGINEERING INTELLIGENCE & SITE SUPERVISION', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Tick ruler under wordmark
        pdf.set_y(60)
        cls._tick_ruler(pdf, content_x, pdf.get_y(), content_w, spacing=5, size=1.8, color=(60, 95, 125))
        pdf.ln(8)

        # Report title, in a thin outlined box (not filled) w/ corner ticks
        title_box_y = pdf.get_y()
        title_box_h = 13
        pdf.set_draw_color(*BRAND_CYAN)
        pdf.set_line_width(0.4)
        pdf.rect(content_x, title_box_y, content_w, title_box_h)
        cls._corner_ticks(pdf, content_x, title_box_y, content_w, title_box_h, size=3, color=BRAND_CYAN, width=0.35)
        pdf.set_xy(content_x + 5, title_box_y + 2.2)
        pdf.set_font('helvetica', 'B', 14)
        pdf.set_text_color(*WHITE)
        pdf.cell(content_w - 10, 9, 'STRUCTURAL SITE INSPECTION REPORT')

        pdf.set_y(title_box_y + title_box_h + 12)

        proj_name = cover.get('project_name') or (project.name if project and project.name else 'Site Survey Project')
        date_str = scan.created_at.strftime('%d %B %Y') if getattr(scan, 'created_at', None) else datetime.now().strftime('%d %B %Y')
        proj_num = cover.get('project_number') or (project.project_number if project and project.project_number else '-')
        client = cover.get('client_name') or (project.client_name if project and project.client_name else '-')
        address = cover.get('site_address') or (project.site_address if project and project.site_address else '-')
        contact = cover.get('client_contact') or (project.client_contact if project and project.client_contact else '-')

        pdf.set_x(content_x)
        pdf.set_font('helvetica', 'B', 25)
        pdf.set_text_color(*WHITE)
        pdf.multi_cell(content_w, 11, proj_name, align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

        # ---- Drafting title block: a real grid of labelled cells ----
        # Values wrap to their cell width: a long site address grows its
        # row instead of being cut off at 34 characters.
        tb_y = pdf.get_y()
        tb_x = content_x
        tb_w = content_w
        cols = [0.16, 0.34, 0.16, 0.34]  # relative widths for label/value/label/value
        rows = [
            ('CLIENT', client, 'PROJECT NO.', proj_num),
            ('SITE ADDRESS', address, 'REPORT DATE', date_str),
            ('CLIENT CONTACT', contact, 'SCANNER ID', str(getattr(scan, 'scanner_id', '-'))),
        ]
        cell_w = [tb_w * cols[i] for i in range(4)]
        c0 = tb_x
        c1 = c0 + cell_w[0]
        c2 = c1 + cell_w[1]
        c3 = c2 + cell_w[2]
        val_line_h = 3.6
        row_tops = []
        row_heights = []
        for (l1, v1, l2, v2) in rows:
            pdf.set_font('courier', '', 8.5)
            ln1 = pdf.multi_cell(cell_w[1] - 4, val_line_h, str(v1), dry_run=True, output='LINES')
            ln2 = pdf.multi_cell(cell_w[3] - 4, val_line_h, str(v2), dry_run=True, output='LINES')
            row_heights.append(max(9, max(len(ln1), len(ln2)) * val_line_h + 4))
            row_tops.append(tb_y + sum(row_heights[:-1]))
        tb_h = sum(row_heights)

        pdf.set_draw_color(*BRAND_CYAN)
        pdf.set_line_width(0.35)
        pdf.rect(tb_x, tb_y, tb_w, tb_h)
        for ry in row_tops[:-1]:
            pdf.line(tb_x, ry, tb_x + tb_w, ry)
        for cx in (c1, c2, c3):
            pdf.line(cx, tb_y, cx, tb_y + tb_h)

        for (l1, v1, l2, v2), ry in zip(rows, row_tops):
            for (lx, lw, label, value, is_label) in [
                (c0, cell_w[0], l1, None, True),
                (c1, cell_w[1], None, v1, False),
                (c2, cell_w[2], l2, None, True),
                (c3, cell_w[3], None, v2, False),
            ]:
                pdf.set_xy(lx + 2, ry + 1.6)
                if is_label:
                    pdf.set_font('helvetica', 'B', 7)
                    pdf.set_text_color(*BRAND_CYAN)
                    pdf.cell(lw - 4, 4, str(label))
                else:
                    pdf.set_font('courier', '', 8.5)
                    pdf.set_text_color(*WHITE)
                    pdf.multi_cell(lw - 4, val_line_h, str(value))

        pdf.set_y(tb_y + tb_h + 8)

        # ---- Required-field validation ----
        # A report must not ship with blank Client / Project No. / Client
        # Contact cells. Rather than silently printing "-", flag the
        # missing fields prominently so the operator completes the project
        # record before the report is issued.
        missing_fields = [
            label for label, v in [
                ('Client', client), ('Project No.', proj_num),
                ('Site Address', address), ('Client Contact', contact),
            ]
            if str(v).strip() in ('', '-', 'N/A', 'None')
        ]
        if missing_fields:
            pdf.set_draw_color(*SEV_HIGH)
            pdf.set_line_width(0.35)
            vb_y = pdf.get_y()
            pdf.rect(content_x, vb_y, content_w, 16)
            pdf.set_xy(content_x + 3, vb_y + 1.6)
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*SEV_HIGH)
            pdf.cell(content_w - 6, 4.5, 'VALIDATION WARNING - REPORT ISSUED WITH MISSING REQUIRED FIELDS')
            pdf.set_xy(content_x + 3, vb_y + 7)
            pdf.set_font('helvetica', '', 7.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(content_w - 6, 3.6, f"Missing: {', '.join(missing_fields)}. Complete the project record (client, project number, site address, client contact) and regenerate this report before issuing it to the client.")
            pdf.set_y(vb_y + 20)

        # Legend strip - severity key, drafted like a drawing legend
        legend_y = pdf.get_y()
        pdf.set_font('helvetica', 'B', 7.5)
        pdf.set_text_color(*BRAND_CYAN)
        pdf.set_xy(content_x, legend_y)
        pdf.cell(20, 5, 'LEGEND:')
        lx = content_x + 22
        for label, col in [('CRITICAL', SEV_CRITICAL), ('URGENT', SEV_HIGH), ('ROUTINE', SEV_MEDIUM), ('CLEAR', SEV_LOW)]:
            cls._swatch(pdf, lx, legend_y + 0.3, col, size=3.2)
            pdf.set_xy(lx + 4.5, legend_y)
            pdf.set_font('courier', '', 7.5)
            pdf.set_text_color(210, 220, 230)
            tw = pdf.get_string_width(label) + 8
            pdf.cell(tw, 5, label)
            lx += 4.5 + tw

        # Footer / confidentiality block
        pdf.set_y(H - 30)
        pdf.set_x(content_x)
        cls._dashed_line(pdf, content_x, pdf.get_y(), content_x + content_w, pdf.get_y(), color=(60, 95, 125))
        pdf.ln(3)
        pdf.set_x(content_x)
        pdf.set_font('courier', 'B', 8)
        pdf.set_text_color(*BRAND_CYAN)
        pdf.cell(0, 5, 'CONFIDENTIAL ENGINEERING DOCUMENT', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_x(content_x)
        pdf.set_font('helvetica', '', 7.2)
        pdf.set_text_color(120, 140, 160)
        pdf.multi_cell(content_w - 45, 3.8, 'This document and the information it contains are the exclusive property of Nexucon.net & SiteSupervise.tech. Prepared for the sole use of the named client; may not be reproduced or distributed without prior written consent.', align='L')
        pdf.set_x(content_x)
        pdf.set_font('courier', '', 7)
        pdf.set_text_color(120, 140, 160)
        pdf.cell(0, 4, 'COPYRIGHT 2026 NEXUCON.NET & SITESUPERVISE.TECH -- ALL RIGHTS RESERVED')

        # Version stamp, bottom-right, outlined bracket badge
        stamp_w = 42
        stamp_x = W - frame_m - 14 - stamp_w
        stamp_y = H - 25
        pdf.set_draw_color(*BRAND_CYAN)
        pdf.set_line_width(0.35)
        pdf.rect(stamp_x, stamp_y, stamp_w, 8)
        pdf.set_xy(stamp_x, stamp_y + 1.4)
        pdf.set_font('courier', 'B', 7.5)
        pdf.set_text_color(*WHITE)
        pdf.cell(stamp_w, 5, 'NEXUCON-AI v1.2', align='C')

    # ------------------------------------------------------------------
    # Image fetch / annotation (logic unchanged; marker style tightened
    # slightly to read as survey targets rather than soft pins)
    # ------------------------------------------------------------------

    @classmethod
    def _fetch_image_file(cls, scan, file_type):
        """Downloads or resolves a scan image (rgb/thermal) and returns the local file path."""
        import tempfile, urllib.request, os
        from django.conf import settings

        file_obj = scan.files.filter(file_type=file_type).first()
        if not file_obj:
            return None

        img_path = None
        # Stored URLs are presigned and expire ~1h after upload — always
        # re-sign before fetching, exactly like the rest of the pipeline.
        file_url = file_obj.fresh_url() or ''

        try:
            # Try local media path first
            if '127.0.0.1' in file_url or 'localhost' in file_url:
                import re
                m = re.search('/media/(.+)$', file_url)
                if m:
                    media_root = str(getattr(settings, 'MEDIA_ROOT', os.path.join(settings.BASE_DIR, 'media')))
                    local = os.path.join(media_root, m.group(1).replace('/', os.sep))
                    if os.path.exists(local):
                        img_path = local

            # Try direct local path
            if not img_path and file_url.startswith('/media/'):
                media_root = str(getattr(settings, 'MEDIA_ROOT', os.path.join(settings.BASE_DIR, 'media')))
                local = os.path.join(media_root, file_url.replace('/media/', '').replace('/', os.sep))
                if os.path.exists(local):
                    img_path = local

            # Download from Cloudinary
            if not img_path and file_url.startswith('http'):
                fd, tmp_p = tempfile.mkstemp(suffix='.jpg')
                os.close(fd)
                req = urllib.request.Request(file_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as r, open(tmp_p, 'wb') as f:
                    f.write(r.read())
                img_path = tmp_p
        except Exception as e:
            logger.warning('Failed to fetch %s image: %s', file_type, e)
            return None

        if not img_path or not os.path.exists(img_path):
            return None
        return img_path

    @classmethod
    def _annotate_image(cls, img_path, points, title=None):
        """
        Draws survey-style target markers on an image at the exact reported
        defect locations.

        Coordinate system: every point carries NORMALISED image coordinates
        (0.0-1.0, origin top-left) — either an optional 'bbox' rectangle
        (xmin, ymin, xmax, ymax) around the defect plus a reticle at its
        centre, or a plain 'x'/'y' centre point.

        Each point dict: {'x', 'y', 'bbox': (xmin,ymin,xmax,ymax) optional,
        'color': tuple, 'label': str, 'pin': bool, 'num': int}
        Returns: path to the annotated image, or None.
        """
        import tempfile, os
        from PIL import Image, ImageDraw, ImageFont

        try:
            img = Image.open(img_path).convert('RGBA')
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            w, h = img.size

            if not points:
                return None

            reticle = max(14, int(min(w, h) * 0.022))
            pin_r = max(13, int(min(w, h) * 0.030))
            try:
                num_font = ImageFont.load_default(size=max(11, int(pin_r * 0.95)))
            except TypeError:
                num_font = ImageFont.load_default()

            for p in points:
                bbox = p.get('bbox')
                if bbox:
                    bx0 = max(0.0, min(1.0, bbox[0]))
                    by0 = max(0.0, min(1.0, bbox[1]))
                    bx1 = max(0.0, min(1.0, bbox[2]))
                    by1 = max(0.0, min(1.0, bbox[3]))
                    nx = (bx0 + bx1) / 2.0
                    ny = (by0 + by1) / 2.0
                else:
                    nx = max(0.0, min(1.0, p.get('x', 0.5)))
                    ny = max(0.0, min(1.0, p.get('y', 0.5)))

                px = int(nx * w)
                py = int(ny * h)
                color = p['color']
                label = p.get('label', '')
                lw = max(2, reticle // 9)

                # Highlight rectangle around the exact defect region when the
                # AI reported a bounding box — this is what "points at the
                # exact place" on the report image.
                if bbox:
                    pad = int(reticle * 0.35)
                    x0 = max(0, int(bx0 * w) - pad)
                    y0 = max(0, int(by0 * h) - pad)
                    x1 = min(w, int(bx1 * w) + pad)
                    y1 = min(h, int(by1 * h) + pad)
                    # corner-bracket highlight (keeps the interior visible)
                    bl = max(10, reticle)  # bracket arm length
                    draw.line([(x0, y0), (min(x0 + bl, x1), y0)], fill=color, width=lw)
                    draw.line([(x0, y0), (x0, min(y0 + bl, y1))], fill=color, width=lw)
                    draw.line([(x1, y1), (max(x1 - bl, x0), y1)], fill=color, width=lw)
                    draw.line([(x1, y1), (x1, max(y1 - bl, y0))], fill=color, width=lw)

                # Square reticle (survey-target look) at the centre
                if p.get('pin'):
                    # Map pin: numbered circular head hovering above the exact
                    # spot, tapering tail touching it, and a dot on the spot.
                    cx = px
                    cy = py - int(pin_r * 1.45)
                    draw.polygon(
                        [(cx - int(pin_r * 0.34), cy + int(pin_r * 0.72)),
                         (cx + int(pin_r * 0.34), cy + int(pin_r * 0.72)),
                         (px, py)],
                        fill=color
                    )
                    draw.ellipse(
                        [cx - pin_r, cy - pin_r, cx + pin_r, cy + pin_r],
                        fill=color, outline=(255, 255, 255, 255) if sum(color[:3]) < 500 else (40, 40, 40, 255), width=lw
                    )
                    dot_r = max(2, reticle // 8)
                    draw.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r], fill=color)
                    num = p.get('num')
                    if num is not None:
                        num_str = str(num)
                        tb = draw.textbbox((0, 0), num_str, font=num_font)
                        draw.text(
                            (cx - (tb[2] - tb[0]) / 2 - tb[0], cy - (tb[3] - tb[1]) / 2 - tb[1]),
                            num_str, fill=(255, 255, 255, 255), font=num_font
                        )
                else:
                    draw.rectangle(
                        [px - reticle, py - reticle, px + reticle, py + reticle],
                        outline=color, width=lw
                    )
                    dot_r = max(2, reticle // 8)
                    draw.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r], fill=color)

                    # Crosshair ticks just outside the square
                    tick = reticle * 0.4
                    draw.line([(px - reticle - tick, py), (px - reticle, py)], fill=color, width=lw)
                    draw.line([(px + reticle, py), (px + reticle + tick, py)], fill=color, width=lw)
                    draw.line([(px, py - reticle - tick), (px, py - reticle)], fill=color, width=lw)
                    draw.line([(px, py + reticle), (px, py + reticle + tick)], fill=color, width=lw)

                if label:
                    try:
                        font = ImageFont.load_default()
                        bbox = draw.textbbox((0, 0), label, font=font)
                        text_w = bbox[2] - bbox[0]
                        text_h = bbox[3] - bbox[1]
                        pad_x, pad_y = 6, 4
                        callout_length = 30
                        bg_x = px + reticle + callout_length
                        bg_y = py - text_h // 2 - pad_y
                        draw.line([(px + reticle, py), (bg_x, py)], fill=color, width=lw)
                        draw.rectangle(
                            [bg_x, bg_y, bg_x + text_w + pad_x * 2, bg_y + text_h + pad_y * 2],
                            fill=(12, 22, 34, 235),
                            outline=color,
                            width=1
                        )
                        draw.text((bg_x + pad_x, bg_y + pad_y), label, fill=(255, 255, 255, 255), font=font)
                    except Exception:
                        draw.line([(px + reticle, py), (px + reticle + 20, py)], fill=color, width=lw)
                        draw.text((px + reticle + 24, py - 6), label, fill=(255, 255, 255, 255))

            img = Image.alpha_composite(img, overlay)
            final = img.convert('RGB')

            fd, out_path = tempfile.mkstemp(suffix='.png')
            os.close(fd)
            final.save(out_path, format='PNG')

            import tempfile as _tf
            if img_path.startswith(_tf.gettempdir()):
                os.remove(img_path)

            return out_path
        except Exception as e:
            logger.warning('Failed to annotate image: %s', e)
            return None

    @classmethod
    def _generate_annotated_sitemap(cls, scan, defects, anomalies, clashes):
        """
        Generates the "Where are the problems?" site map: numbered map pins
        (red/orange/yellow by severity) dropped at the exact defect locations
        on the RGB survey image. Only findings the AI located on the image (a
        stored image bounding box) are pinned — world-coordinate clash
        locations belong to the model, not the photograph, so they are never
        plotted onto it.
        """
        img_path = cls._fetch_image_file(scan, 'rgb')
        if not img_path:
            return None

        # Pin palette matches the legend printed beside the image in §2.
        pin_colors = {
            'critical': (222, 30, 38, 255),
            'high': (222, 30, 38, 255),
            'medium': (240, 140, 16, 255),
            'low': (243, 195, 25, 255),
        }

        points = []

        for i, d in enumerate(defects, 1):
            if d.bbox_xmin is None or d.bbox_ymin is None or d.bbox_xmax is None or d.bbox_ymax is None:
                continue
            dtype = getattr(d, 'type', '') or ''
            sev = (getattr(d, 'severity', '') or 'low').lower()
            points.append({
                'bbox': (d.bbox_xmin, d.bbox_ymin, d.bbox_xmax, d.bbox_ymax),
                'x': (d.bbox_xmin + d.bbox_xmax) / 2.0,
                'y': (d.bbox_ymin + d.bbox_ymax) / 2.0,
                'color': pin_colors.get(sev, pin_colors['medium']),
                'label': f"FINDING #{i}: {dtype.replace('_', ' ').title()} ({sev.upper()})",
                'pin': True,
                'num': i,
            })

        if not points:
            return None

        return cls._annotate_image(img_path, points)

    @classmethod
    def _generate_annotated_thermal(cls, scan, anomalies):
        """Generates an annotated thermal image with target markers at the
        exact anomaly locations (AI-reported image bounding boxes)."""
        img_path = cls._fetch_image_file(scan, 'thermal')
        if not img_path:
            return None

        points = []
        for i, a in enumerate(anomalies, 1):
            if a.bbox_xmin is None or a.bbox_ymin is None or a.bbox_xmax is None or a.bbox_ymax is None:
                continue
            variance = getattr(a, 'temperature_variance', 0) or 0
            sev = getattr(a, 'severity', 'low')
            points.append({
                'bbox': (a.bbox_xmin, a.bbox_ymin, a.bbox_xmax, a.bbox_ymax),
                'x': (a.bbox_xmin + a.bbox_xmax) / 2.0,
                'y': (a.bbox_ymin + a.bbox_ymax) / 2.0,
                'color': (210, 130, 10, 240) if sev != 'high' else (210, 40, 40, 240),
                'label': f'ANOMALY #{i}: {variance}C {str(sev).upper()}'
            })

        if not points:
            return None

        return cls._annotate_image(img_path, points)

    # ------------------------------------------------------------------
    # Main document builder
    # ------------------------------------------------------------------

    @classmethod
    def _build_fpdf_document(cls, scan, defects, anomalies, mean_dev, top_devs, recommendations, progress_val, clashes=None, overall_confidence=None, project=None, cover_overrides=None, include_sections=None):
        from fpdf import FPDF
        from fpdf.fonts import FontFace
        from fpdf.enums import XPos, YPos
        from datetime import datetime
        # Optional per-request cover overrides (client name, project number,
        # site address, contact) and a template-driven section filter.
        cover = {k: v for k, v in (cover_overrides or {}).items() if v}
        sections = include_sections if include_sections is not None else set(ALL_SECTIONS)
        # BIMAlignmentResult.mean_deviation is persisted in MILLIMETRES
        # (see run_bim_alignment / compute_deviations "mean_mm"), while this
        # report reasons in metres. Convert once, at the boundary, so every
        # threshold and label below is dimensionally correct — previously the
        # raw millimetre value was rendered as "M" and compared against metre
        # tolerances, turning a 140.4mm mean into a "140.40 m STOP WORK".
        if mean_dev is not None:
            mean_dev = float(mean_dev) / 1000.0
        proj_name = project.name if project and project.name else 'Site Survey'
        proj_num = project.project_number if project and project.project_number else 'N/A'
        client_name = project.client_name if project and project.client_name else 'N/A'
        date_str = scan.created_at.strftime('%d %b %Y %H:%M') if getattr(scan, 'created_at', None) else datetime.now().strftime('%d %b %Y %H:%M')
        scan_ref = str(getattr(scan, 'scanner_id', '') or 'UNSET')[:12]
        date_ref = scan.created_at.strftime('%Y%m%d') if getattr(scan, 'created_at', None) else datetime.now().strftime('%Y%m%d')
        dwg_no = f'NXC-{date_ref}-{scan_ref}'

        class QAQCPDF(FPDF):

            def header(self):
                if self.page_no() == 1:
                    return
                # Title-block style header: navy strip, cyan double rule,
                # drawing-number / sheet reference on the right.
                self.set_fill_color(*BRAND_NAVY)
                self.rect(0, 0, self.w, 15, style='F')
                self.set_draw_color(*BRAND_CYAN)
                self.set_line_width(0.6)
                self.line(0, 15, self.w, 15)
                self.set_draw_color(*BRAND_CYAN)
                self.set_line_width(0.15)
                self.line(0, 16.3, self.w, 16.3)

                self.set_text_color(*WHITE)
                self.set_font('courier', 'B', 8)
                self.set_xy(6, 3)
                self.cell(90, 8, 'NEXUCON // SITE INSPECTION REPORT')

                header_text = f'DWG {dwg_no}'
                if proj_num != 'N/A':
                    header_text += f'  |  PRJ {proj_num}'
                header_text += f'  |  SHEET {self.page_no():02d}/{{nb}}'
                self.set_font('courier', '', 7.5)
                self.set_xy(0, 3)
                self.cell(self.w - 6, 8, header_text, align='R')
                self.set_y(21)

            def footer(self):
                if self.page_no() == 1:
                    return
                self.set_y(-15)
                self.set_draw_color(*HAIRLINE)
                self.set_line_width(0.2)
                self.line(6, self.h - 15, self.w - 6, self.h - 15)
                ReportService._tick_ruler(self, 6, self.h - 15, self.w - 12, spacing=5, size=1.2, color=MID_GREY)
                self.set_text_color(*DARK_GREY)
                self.set_font('courier', '', 7)
                self.set_xy(6, self.h - 11)
                self.cell(0, 6, 'COPYRIGHT 2026 NEXUCON.NET & SITESUPERVISE.TECH -- CONFIDENTIAL')
                self.set_font('courier', 'B', 7)
                self.set_xy(-45, self.h - 11)
                self.cell(0, 6, f'SHEET {self.page_no():02d}/{{nb}}', align='R')

        pdf = QAQCPDF(orientation='L')
        pdf.alias_nb_pages()
        pdf.set_margins(15, 22, 15)
        pdf.set_auto_page_break(auto=True, margin=22)
        for d in defects:
            if not str(getattr(d, 'description', '')).strip():
                d.description = f"AI-detected {getattr(d, 'type', 'defect')} at ({getattr(d, 'location_x', 0) or 0:.2f}, {getattr(d, 'location_y', 0) or 0:.2f}, {getattr(d, 'location_z', 0) or 0:.2f})"
        for a in anomalies:
            if not str(getattr(a, 'description', '')).strip():
                a.description = f"Thermal anomaly - variance {getattr(a, 'temperature_variance', 0) or 0:.1f}°C at ({getattr(a, 'location_x', 0) or 0:.2f}, {getattr(a, 'location_y', 0) or 0:.2f}, {getattr(a, 'location_z', 0) or 0:.2f})"
        def_list = list(defects)
        ano_list = list(anomalies)
        has_critical = any((str(getattr(d, 'severity', '')).lower() == 'critical' for d in def_list))
        has_high = any((str(getattr(d, 'severity', '')).lower() == 'high' for d in def_list))
        critical_count = len([d for d in def_list if str(getattr(d, 'severity', '')).lower() == 'critical'])
        high_count = len([d for d in def_list if str(getattr(d, 'severity', '')).lower() == 'high'])
        bim_alert = mean_dev is not None and mean_dev >= 1.0
        # Sanity check: the mean is averaged over the same deviation
        # population as the per-point hotspots, so it can never exceed the
        # largest individual deviation. If it does — by a wide margin — the
        # scan and the BIM model are almost certainly referenced to
        # different coordinate frames (or a units/georeferencing transform
        # was skipped on ingest). That is a data error to flag, not a
        # structural finding, so suppress the STOP WORK alert.
        _top_vals_m = [
            float(t.get('deviation_mm')) / 1000.0
            for t in (top_devs or [])
            if isinstance(t, dict) and t.get('deviation_mm') is not None
        ]
        frame_mismatch = bool(
            mean_dev is not None and _top_vals_m
            and mean_dev > max(_top_vals_m) * 1.5
        )
        if frame_mismatch:
            bim_alert = False
        conf_pct = f'{overall_confidence * 100:.0f}%' if overall_confidence is not None else 'N/A'

        pdf.add_page()
        pdf.set_auto_page_break(auto=False)
        cls._draw_cover_page(pdf, scan, project, cover=cover)
        pdf.set_auto_page_break(auto=True, margin=22)
        pdf.add_page()

        # ================= SECTION 1 =================
        cls._section_heading(pdf, '1', 'Site Inspection - Quick Summary (Site Health Dashboard)')
        pdf.set_font('courier', '', 8.5)
        pdf.set_text_color(*DARK_GREY)
        pdf.cell(0, 5, f"PROJECT: {proj_name}   |   SITE: {getattr(project, 'address', '') or '-'}   |   DATE: {date_str}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

        if has_critical or bim_alert:
            ov_label, ov_color, ov_bg = 'FAILED - CRITICAL ISSUES REQUIRE IMMEDIATE ACTION', SEV_CRITICAL, ALERT_RED_BG
        elif has_high or ano_list:
            ov_label, ov_color, ov_bg = 'CONDITIONAL - URGENT ACTIONS REQUIRED', SEV_HIGH, ALERT_AMB_BG
        else:
            ov_label, ov_color, ov_bg = 'PASSED - STRUCTURE IS GENERALLY SOUND AND COMPLIANT', SEV_LOW, ALERT_GRN_BG

        ov_y = pdf.get_y()
        ov_w = pdf.w - pdf.l_margin - pdf.r_margin
        pdf.set_fill_color(*ov_bg)
        pdf.set_draw_color(*ov_color)
        pdf.set_line_width(0.5)
        pdf.rect(pdf.l_margin, ov_y, ov_w, 12, style='FD')
        cls._corner_ticks(pdf, pdf.l_margin, ov_y, ov_w, 12, size=3, color=ov_color, width=0.4)
        pdf.set_xy(pdf.l_margin + 5, ov_y + 1.5)
        pdf.set_font('courier', 'B', 8)
        pdf.set_text_color(*ov_color)
        pdf.cell(30, 9, 'STATUS')
        pdf.set_font('helvetica', 'B', 12)
        pdf.cell(0, 9, ov_label)
        pdf.set_y(ov_y + 15)

        pdf.set_font('courier', '', 8)
        pdf.set_text_color(*BRAND_DARK)
        cls._swatch(pdf, pdf.l_margin, pdf.get_y() + 0.5, BRAND_CYAN, size=3)
        pdf.set_x(pdf.l_margin + 5)
        pdf.cell(0, 5, f'AI CONFIDENCE LEVEL: {conf_pct} -- findings above this threshold are very likely to be real issues requiring attention.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

        cls._subsection_bar(pdf, 'Site Health Check - At A Glance')

        health_items = [('STRUCTURAL INTEGRITY', 'PASSED' if not has_critical else 'FAILED', SEV_LOW if not has_critical else SEV_CRITICAL, 'The main structure is sound. No major cracks or weaknesses found.' if not has_critical else f'{critical_count} critical structural issue(s) detected. Immediate action required.'), ('COMPLIANCE WITH DESIGN (BIM)', 'DATA ERROR' if frame_mismatch else 'FAILED - CRITICAL' if bim_alert else 'PASSED' if mean_dev is None else 'PASSED', SEV_HIGH if frame_mismatch else SEV_CRITICAL if bim_alert else SEV_LOW, f'Mean deviation ({mean_dev:.2f} m) exceeds every individual measured deviation point, which is geometrically impossible for a consistent comparison. Likely coordinate-system/georeferencing error - do not treat as a structural finding.' if frame_mismatch else f'The building is NOT in the correct position - deviation of {mean_dev:.2f} m detected. This is a VERY SERIOUS PROBLEM. All future measurements will be wrong until corrected.' if bim_alert else 'Building position is within acceptable tolerance.'), ('BUILDING ENVELOPE (Thermal)', 'CONDITIONAL' if ano_list else 'PASSED', SEV_HIGH if ano_list else SEV_LOW, f'{len(ano_list)} thermal anomaly/anomalies found - potential heat loss or moisture. Check roof insulation and seal any gaps around windows/doors.' if ano_list else 'No thermal anomalies detected.'), ('3D MODEL CONFLICTS (Clashes)', 'FOUND' if clashes else 'CLEAR', SEV_MEDIUM if clashes else SEV_LOW, f'{len(clashes)} design conflict(s) found. These are planning issues - fix before construction reaches those floors.' if clashes else 'No design conflicts detected in the BIM model.')]

        card_w = pdf.w - pdf.l_margin - pdf.r_margin
        for chk_label, chk_status, chk_color, chk_desc in health_items:
            if pdf.get_y() + 20 > pdf.page_break_trigger:
                pdf.add_page()
            pdf.set_auto_page_break(auto=False)
            start_y = pdf.get_y()
            card_h = 15
            pdf.set_draw_color(*HAIRLINE)
            pdf.set_line_width(0.25)
            pdf.rect(pdf.l_margin, start_y, card_w, card_h)
            cls._swatch(pdf, pdf.l_margin + 2, start_y + card_h / 2 - 1.6, chk_color, size=3.2)

            pdf.set_xy(pdf.l_margin + 8, start_y + 2)
            pdf.set_font('helvetica', 'B', 8.5)
            pdf.set_text_color(*BRAND_NAVY)
            pdf.cell(78, 5, chk_label)

            cls._status_tag(pdf, pdf.l_margin + 8, start_y + 8, chk_status, chk_color, font_size=7.5)

            pdf.set_xy(pdf.l_margin + 90, start_y + 2)
            pdf.set_font('helvetica', '', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(card_w - 95, 4.6, chk_desc)

            pdf.set_y(start_y + card_h + 2.5)
            pdf.set_auto_page_break(auto=True, margin=22)

        pdf.ln(3)
        if pdf.get_y() + 15 > pdf.page_break_trigger:
            pdf.add_page()
        pdf.set_draw_color(*SEV_CRITICAL)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(1.5)
        pdf.set_font('courier', 'B', 9)
        pdf.set_text_color(*SEV_CRITICAL)
        pdf.cell(0, 6, '>> PRIORITY ACTIONS REQUIRED', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)
        pdf.set_text_color(*BRAND_DARK)
        action_num = 1
        if bim_alert:
            if pdf.get_y() + 16 > pdf.page_break_trigger:
                pdf.add_page()
            pdf.set_fill_color(*ALERT_RED_BG)
            pdf.set_draw_color(*SEV_CRITICAL)
            pdf.set_line_width(0.25)
            box_y = pdf.get_y()
            pdf.rect(pdf.l_margin, box_y, card_w, 12, style='FD')
            pdf.set_xy(pdf.l_margin + 3, box_y + 1.3)
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*ALERT_RED_TXT)
            pdf.cell(0, 5, f'{action_num}. URGENT: BUILDING POSITION ERROR -- {mean_dev:.2f} m SHIFT DETECTED', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(pdf.l_margin + 3, box_y + 6.3)
            pdf.set_font('helvetica', '', 7.5)
            pdf.cell(0, 5, 'ACTION: Surveyor must re-check site coordinates IMMEDIATELY. Do NOT pour any more concrete until resolved.')
            pdf.set_y(box_y + 14)
            pdf.set_text_color(*BRAND_DARK)
            action_num += 1
        if ano_list:
            if pdf.get_y() + 16 > pdf.page_break_trigger:
                pdf.add_page()
            top_ano = ano_list[0]
            variance = getattr(top_ano, 'temperature_variance', 0) or 0
            pdf.set_fill_color(*ALERT_AMB_BG)
            pdf.set_draw_color(*SEV_HIGH)
            box_y = pdf.get_y()
            pdf.rect(pdf.l_margin, box_y, card_w, 12, style='FD')
            pdf.set_xy(pdf.l_margin + 3, box_y + 1.3)
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*ALERT_AMB_TXT)
            pdf.cell(0, 5, f'{action_num}. URGENT: THERMAL LOSS -- {variance:.1f} DEG C ANOMALY DETECTED', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(pdf.l_margin + 3, box_y + 6.3)
            pdf.set_font('helvetica', '', 7.5)
            pdf.cell(0, 5, 'ACTION: Check roof insulation and seal any gaps around windows/doors.')
            pdf.set_y(box_y + 14)
            pdf.set_text_color(*BRAND_DARK)
            action_num += 1
        if def_list:
            low_defs = [d for d in def_list if str(getattr(d, 'severity', '')).lower() in ('low', 'medium')]
            if low_defs:
                if pdf.get_y() + 16 > pdf.page_break_trigger:
                    pdf.add_page()
                d = low_defs[0]
                pdf.set_fill_color(*ALERT_GRN_BG)
                pdf.set_draw_color(*SEV_LOW)
                box_y = pdf.get_y()
                pdf.rect(pdf.l_margin, box_y, card_w, 12, style='FD')
                pdf.set_xy(pdf.l_margin + 3, box_y + 1.3)
                pdf.set_font('courier', 'B', 8)
                pdf.set_text_color(*ALERT_GRN_TXT)
                pdf.cell(0, 5, f"{action_num}. ROUTINE: NON-STRUCTURAL {getattr(d, 'type', 'DEFECT').upper()} ON SURFACE", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_xy(pdf.l_margin + 3, box_y + 6.3)
                pdf.set_font('helvetica', '', 7.5)
                pdf.cell(0, 5, 'ACTION: Monitor at next site visit. Mark with pencil and note if it grows.')
                pdf.set_y(box_y + 14)
                pdf.set_text_color(*BRAND_DARK)
                action_num += 1
        if clashes:
            if pdf.get_y() + 16 > pdf.page_break_trigger:
                pdf.add_page()
            pdf.set_fill_color(*LIGHT_GREY)
            pdf.set_draw_color(*SEV_MEDIUM)
            box_y = pdf.get_y()
            pdf.rect(pdf.l_margin, box_y, card_w, 12, style='FD')
            pdf.set_xy(pdf.l_margin + 3, box_y + 1.3)
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*BRAND_DARK)
            pdf.cell(0, 5, f'{action_num}. PLANNING: {len(clashes)} DESIGN CONFLICT(S) IN BIM MODEL', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(pdf.l_margin + 3, box_y + 6.3)
            pdf.set_font('helvetica', '', 7.5)
            pdf.cell(0, 5, 'ACTION: Design team must review and resolve before construction reaches those floors.')
            pdf.set_y(box_y + 14)
            action_num += 1

        pdf.ln(3)
        if pdf.get_y() + 30 > pdf.page_break_trigger:
            pdf.add_page()
        cls._subsection_bar(pdf, 'How To Use This Report')
        pdf.set_font('helvetica', '', 8)
        pdf.set_text_color(*DARK_GREY)
        roles = [('PROJECT MANAGER', 'Traffic-light system and a prioritised To-Do list you can hand directly to the site supervisor.'), ('SITE SUPERVISOR', 'Plain-language instructions for immediate on-site action and a map showing exactly where problems are.'), ('EXECUTIVE', 'A crystal-clear PASS/FAIL rating for the entire project - no engineering knowledge required.')]
        for role, desc in roles:
            pdf.set_text_color(*BRAND_BLUE)
            pdf.set_font('courier', 'B', 7.5)
            pdf.cell(48, 5, f'{role}:')
            pdf.set_font('helvetica', '', 8)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, desc, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 2 =================
        if 'sitemap' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '2', 'Where are the problems? (Site Map)')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, 'The image below shows the view of your building from our survey drone. The colored pins mark the exact location of the issues we found - the number on each pin matches the FINDING # in Section 3, and the dotted brackets outline the precise affected region:', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
            # Pin legend — severity-coloured, with the real count per level
            pin_levels = [
                ('RED PIN', (222, 30, 38), 'Urgent Problem', [d for d in defects if str(getattr(d, 'severity', '')).lower() in ('high', 'critical')]),
                ('ORANGE PIN', (240, 140, 16), 'High Concern', [d for d in defects if str(getattr(d, 'severity', '')).lower() == 'medium']),
                ('YELLOW PIN', (243, 195, 25), 'Routine Check', [d for d in defects if str(getattr(d, 'severity', '')).lower() == 'low']),
            ]
            for lab, col, desc, items in pin_levels:
                cls._swatch(pdf, pdf.l_margin, pdf.get_y() + 1, col, size=3)
                pdf.set_x(pdf.l_margin + 5)
                pdf.set_font('courier', 'B', 7.5)
                pdf.set_text_color(*BRAND_DARK)
                pdf.cell(22, 5, lab)
                pdf.set_font('helvetica', '', 8)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 5, f'= {desc} ({len(items)} found in this scan)', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

            annotated_img_path = cls._generate_annotated_sitemap(scan, defects, anomalies, clashes or [])
            if annotated_img_path and __import__('os').path.exists(annotated_img_path):
                try:
                    from PIL import Image as _PILImage
                    with _PILImage.open(annotated_img_path) as _i:
                        _iw, _ih = _i.size
                    _ratio = _iw / _ih if _ih > 0 else 1
                    _img_w = 175
                    _img_h = _img_w / _ratio
                    if _img_h > 130:
                        _img_h = 130
                        _img_w = _img_h * _ratio
                    _img_x = pdf.l_margin + ((pdf.w - pdf.l_margin - pdf.r_margin) - _img_w) / 2
                    if pdf.get_y() + _img_h + 12 > pdf.page_break_trigger:
                        pdf.add_page()
                    _img_y = pdf.get_y()
                    pdf.set_draw_color(*BRAND_NAVY)
                    pdf.set_line_width(0.4)
                    pdf.rect(_img_x - 1.5, _img_y - 1.5, _img_w + 3, _img_h + 3)
                    cls._corner_ticks(pdf, _img_x - 1.5, _img_y - 1.5, _img_w + 3, _img_h + 3, size=4, color=BRAND_CYAN, width=0.4)
                    pdf.image(annotated_img_path, x=_img_x, y=_img_y, w=_img_w, h=_img_h)
                    pdf.ln(_img_h + 6)
                except Exception as e:
                    logger.warning('Failed to add annotated image to PDF: %s', e)
                finally:
                    __import__('os').remove(annotated_img_path)

            pdf.set_text_color(*BRAND_DARK)
            sensors = getattr(scan, 'sensors_used', []) or []
            sensors_str = ', '.join(sensors) if sensors else 'LiDAR, RGB, Thermal (default)'
            cls._subsection_bar(pdf, 'Scan Parameters')
            for label, value in [('Scanner ID', str(getattr(scan, 'scanner_id', '-'))), ('Sensors Used', sensors_str), ('Scan Date', date_str), ('Status', str(getattr(scan, 'status', '-')).upper())]:
                cls._dim_row(pdf, label, value)
                pdf.ln(5.2)
            pdf.ln(2)
            try:
                lidar_files = list(scan.files.filter(file_type='lidar').values('file_url', 'file_name'))
                rgb_files = list(scan.files.filter(file_type='rgb').values('file_url', 'file_name'))
                if lidar_files:
                    pdf.set_font('courier', 'B', 8)
                    pdf.set_text_color(*BRAND_BLUE)
                    pdf.cell(0, 6, f'LIDAR DATA FILES ({len(lidar_files)} REGISTERED):', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_font('courier', '', 7.5)
                    pdf.set_text_color(*DARK_GREY)
                    for f in lidar_files:
                        pdf.cell(0, 5, f"  - {f.get('file_name', 'lidar_file')}  -  {f.get('file_url', '')[:85]}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(3)
                if rgb_files:
                    pdf.set_font('courier', 'B', 8)
                    pdf.set_text_color(*BRAND_BLUE)
                    pdf.cell(0, 6, f'RGB IMAGE FILES ({len(rgb_files)} REGISTERED):', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_font('courier', '', 7.5)
                    pdf.set_text_color(*DARK_GREY)
                    for f in rgb_files:
                        pdf.cell(0, 5, f"  - {f.get('file_name', 'rgb_file')}  -  {f.get('file_url', '')[:85]}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(3)
            except Exception as e:
                logger.debug('Could not list scan files: %s', e)
            pdf.set_text_color(*BRAND_DARK)
            site_images = []
            rgb_url = getattr(scan, 'rgb_url', None)
            thermal_url = getattr(scan, 'thermal_url', None)
            if rgb_url:
                site_images.append(('RGB Site Image', rgb_url))
            if thermal_url:
                site_images.append(('Thermal Overlay', thermal_url))
            if site_images:
                for label, url in site_images:
                    if pdf.get_y() + 80 > pdf.page_break_trigger:
                        pdf.add_page()
                    pdf.set_font('courier', 'B', 8)
                    pdf.cell(0, 6, label.upper() + ':', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    embedded = cls._try_embed_image(pdf, url, w=100)
                    if not embedded:
                        pdf.set_font('helvetica', 'I', 7.5)
                        pdf.set_text_color(*DARK_GREY)
                        pdf.cell(0, 5, f'View online: {url}', link=url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                        pdf.set_text_color(*BRAND_DARK)
                    pdf.ln(3)
            else:
                pdf.set_font('helvetica', 'I', 8.5)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 6, 'No site images are linked to this scan session.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 3 =================
        if 'defects' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '3', 'Defect Findings & Evidence - Structured Action Cards')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, 'Each finding below is a drafted detail callout: what the problem is, where it is, how serious it is, what to do, and who is responsible. All findings reference relevant Nigerian Standards (NIS/SON) and the National Building Code.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(*BRAND_DARK)
            if not def_list:
                pdf.set_draw_color(*SEV_LOW)
                pdf.set_fill_color(*ALERT_GRN_BG)
                pdf.set_line_width(0.3)
                pdf.rect(pdf.l_margin, pdf.get_y(), card_w, 10, style='FD')
                pdf.set_xy(pdf.l_margin + 4, pdf.get_y() + 2)
                pdf.set_font('courier', 'B', 9)
                pdf.set_text_color(*ALERT_GRN_TXT)
                pdf.cell(0, 6, 'NO VISUAL DEFECTS DETECTED IN THIS SCAN.')
                pdf.set_text_color(*BRAND_DARK)
                pdf.ln(12)
            else:
                for i, d in enumerate(def_list, 1):
                    sev = str(getattr(d, 'severity', 'low')).lower()
                    sev_color = cls._severity_color(sev)
                    dtype = str(getattr(d, 'type', 'defect'))
                    loc_x = getattr(d, 'location_x', 0) or 0
                    loc_y = getattr(d, 'location_y', 0) or 0
                    loc_z = getattr(d, 'location_z', 0) or 0
                    zone = getattr(d, 'grid_zone', '') or ''
                    level = getattr(d, 'room_level', '') or ''
                    conf_raw = getattr(d, 'confidence_score', None)
                    conf_text = f'{conf_raw * 100:.0f}%' if conf_raw is not None else 'N/A'
                    desc = str(getattr(d, 'description', '')).encode('ascii', 'ignore').decode('ascii')
                    sla = SLA_DAYS.get(sev, 14)
                    tl_label, _ = TRAFFIC_LIGHT.get(sev, ('ROUTINE', SEV_LOW))
                    acct = ACCOUNTABILITY.get(sev, 'Site Supervisor')
                    std = cls._nigerian_standard(dtype)

                    plain = cls._plain_english(dtype, sev)
                    # Full text, wrapped — the card grows to fit the whole
                    # assessment and recommended action instead of slicing
                    # them mid-sentence. Measure the wrapped line counts
                    # first (dry run) so the callout border encloses all of
                    # the text.
                    desc_x = pdf.l_margin + 85
                    desc_w = card_w - 92
                    pdf.set_font('helvetica', '', 8)
                    desc_lines = pdf.multi_cell(desc_w, 3.8, desc if desc else f'AI-detected {dtype} in this area.', dry_run=True, output='LINES')
                    pdf.set_font('helvetica', 'I', 7.3)
                    plain_lines = pdf.multi_cell(desc_w - 2.5, 3.4, plain, dry_run=True, output='LINES')
                    desc_h = len(desc_lines) * 3.8
                    rec_h = len(plain_lines) * 3.4 + 3.8
                    # Grow the callout card so the assessment, the action
                    # block AND the verified-by-AI footer strip all fit —
                    # the second term covers the content_y+20 floor on
                    # rec_y when the description itself is short.
                    card_h = max(52, 31 + desc_h + rec_h, 44.5 + rec_h)

                    if pdf.get_y() + card_h + 8 > pdf.page_break_trigger:
                        pdf.add_page()
                    pdf.set_auto_page_break(auto=False)
                    start_y = pdf.get_y()

                    # Dashed detail-callout border (technical, not corporate)
                    cls._dashed_line(pdf, pdf.l_margin, start_y, pdf.l_margin + card_w, start_y, color=sev_color, width=0.3, dash=2.2, gap=1.4)
                    cls._dashed_line(pdf, pdf.l_margin, start_y + card_h, pdf.l_margin + card_w, start_y + card_h, color=sev_color, width=0.3, dash=2.2, gap=1.4)
                    cls._dashed_line(pdf, pdf.l_margin, start_y, pdf.l_margin, start_y + card_h, color=sev_color, width=0.3, dash=2.2, gap=1.4)
                    cls._dashed_line(pdf, pdf.l_margin + card_w, start_y, pdf.l_margin + card_w, start_y + card_h, color=sev_color, width=0.3, dash=2.2, gap=1.4)

                    # Finding number bubble (drawing balloon reference)
                    bubble_d = 8
                    pdf.set_fill_color(*sev_color)
                    pdf.ellipse(pdf.l_margin - bubble_d / 2, start_y - bubble_d / 2, bubble_d, bubble_d, style='F')
                    pdf.set_xy(pdf.l_margin - bubble_d / 2, start_y - bubble_d / 2 + 1.1)
                    pdf.set_font('courier', 'B', 8)
                    pdf.set_text_color(*WHITE)
                    pdf.cell(bubble_d, bubble_d - 2, str(i), align='C')

                    pdf.set_xy(pdf.l_margin + 8, start_y + 2)
                    pdf.set_font('helvetica', 'B', 10)
                    pdf.set_text_color(*BRAND_DARK)
                    type_display = dtype.replace('_', ' ').upper()
                    pdf.cell(100, 6, f'FINDING #{i}: {type_display}')

                    cls._status_tag(pdf, pdf.l_margin + card_w - 46, start_y + 2, f'{tl_label}', sev_color, font_size=7.5)

                    pdf.set_draw_color(*HAIRLINE)
                    pdf.set_line_width(0.15)
                    pdf.line(pdf.l_margin + 4, start_y + 9, pdf.l_margin + card_w - 4, start_y + 9)

                    content_y = start_y + 12
                    pdf.set_xy(pdf.l_margin + 8, content_y)
                    cls._dim_row(pdf, 'SLA', f'{sla} DAYS', x=pdf.l_margin + 8, w_label=26, font_size=7)
                    pdf.set_xy(pdf.l_margin + 8, content_y + 5)
                    cls._dim_row(pdf, 'RESP.', acct, x=pdf.l_margin + 8, w_label=26, font_size=7)
                    pdf.set_xy(pdf.l_margin + 8, content_y + 10)
                    cls._dim_row(pdf, 'AI CONF.', conf_text, x=pdf.l_margin + 8, w_label=26, font_size=7)
                    pdf.set_xy(pdf.l_margin + 8, content_y + 15)
                    loc_str = f'X{loc_x:.2f} Y{loc_y:.2f} Z{loc_z:.2f}'
                    if zone or level:
                        loc_str += f' {zone}{level}'.strip()
                    cls._dim_row(pdf, 'LOC.', loc_str, x=pdf.l_margin + 8, w_label=26, font_size=7)
                    pdf.set_xy(pdf.l_margin + 8, content_y + 20)
                    first_seen = d.created_at.strftime('%Y-%m-%d') if getattr(d, 'created_at', None) else 'this scan'
                    cls._dim_row(pdf, 'FIRST OBS.', first_seen, x=pdf.l_margin + 8, w_label=26, font_size=7)

                    pdf.set_xy(desc_x, content_y)
                    pdf.set_font('courier', 'B', 7.5)
                    pdf.set_text_color(*BRAND_DARK)
                    pdf.cell(desc_w, 4, 'ENGINEERING ASSESSMENT')
                    pdf.set_xy(desc_x, content_y + 5)
                    pdf.set_font('helvetica', '', 8)
                    pdf.set_text_color(*DARK_GREY)
                    pdf.multi_cell(desc_w, 3.8, desc if desc else f'AI-detected {dtype} in this area.')

                    rec_y = max(pdf.get_y() + 1.5, content_y + 20)
                    pdf.set_draw_color(*BRAND_CYAN)
                    pdf.set_line_width(0.2)
                    pdf.line(desc_x, rec_y, desc_x, rec_y + 11)
                    pdf.set_xy(desc_x + 2.5, rec_y)
                    pdf.set_font('courier', 'B', 7)
                    pdf.set_text_color(*BRAND_BLUE)
                    pdf.cell(desc_w - 2.5, 3.5, 'RECOMMENDED ACTION')
                    pdf.set_xy(desc_x + 2.5, rec_y + 3.8)
                    pdf.set_font('helvetica', 'I', 7.3)
                    pdf.set_text_color(*DARK_GREY)
                    pdf.multi_cell(desc_w - 2.5, 3.4, plain)

                    # Verified-by-AI footer strip — states plainly that the
                    # finding is machine-confirmed visual evidence, not a
                    # heuristic guess, and cites the confidence again.
                    pdf.set_xy(pdf.l_margin + 8, start_y + card_h - 8.5)
                    pdf.set_font('courier', 'B', 7)
                    pdf.set_text_color(*SEV_LOW if sev == 'low' else BRAND_BLUE)
                    pdf.cell(desc_w, 4, f'VERIFIED BY AI: machine-confirmed visual finding ({conf_text} confidence) - distinguished from shadow, stain or paint line by the vision model.')

                    pdf.set_y(start_y + card_h + 5)
                    pdf.set_auto_page_break(auto=True, margin=22)

        # ================= SECTION 4 =================
        if 'progress' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(4)
            cls._section_heading(pdf, '4', '3D Point Cloud & Progress Validation')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, 'LiDAR point cloud data creates a precise 3D map of the site at the time of scanning, compared against the approved BIM design model to calculate the percentage of planned construction physically completed.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(*BRAND_DARK)
            if progress_val:
                area = progress_val.covered_area_sqm or 0.0
                score = progress_val.progress_score or 0.0
                score_pct = score * 100
                bar_w = 200
                bar_h = 10
                bar_x = pdf.l_margin
                bar_y = pdf.get_y()
                fill_w = bar_w * min(1.0, score)
                bar_color = SEV_LOW if score_pct >= 80 else SEV_MEDIUM if score_pct >= 50 else SEV_HIGH
                # outlined gauge with tick graduations rather than a soft filled bar
                pdf.set_draw_color(*BRAND_DARK)
                pdf.set_line_width(0.3)
                pdf.rect(bar_x, bar_y, bar_w, bar_h)
                pdf.set_fill_color(*bar_color)
                pdf.rect(bar_x, bar_y, fill_w, bar_h, style='F')
                pdf.set_draw_color(*BRAND_DARK)
                pdf.set_line_width(0.3)
                pdf.rect(bar_x, bar_y, bar_w, bar_h)
                for pct in (25, 50, 75):
                    tx = bar_x + bar_w * pct / 100.0
                    pdf.set_draw_color(*WHITE)
                    pdf.set_line_width(0.25)
                    pdf.line(tx, bar_y, tx, bar_y + bar_h)
                pdf.set_xy(bar_x, bar_y + 1)
                pdf.set_font('courier', 'B', 9)
                pdf.set_text_color(*WHITE) if score_pct >= 30 else pdf.set_text_color(*BRAND_DARK)
                pdf.cell(bar_w, bar_h - 2, f'CONSTRUCTION PROGRESS: {score_pct:g}%', align='C')
                pdf.set_y(bar_y + bar_h + 4)
                pdf.set_text_color(*BRAND_DARK)
                cls._dim_row(pdf, 'Mapped Area', f'{area:g} m²')
                pdf.ln(5.2)
                cls._dim_row(pdf, 'Progress Score', f'{score_pct:g}%')
                pdf.ln(5.2)
                cls._dim_row(pdf, '3D Visualisation', 'AVAILABLE IN NEXUCON WEB DASHBOARD')
                pdf.ln(5.2)
            else:
                pdf.set_font('helvetica', 'I', 8.5)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 8, 'No Point Cloud progress metrics are available for this scan session.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 5 =================
        if 'thermal' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '5', 'Thermal Analysis - Heat-Loss Map')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, 'Thermal imaging detects heat differences across surfaces - like finding a draught without needing to feel it physically. Unusual temperature patterns often indicate hidden moisture, missing insulation, gaps around openings, or electrical hotspots. Applicable standard: SON NIS 412.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(*BRAND_DARK)

            if not ano_list:
                pdf.set_draw_color(*SEV_LOW)
                pdf.set_fill_color(*ALERT_GRN_BG)
                pdf.set_line_width(0.3)
                pdf.rect(pdf.l_margin, pdf.get_y(), card_w, 10, style='FD')
                pdf.set_xy(pdf.l_margin + 4, pdf.get_y() + 2)
                pdf.set_font('courier', 'B', 9)
                pdf.set_text_color(*ALERT_GRN_TXT)
                pdf.cell(0, 6, 'NO THERMAL ANOMALIES DETECTED IN THIS SCAN.')
                pdf.set_text_color(*BRAND_DARK)
                pdf.ln(12)
            else:
                # Annotated thermal image: every anomaly bracketed at its
                # exact location on the thermal photo itself.
                thermal_annotated = cls._generate_annotated_thermal(scan, ano_list)
                if thermal_annotated and __import__('os').path.exists(thermal_annotated):
                    try:
                        from PIL import Image as _PILImage
                        with _PILImage.open(thermal_annotated) as _i:
                            _iw, _ih = _i.size
                        _ratio = _iw / _ih if _ih > 0 else 1
                        _img_w = 165
                        _img_h = _img_w / _ratio
                        if _img_h > 110:
                            _img_h = 110
                            _img_w = _img_h * _ratio
                        _img_x = pdf.l_margin + ((pdf.w - pdf.l_margin - pdf.r_margin) - _img_w) / 2
                        if pdf.get_y() + _img_h + 12 > pdf.page_break_trigger:
                            pdf.add_page()
                        _img_y = pdf.get_y()
                        pdf.set_draw_color(*BRAND_NAVY)
                        pdf.set_line_width(0.4)
                        pdf.rect(_img_x - 1.5, _img_y - 1.5, _img_w + 3, _img_h + 3)
                        cls._corner_ticks(pdf, _img_x - 1.5, _img_y - 1.5, _img_w + 3, _img_h + 3, size=4, color=BRAND_CYAN, width=0.4)
                        pdf.image(thermal_annotated, x=_img_x, y=_img_y, w=_img_w, h=_img_h)
                        pdf.ln(_img_h + 8)
                    except Exception as e:
                        logger.warning('Failed to add annotated thermal image to PDF: %s', e)
                    finally:
                        __import__('os').remove(thermal_annotated)

                pdf.set_font('helvetica', 'I', 7.5)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 5, 'Note: refer to the Nexucon web dashboard for the interactive temperature-variance heatmap.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(2)
                pdf.set_text_color(*BRAND_DARK)
                for i, a in enumerate(ano_list, 1):
                    sev = str(getattr(a, 'severity', 'low')).lower()
                    sev_color = cls._severity_color(sev)
                    variance = getattr(a, 'temperature_variance', 0) or 0
                    loc_x = getattr(a, 'location_x', 0) or 0
                    loc_y = getattr(a, 'location_y', 0) or 0
                    loc_z = getattr(a, 'location_z', 0) or 0
                    conf_raw = getattr(a, 'confidence_score', None)
                    conf = f'{conf_raw * 100:.0f}%' if conf_raw is not None else 'N/A'
                    desc = str(getattr(a, 'description', '')).encode('ascii', 'ignore').decode('ascii')
                    img = getattr(a, 'image_url', '') or ''
                    sla = SLA_DAYS.get(sev, 14)
                    acct = ACCOUNTABILITY.get(sev, 'Site Supervisor / Contractor')

                    if pdf.get_y() + 45 > pdf.page_break_trigger:
                        pdf.add_page()

                    head_y = pdf.get_y()
                    pdf.set_draw_color(*sev_color)
                    pdf.set_line_width(0.4)
                    pdf.line(pdf.l_margin, head_y, pdf.l_margin + card_w, head_y)
                    pdf.set_font('courier', 'B', 9)
                    pdf.set_text_color(*sev_color)
                    pdf.cell(0, 6, f'THERMAL ANOMALY #{i} -- {variance:.1f} DEG C VARIANCE', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                    cls._dim_row(pdf, 'Severity', sev.upper(), w_label=30, font_size=7.5)
                    pdf.ln(4.5)
                    cls._dim_row(pdf, 'SLA', f'{sla} DAYS', w_label=30, font_size=7.5)
                    pdf.ln(4.5)
                    cls._dim_row(pdf, 'Responsible', acct, w_label=30, font_size=7.5)
                    pdf.ln(4.5)
                    cls._dim_row(pdf, 'Location', f'X{loc_x:.2f} Y{loc_y:.2f} Z{loc_z:.2f}', w_label=30, font_size=7.5)
                    pdf.ln(4.5)
                    cls._dim_row(pdf, 'AI Confidence', conf, w_label=30, font_size=7.5)
                    pdf.ln(5)
                    if desc:
                        pdf.set_font('courier', 'B', 7)
                        pdf.set_text_color(*BRAND_DARK)
                        pdf.cell(30, 5, 'AI FINDING')
                        pdf.set_font('helvetica', '', 7.8)
                        pdf.set_text_color(*DARK_GREY)
                        pdf.multi_cell(0, 4.4, desc, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    plain = cls._plain_english('thermal_anomaly', sev)
                    pdf.set_draw_color(*BRAND_CYAN)
                    pdf.set_line_width(0.2)
                    wy = pdf.get_y()
                    pdf.line(pdf.l_margin, wy, pdf.l_margin, wy + 9)
                    pdf.set_xy(pdf.l_margin + 2.5, wy)
                    pdf.set_font('courier', 'B', 7)
                    pdf.set_text_color(*BRAND_BLUE)
                    pdf.cell(0, 3.5, 'WHAT THIS MEANS', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_x(pdf.l_margin + 2.5)
                    pdf.set_font('helvetica', 'I', 7.5)
                    pdf.set_text_color(*DARK_GREY)
                    pdf.multi_cell(0, 3.6, plain, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                    pdf.set_font('courier', '', 7)
                    pdf.set_text_color(*DARK_GREY)
                    pdf.cell(0, 4.5, f"REF: {NIGERIAN_STANDARDS['thermal_anomaly']}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if img:
                        embedded = cls._try_embed_image(pdf, img, w=65)
                        if not embedded:
                            pdf.set_font('helvetica', '', 7)
                            pdf.cell(0, 5, f'Heatmap: {img}', link=img, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_text_color(*BRAND_DARK)
                    pdf.ln(4)

        # ================= SECTION 6 =================
        if 'bim' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '6', 'BIM Comparison & Deviation Analysis')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, "BIM (Building Information Modelling) is the approved 3D design model for this project. Nexucon compares the real-world scan against the BIM to find where construction differs from design ('deviations'). Ref: ISO 19650 and FMB Nigeria BIM documentation guidelines.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(*BRAND_DARK)
            if frame_mismatch:
                # Mean larger than every per-point deviation: inconsistent
                # comparison — flag the likely coordinate-frame error
                # instead of a structural STOP WORK verdict.
                fy = pdf.get_y()
                pdf.set_draw_color(*SEV_MEDIUM)
                pdf.set_line_width(0.5)
                pdf.rect(pdf.l_margin, fy, card_w, 10)
                cls._corner_ticks(pdf, pdf.l_margin, fy, card_w, 10, size=4, color=SEV_MEDIUM, width=0.4)
                pdf.set_xy(pdf.l_margin, fy + 2)
                pdf.set_font('courier', 'B', 10)
                pdf.set_text_color(*SEV_MEDIUM)
                pdf.cell(card_w, 6, 'DATA VALIDATION FLAG -- LIKELY COORDINATE-SYSTEM ERROR', align='C')
                pdf.set_y(fy + 13)
                pdf.set_fill_color(*ALERT_AMB_BG)
                pdf.set_draw_color(*SEV_MEDIUM)
                pdf.set_line_width(0.2)
                box2_y = pdf.get_y()
                pdf.rect(pdf.l_margin, box2_y, card_w, 26, style='FD')
                pdf.set_xy(pdf.l_margin + 4, box2_y + 2)
                pdf.set_font('courier', 'B', 8)
                pdf.set_text_color(*SEV_MEDIUM)
                pdf.cell(0, 5, 'WHY THIS IS NOT A STRUCTURAL FINDING:', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_x(pdf.l_margin + 4)
                pdf.set_font('helvetica', '', 8.3)
                pdf.set_text_color(*DARK_GREY)
                pdf.multi_cell(card_w - 8, 4.4, f'The mean deviation ({mean_dev:.2f} m) is larger than every individual measured deviation point (max {max(_top_vals_m):.2f} m). That is geometrically impossible when scan and BIM share one coordinate frame, so the scan is most likely not georeferenced to the BIM project frame (or a units/transform error occurred on ingest). Re-check the scan georeferencing / project control points and re-run "Align to BIM" before drawing any structural conclusion.')
                pdf.set_y(box2_y + 28)
                pdf.set_text_color(*BRAND_DARK)
            elif mean_dev is not None:
                if bim_alert:
                    alert_y = pdf.get_y()
                    pdf.set_draw_color(*SEV_CRITICAL)
                    pdf.set_line_width(0.6)
                    pdf.rect(pdf.l_margin, alert_y, card_w, 11)
                    cls._corner_ticks(pdf, pdf.l_margin, alert_y, card_w, 11, size=4, color=SEV_CRITICAL, width=0.5)
                    pdf.set_xy(pdf.l_margin, alert_y + 2.4)
                    pdf.set_font('courier', 'B', 13)
                    pdf.set_text_color(*SEV_CRITICAL)
                    pdf.cell(card_w, 7, 'STOP WORK -- MAJOR POSITION ERROR DETECTED', align='C')
                    pdf.set_y(alert_y + 15)
                    pdf.set_fill_color(*ALERT_RED_BG)
                    pdf.set_draw_color(*SEV_CRITICAL)
                    pdf.set_line_width(0.2)
                    box2_y = pdf.get_y()
                    pdf.rect(pdf.l_margin, box2_y, card_w, 40, style='FD')
                    pdf.set_xy(pdf.l_margin + 4, box2_y + 2)
                    pdf.set_font('courier', 'B', 8)
                    pdf.set_text_color(*ALERT_RED_TXT)
                    pdf.cell(0, 5, 'WHAT IS THE PROBLEM?', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_x(pdf.l_margin + 4)
                    pdf.set_font('helvetica', '', 8.3)
                    pdf.multi_cell(card_w - 8, 4.6, f'The AI has discovered that the building is NOT in the right place. According to the design plans, the building should be in its designated position. Our scan shows it is actually {mean_dev:.2f} metres away from where it should be.')
                    pdf.ln(1)
                    pdf.set_x(pdf.l_margin + 4)
                    pdf.set_font('courier', 'B', 8)
                    pdf.cell(0, 5, 'WHAT MUST HAPPEN NOW (NEXT 24 HOURS):', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_x(pdf.l_margin + 4)
                    pdf.set_font('helvetica', '', 8.3)
                    pdf.multi_cell(card_w - 8, 4.6, '1. Contact the site surveyor to re-check the project control points immediately.\n2. Do NOT proceed: no further concrete pours or wall construction until the surveyor confirms the correct position.\n3. Re-scan the building once coordinates are confirmed.')
                    pdf.set_y(box2_y + 42)
                    pdf.set_font('courier', '', 7)
                    pdf.set_text_color(*DARK_GREY)
                    scan_date = scan.created_at.strftime('%Y-%m-%d') if getattr(scan, 'created_at', None) else 'this scan'
                    pdf.cell(0, 5, f'FIRST DETECTED: {scan_date} (Nexucon-AI structural comparison engine)', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_text_color(*BRAND_DARK)
                else:
                    compliance = mean_dev < 0.01
                    comp_color = SEV_LOW if compliance else SEV_MEDIUM
                    label = 'PASS - BUILDING POSITION WITHIN ACCEPTABLE TOLERANCE' if compliance else 'REVIEW REQUIRED - POSITION DEVIATION DETECTED'
                    cy = pdf.get_y()
                    pdf.set_draw_color(*comp_color)
                    pdf.set_line_width(0.4)
                    pdf.rect(pdf.l_margin, cy, card_w, 9)
                    pdf.set_xy(pdf.l_margin, cy + 1.6)
                    pdf.set_font('courier', 'B', 9)
                    pdf.set_text_color(*comp_color)
                    pdf.cell(card_w, 6, label, align='C')
                    pdf.ln(13)
                    pdf.set_text_color(*BRAND_DARK)
                pdf.ln(2)
                cls._dim_row(pdf, 'Mean Deviation (Scan vs BIM)', f'{mean_dev:.4f} M')
                pdf.ln(5.2)
                cls._dim_row(pdf, 'Tolerance Threshold', '0.010 M  (NIS 87 / ISO 19650)')
                pdf.ln(5.2)
                cls._dim_row(pdf, 'Applicable Standard', NIGERIAN_STANDARDS['bim_deviation'], font_size=7.5)
                pdf.ln(5.2)
                if top_devs:
                    pdf.ln(2)
                    cls._subsection_bar(pdf, 'Top Deviation Points')
                    dev_mag_w, dev_loc_w = 30, 80
                    dev_desc_w = card_w - dev_mag_w - dev_loc_w
                    pdf.set_font('courier', 'B', 7.5)
                    pdf.set_fill_color(*BRAND_NAVY)
                    pdf.set_text_color(*WHITE)
                    pdf.cell(dev_mag_w, 6, ' MAGNITUDE', fill=True)
                    pdf.cell(dev_loc_w, 6, ' LOCATION', fill=True)
                    pdf.cell(dev_desc_w, 6, ' DESCRIPTION', fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_text_color(*BRAND_DARK)
                    for j, dev in enumerate(top_devs):
                        fill_bg = LIGHT_GREY if j % 2 == 0 else WHITE
                        pdf.set_fill_color(*fill_bg)
                        dev_mm = dev.get('deviation_mm')
                        # Description comes from the persisted hotspot; fall
                        # back to a plain-English line derived from the
                        # hotspot's own fields so no row ever reads "N/A".
                        desc = dev.get('description')
                        if not desc:
                            if str(dev.get('type', '')).lower() == 'clash':
                                desc = f"Clash between as-built geometry and {dev.get('element', 'BIM element')}"
                            elif dev_mm is not None:
                                desc = f"Deviation of {dev_mm}mm from BIM design surface"
                            else:
                                desc = 'Deviation hotspot flagged by scan-vs-BIM comparison'
                        # Full description, word-wrapped: the row grows to
                        # fit the text instead of cutting it at 55 chars.
                        pdf.set_font('helvetica', '', 7.8)
                        desc_lines = pdf.multi_cell(dev_desc_w - 2, 4, f" {desc}", dry_run=True, output='LINES')
                        row_h = max(6, len(desc_lines) * 4 + 2)
                        if pdf.get_y() + row_h > pdf.page_break_trigger:
                            pdf.add_page()
                        row_y = pdf.get_y()
                        pdf.rect(pdf.l_margin, row_y, card_w, row_h, style='F')
                        pdf.set_xy(pdf.l_margin, row_y + 1)
                        pdf.set_font('courier', '', 7.8)
                        pdf.cell(dev_mag_w, 5, f" {dev_mm / 1000.0:.4f} M" if dev_mm is not None else ' N/A')
                        pdf.set_xy(pdf.l_margin + dev_mag_w, row_y + 1)
                        pdf.set_font('helvetica', '', 7.8)
                        pdf.cell(dev_loc_w, 5, f" {str(dev.get('location', 'N/A'))}")
                        pdf.set_xy(pdf.l_margin + dev_mag_w + dev_loc_w, row_y + 1)
                        pdf.multi_cell(dev_desc_w - 2, 4, f" {desc}")
                        pdf.set_y(row_y + row_h)
            else:
                pdf.set_font('helvetica', 'I', 8.5)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 8, 'BIM Alignment analysis has not yet been completed for this scan session.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 7 =================
        if 'clash' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '7', 'Clash Detection - Building Element Conflicts')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, "Our AI checks whether building elements are fighting for the same space in the design model. When two walls, beams, or pipes are designed to occupy the same physical spot, it is a 'clash'. Catching these before construction prevents costly rework.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(*BRAND_DARK)
            if not clashes:
                pdf.set_draw_color(*SEV_LOW)
                pdf.set_fill_color(*ALERT_GRN_BG)
                pdf.set_line_width(0.3)
                pdf.rect(pdf.l_margin, pdf.get_y(), card_w, 10, style='FD')
                pdf.set_xy(pdf.l_margin + 4, pdf.get_y() + 2)
                pdf.set_font('courier', 'B', 9)
                pdf.set_text_color(*ALERT_GRN_TXT)
                pdf.cell(0, 6, 'NO DESIGN CONFLICTS DETECTED IN THE BIM MODEL.')
                pdf.set_text_color(*BRAND_DARK)
            else:
                cy = pdf.get_y()
                pdf.set_draw_color(*SEV_MEDIUM)
                pdf.set_line_width(0.4)
                pdf.rect(pdf.l_margin, cy, card_w, 9)
                pdf.set_xy(pdf.l_margin, cy + 1.6)
                pdf.set_font('courier', 'B', 9)
                pdf.set_text_color(*SEV_MEDIUM)
                pdf.cell(card_w, 6, f'{len(clashes)} BUILDING ELEMENT CONFLICT(S) FOUND', align='C')
                pdf.ln(13)
                pdf.set_text_color(*BRAND_DARK)
                medium_clashes = [c for c in clashes if str(c.get('severity', '')).lower() == 'medium']
                high_clashes = [c for c in clashes if str(c.get('severity', '')).lower() in ('high', 'critical')]
                if high_clashes:
                    cls._dim_row(pdf, 'High-severity', f'{len(high_clashes)} -- require urgent design review', font_size=8, value_color=SEV_HIGH)
                    pdf.ln(5)
                if medium_clashes:
                    cls._dim_row(pdf, 'Medium-severity', f'{len(medium_clashes)} -- fix before relevant floors', font_size=8, value_color=SEV_MEDIUM)
                    pdf.ln(5)
                pdf.ln(2)
                pdf.set_draw_color(*BRAND_CYAN)
                pdf.set_line_width(0.2)
                wy = pdf.get_y()
                pdf.line(pdf.l_margin, wy, pdf.l_margin, wy + 6)
                pdf.set_xy(pdf.l_margin + 2.5, wy)
                pdf.set_font('courier', 'B', 7.5)
                pdf.set_text_color(*BRAND_BLUE)
                pdf.multi_cell(card_w - 3, 3.6, 'WHAT TO DO NEXT: design team must review the 3D model and resolve these conflicts before construction reaches the specified floors.')
                pdf.ln(3)
                pdf.set_text_color(*BRAND_DARK)
                pdf.set_font('courier', 'B', 7.5)
                pdf.set_fill_color(*BRAND_NAVY)
                pdf.set_text_color(*WHITE)
                col_id, col_sev, col_el = 35, 22, 75
                col_loc = card_w - col_id - col_sev - col_el * 2
                pdf.cell(col_id, 6, ' CLASH ID', fill=True)
                pdf.cell(col_sev, 6, ' SEVERITY', fill=True)
                pdf.cell(col_el, 6, ' ELEMENT 1', fill=True)
                pdf.cell(col_el, 6, ' ELEMENT 2', fill=True)
                pdf.cell(col_loc, 6, ' LOCATION', fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BRAND_DARK)
                for j, c in enumerate(clashes):
                    fill_bg = LIGHT_GREY if j % 2 == 0 else WHITE
                    pdf.set_fill_color(*fill_bg)
                    el1 = str(c.get('element1_id', 'N/A')).encode('ascii', 'ignore').decode('ascii')
                    el2 = str(c.get('element2_id', 'N/A')).encode('ascii', 'ignore').decode('ascii')
                    loc = str(c.get('location', 'N/A'))
                    cid = str(c.get('id', 'N/A'))
                    # Measure the wrapped height of every column first, then
                    # draw the row at that height so GUIDs and long element
                    # names wrap inside their column instead of overflowing
                    # it or being cut off.
                    pdf.set_font('courier', '', 7.5)
                    id_lines = pdf.multi_cell(col_id - 2, 3.4, f' {cid}', dry_run=True, output='LINES')
                    pdf.set_font('helvetica', '', 6.5)
                    el1_lines = pdf.multi_cell(col_el - 2, 3.0, f' {el1}', dry_run=True, output='LINES')
                    el2_lines = pdf.multi_cell(col_el - 2, 3.0, f' {el2}', dry_run=True, output='LINES')
                    pdf.set_font('helvetica', '', 7.8)
                    loc_lines = pdf.multi_cell(col_loc - 2, 3.4, f' {loc}', dry_run=True, output='LINES')
                    row_h = max(6, max(len(id_lines) * 3.4, len(el1_lines) * 3.0, len(el2_lines) * 3.0, len(loc_lines) * 3.4) + 2.4)
                    if pdf.get_y() + row_h > pdf.page_break_trigger:
                        pdf.add_page()
                    row_y = pdf.get_y()
                    pdf.rect(pdf.l_margin, row_y, card_w, row_h, style='F')
                    pdf.set_draw_color(*HAIRLINE)
                    pdf.set_line_width(0.12)
                    for cx in (col_id, col_id + col_sev, col_id + col_sev + col_el, col_id + col_sev + col_el * 2):
                        pdf.line(pdf.l_margin + cx, row_y, pdf.l_margin + cx, row_y + row_h)
                    pdf.set_xy(pdf.l_margin, row_y + 1.2)
                    pdf.set_font('courier', '', 7.5)
                    pdf.multi_cell(col_id - 2, 3.4, f' {cid}')
                    sevc = cls._severity_color(str(c.get('severity', '')))
                    pdf.set_xy(pdf.l_margin + col_id, row_y + 1.2)
                    pdf.set_font('helvetica', 'B', 7.5)
                    pdf.set_text_color(*sevc)
                    pdf.multi_cell(col_sev - 2, 3.4, f" {str(c.get('severity', 'N/A')).upper()}")
                    pdf.set_text_color(*BRAND_DARK)
                    pdf.set_xy(pdf.l_margin + col_id + col_sev, row_y + 1.2)
                    pdf.set_font('helvetica', '', 6.5)
                    pdf.multi_cell(col_el - 2, 3.0, f' {el1}')
                    pdf.set_xy(pdf.l_margin + col_id + col_sev + col_el, row_y + 1.2)
                    pdf.multi_cell(col_el - 2, 3.0, f' {el2}')
                    pdf.set_xy(pdf.l_margin + col_id + col_sev + col_el * 2, row_y + 1.2)
                    pdf.set_font('helvetica', '', 7.8)
                    pdf.multi_cell(col_loc - 2, 3.4, f' {loc}')
                    pdf.set_y(row_y + row_h)

        # ================= SECTION 8 =================
        if 'recommendations' in sections:
            if pdf.get_y() + 60 > pdf.page_break_trigger:
                pdf.add_page()
            else:
                pdf.ln(8)
            cls._section_heading(pdf, '8', 'Engineering Recommendations - Prioritised To-Do List')
            pdf.set_font('helvetica', 'I', 8.5)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(0, 5, "This is the project's To-Do list. Each item has a Priority, Risk level, Recommended Action, and who is Accountable. SLA: CRITICAL=2 days, URGENT/HIGH=7 days, ROUTINE/MEDIUM=10 days, LOW=14 days.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
            pdf.set_text_color(*BRAND_DARK)
            for sev_label, sev_color, days in [('CRITICAL', SEV_CRITICAL, 2), ('URGENT', SEV_HIGH, 7), ('ROUTINE', SEV_MEDIUM, 10), ('PLANNING', SEV_LOW, 14)]:
                cls._swatch(pdf, pdf.l_margin, pdf.get_y() + 1, sev_color, size=3)
                pdf.set_x(pdf.l_margin + 5)
                pdf.set_font('courier', 'B', 7.5)
                pdf.set_text_color(*sev_color)
                pdf.cell(24, 5, sev_label)
                pdf.set_font('helvetica', '', 7.8)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 5, f'remediate within {days} days', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            col_num = 8
            col_pri = 24
            col_risk = 35
            col_act = pdf.w - pdf.l_margin - pdf.r_margin - col_num - col_pri - col_risk - 45
            col_acct = 45
            pdf.set_fill_color(*BRAND_NAVY)
            pdf.set_text_color(*WHITE)
            pdf.set_font('courier', 'B', 7.5)
            pdf.cell(col_num, 7, '#', fill=True, align='C')
            pdf.cell(col_pri, 7, 'PRIORITY', fill=True, align='C')
            pdf.cell(col_risk, 7, 'RISK', fill=True, align='C')
            pdf.cell(col_act, 7, ' RECOMMENDED ACTION', fill=True)
            pdf.cell(col_acct, 7, ' ACCOUNTABILITY', fill=True)
            pdf.ln(7)
            if not recommendations:
                pdf.set_font('helvetica', 'I', 8.5)
                pdf.set_text_color(*DARK_GREY)
                pdf.cell(0, 8, 'No specific recommendations were generated for this scan.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BRAND_DARK)
            else:
                for i, rec in enumerate(recommendations, 1):
                    if isinstance(rec, dict):
                        text = str(rec.get('recommendation', '')).encode('ascii', 'ignore').decode('ascii')
                        priority = str(rec.get('priority', 'Low')).lower()
                        linked = str(rec.get('related_finding_id', ''))
                        if linked:
                            text = f'{text} (Ref: {linked})'
                    else:
                        text = str(rec).encode('ascii', 'ignore').decode('ascii')
                        priority = 'low'
                    sev_color = cls._severity_color(priority)
                    tl_label, _ = TRAFFIC_LIGHT.get(priority, ('ROUTINE', SEV_LOW))
                    acct = ACCOUNTABILITY.get(priority, 'Site Supervisor')
                    risk_labels = {'critical': 'Structural & Safety', 'high': 'Safety & Quality', 'medium': 'Quality', 'low': 'Low'}
                    risk = risk_labels.get(priority, 'Low')
                    std = NIGERIAN_STANDARDS.get('general')
                    fill_bg = LIGHT_GREY if i % 2 == 0 else WHITE

                    if pdf.get_y() + 16 > pdf.page_break_trigger:
                        pdf.add_page()

                    pdf.set_fill_color(*fill_bg)
                    pdf.set_text_color(*BRAND_DARK)
                    pdf.set_font('courier', 'B', 7.5)
                    pdf.cell(col_num, 7, str(i), fill=True, align='C')
                    pdf.set_text_color(*sev_color)
                    pdf.cell(col_pri, 7, tl_label, fill=True, align='C')
                    pdf.set_text_color(*DARK_GREY)
                    pdf.set_font('helvetica', '', 7.5)
                    pdf.cell(col_risk, 7, risk, fill=True)
                    x_before = pdf.get_x()
                    y_before = pdf.get_y()
                    pdf.set_font('helvetica', '', 7.8)
                    pdf.multi_cell(col_act, 4, ' ' + text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    y_after = pdf.get_y()
                    pdf.set_xy(x_before + col_act, y_before)
                    pdf.set_font('courier', '', 7)
                    pdf.cell(col_acct, max(7, y_after - y_before), acct, fill=True)
                    pdf.set_xy(pdf.l_margin, max(y_after, y_before + 7))
                    pdf.set_font('courier', '', 6.2)
                    pdf.set_text_color(*DARK_GREY)
                    pdf.cell(0, 4, f'     REF: {std}', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 9: OBSERVATIONS, CONCLUSIONS & RECOMMENDATIONS =================
        # The written engineering narrative behind the tables and images in
        # Sections 2-8: what was observed, what it means, and what should
        # happen next. Every statement is derived from the same measured
        # data — nothing is invented, and anything not assessed says so.
        if pdf.get_y() + 60 > pdf.page_break_trigger:
            pdf.add_page()
        else:
            pdf.ln(8)
        cls._section_heading(pdf, '9', 'Observations, Conclusions & Recommendations - Detailed Narrative')
        pdf.set_font('helvetica', 'I', 8.5)
        pdf.set_text_color(*DARK_GREY)
        pdf.multi_cell(0, 5, 'This section is the written engineering narrative behind the tables and images in this report. It explains what was observed on site, what those observations mean for the project, and exactly what should happen next. Every statement below is drawn from the measured data in Sections 2-8 of this report.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        pdf.set_text_color(*BRAND_DARK)

        def _para(title, body, title_color=BRAND_BLUE):
            """One titled narrative paragraph: courier label, wrapped body."""
            if pdf.get_y() + 20 > pdf.page_break_trigger:
                pdf.add_page()
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*title_color)
            pdf.cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font('helvetica', '', 8.3)
            pdf.set_text_color(*BRAND_DARK)
            pdf.multi_cell(0, 4.3, body, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2.5)

        # ---- Measured aggregates the narrative is built from ----
        sev_counts = {s: len([d for d in def_list if str(getattr(d, 'severity', '')).lower() == s]) for s in ('critical', 'high', 'medium', 'low')}
        type_counts = {}
        for d in def_list:
            t = str(getattr(d, 'type', 'defect')).replace('_', ' ').title()
            type_counts[t] = type_counts.get(t, 0) + 1
        type_str = ', '.join(f'{n} x {t}' for t, n in type_counts.items())
        max_var = max((float(getattr(a, 'temperature_variance', 0) or 0) for a in ano_list), default=None)
        clash_high = len([c for c in (clashes or []) if str(c.get('severity', '')).lower() in ('high', 'critical')])
        clash_medium = len([c for c in (clashes or []) if str(c.get('severity', '')).lower() == 'medium'])
        defect_meanings = {
            'crack': 'cracks are splits in the surface that can admit water and, over time, corrode the steel reinforcement inside the member',
            'concrete_crack': 'cracks are splits in the concrete that can admit water and, over time, corrode the steel reinforcement inside the member',
            'spalling': 'spalling is concrete breaking away from the surface, typically caused by rusting reinforcement expanding from within',
            'corrosion': 'corrosion on reinforcing steel expands as it develops and eventually splits the surrounding concrete, reducing the member\'s capacity',
            'thermal_anomaly': 'thermal anomalies are temperature differences that usually indicate hidden moisture, missing insulation or air leakage paths',
            'deformation': 'deformation indicates a member has moved or bent beyond its designed geometry, which can affect load distribution',
            'delamination': 'delamination means layers of the material have separated, leaving a hollow and weakened zone',
        }
        meaning_str = '; '.join(defect_meanings.get(str(d.type).lower(), 'an AI-confirmed surface anomaly requiring engineer review') for d in def_list[:3])
        sensors_list = getattr(scan, 'sensors_used', []) or []
        sensors_str = ', '.join(str(s).upper() for s in sensors_list) if sensors_list else 'not recorded on this session'
        max_dev_m = max(_top_vals_m) if _top_vals_m else None

        # ---------- OBSERVATIONS ----------
        cls._subsection_bar(pdf, '9.1  Observations')

        _para('OBSERVATION 1 - SURVEY METHOD AND COVERAGE',
              f'The site was surveyed on {date_str} using capture device "{scan_ref}" with the following capture modalities: {sensors_str}. The session is recorded with status {str(getattr(scan, "status", "-")).upper()}. During analysis the Nexucon AI inspection engine registered {len(def_list)} visual finding(s) and {len(ano_list)} thermal anomaly/anomalies, with an overall AI confidence of {conf_pct}. '
              + ('A high confidence figure means the vision models repeatedly and independently confirmed the findings; individual per-finding confidence values are printed on each finding card in Section 3.' if overall_confidence is not None and overall_confidence >= 0.7 else 'Where the overall confidence is moderate, the findings should be treated as strong indications to be confirmed by a physical inspection rather than as certainties.'))

        if not def_list:
            _para('OBSERVATION 2 - STRUCTURAL AND SURFACE CONDITION',
                  'No visual defects were detected in this scan. The surfaces captured by the vision models showed no cracks, spalling, corrosion, deformation or delamination above the detection threshold. This is a favourable result; however, it reflects only the areas and surfaces actually captured by this survey pass - any area obscured from the scanner remains unverified until a subsequent pass covers it.')
        else:
            _para('OBSERVATION 2 - STRUCTURAL AND SURFACE CONDITION',
                  f'{len(def_list)} visual finding(s) were recorded, distributed by severity as {sev_counts["critical"]} critical, {sev_counts["high"]} high, {sev_counts["medium"]} medium and {sev_counts["low"]} low, and by type as: {type_str}. In physical terms: {meaning_str}. '
                  + ('The presence of critical-severity findings means at least one condition was assessed as an immediate safety concern.' if sev_counts['critical'] else 'No finding reached critical severity, so no immediate life-safety trigger was raised by the visual inspection.')
                  + f' The full evidence, per-finding confidence, location coordinates and applicable Nigerian Standards for each item are given in Section 3 (Defect Findings).')

        if not ano_list:
            _para('OBSERVATION 3 - THERMAL PERFORMANCE (BUILDING ENVELOPE)',
                  'No thermal anomalies were detected. The building envelope shows no abnormal temperature patterns, which indicates continuous insulation and an intact air barrier in the areas imaged. Applicable standard: SON NIS 412 (Thermal Performance of Buildings).')
        else:
            var_str = f'up to {max_var:.1f} deg C' if max_var is not None else 'of measurable magnitude'
            _para('OBSERVATION 3 - THERMAL PERFORMANCE (BUILDING ENVELOPE)',
                  f'{len(ano_list)} thermal anomaly/anomalies were detected with temperature variance {var_str} against the surrounding surface. Thermography cannot see through structures - it reads surface temperature - but abnormal surface temperatures reliably indicate an underlying cause: hidden moisture (evaporative cooling), missing or displaced insulation, air leakage around openings, or an electrical hotspot. Each anomaly, with its exact bracketed location on the thermal image, is listed in Section 5. Applicable standard: SON NIS 412 (Thermal Performance of Buildings).')

        if frame_mismatch:
            _para('OBSERVATION 4 - POSITIONAL COMPLIANCE VS BIM DESIGN',
                  f'The scan-to-BIM comparison produced a mean deviation of {mean_dev:.2f} m, but every individual measured deviation point is smaller (maximum {max_dev_m:.2f} m). A mean cannot exceed every member of its own population, so this result is internally inconsistent and points to a data problem rather than a structural one: the scan and the BIM model are almost certainly not referenced to the same coordinate frame, or a units/georeferencing transform was skipped when the data was ingested. The comparison must be re-run after the surveyor confirms the project control points before ANY positional conclusion is drawn from it (see the data-validation flag in Section 6).')
        elif mean_dev is None:
            _para('OBSERVATION 4 - POSITIONAL COMPLIANCE VS BIM DESIGN',
                  'BIM alignment has not been run for this scan session, so no as-built versus design positional comparison is available. Without it, the report cannot state whether the constructed work is within the designed position. It is recommended that "Align to BIM" be executed against the project model to complete this assessment.')
        elif bim_alert:
            _para('OBSERVATION 4 - POSITIONAL COMPLIANCE VS BIM DESIGN',
                  f'The as-built scan deviates from the approved BIM design by a mean of {mean_dev:.2f} m against a tolerance of 0.010 m (10 mm) - roughly {mean_dev / 0.01:.0f} times the permitted value. This is a STOP WORK condition: if the measured shift is real, every dimension set out from the current position will inherit the error. The surveyor must re-verify the project control points before any further construction proceeds (see Section 6).')
        elif mean_dev < 0.01:
            _para('OBSERVATION 4 - POSITIONAL COMPLIANCE VS BIM DESIGN',
                  f'The as-built scan deviates from the approved BIM design by a mean of {mean_dev * 1000:.1f} mm, which is within the 10 mm tolerance of NIS 87 / ISO 19650. The constructed work is in the designed position to the accuracy of this survey. The largest individual deviations are itemised in the Top Deviation Points table in Section 6' + (f' (maximum {max_dev_m * 1000:.0f} mm).' if max_dev_m is not None else '.'))
        else:
            _para('OBSERVATION 4 - POSITIONAL COMPLIANCE VS BIM DESIGN',
                  f'The as-built scan deviates from the approved BIM design by a mean of {mean_dev * 1000:.1f} mm against the 10 mm tolerance, so the deviation is real but not of the magnitude of a structural misplacement - this is a workmanship/setting-out review item rather than a stop-work condition. The elements concerned are listed with their exact magnitudes in Section 6 and should be measured up on site and corrected or accepted by the engineer on record.')

        if clashes:
            _para('OBSERVATION 5 - DESIGN COORDINATION (CLASH DETECTION)',
                  f'{len(clashes)} clash(es) were found in the model: {clash_high} high/critical severity and {clash_medium} medium severity. A clash means two or more elements - beams, walls, pipes, ducts - are designed to occupy the same physical space, so they cannot be built as drawn. Each unfixed clash becomes rework the moment construction reaches that floor: typically demolition of freshly built work at a cost several times that of a drawing revision today. The conflicting elements and their locations are tabulated in Section 7.')
        else:
            _para('OBSERVATION 5 - DESIGN COORDINATION (CLASH DETECTION)',
                  'No element clashes were detected in the model comparison. The design is spatially coordinated in the areas scanned: no two elements are claimed to occupy the same space.')

        if progress_val:
            score_pct = (progress_val.progress_score or 0.0) * 100
            area = progress_val.covered_area_sqm or 0.0
            _para('OBSERVATION 6 - CONSTRUCTION PROGRESS',
                  f'Progress validation against the BIM model measures {score_pct:g}% physically complete, with {area:g} m2 of construction captured in the point cloud. ' + ('The remaining works are concentrated in the unscanned/unbuilt areas of the model; the progress figure will rise as subsequent scan passes capture them.' if score_pct < 100 else 'The captured works are complete relative to the model.') + ' The measurement basis is described in Section 4.')
        else:
            _para('OBSERVATION 6 - CONSTRUCTION PROGRESS',
                  'No progress validation result is stored for this scan session, so no percentage-complete figure can be reported. Run the progress validation pipeline against the BIM model to quantify physical completion.')

        # ---------- CONCLUSIONS ----------
        if pdf.get_y() + 30 > pdf.page_break_trigger:
            pdf.add_page()
        cls._subsection_bar(pdf, '9.2  Conclusions')

        if has_critical or bim_alert:
            verdict = 'THE SITE FAILS THE INSPECTION IN ITS CURRENT STATE'
        elif has_high or ano_list:
            verdict = 'THE SITE IS CONDITIONAL - SAFE TO CONTINUE ONLY WITH THE URGENT ACTIONS BELOW COMPLETED'
        else:
            verdict = 'THE SITE PASSES THE INSPECTION AND IS GENERALLY SOUND AND COMPLIANT'
        _para('OVERALL CONCLUSION', f'{verdict}. This verdict is the synthesis of every measured dimension of this survey: {len(def_list)} visual finding(s) ({sev_counts["critical"]} critical / {sev_counts["high"]} high), {len(ano_list)} thermal anomaly/anomalies, a mean scan-to-BIM deviation of ' + (f'{mean_dev * 1000:.1f} mm' if mean_dev is not None else 'not assessed') + f', {len(clashes or [])} design clash(es), and an overall AI confidence of {conf_pct}.')

        _para('CONCLUSION - STRUCTURAL INTEGRITY',
              'The main structure is sound and no critical structural weakness was detected by this survey. Findings of lower severity, if acted on within their SLA, will not develop into structural problems.' if not has_critical else f'The structure has {sev_counts["critical"]} critical-severity finding(s) that were assessed as immediate safety concerns. Until they are reviewed by a COREN-registered structural engineer and remediated or justified, they represent an unacceptable risk, and work in the affected zones should not proceed.')

        _para('CONCLUSION - BUILDING ENVELOPE',
              'The building envelope is performing as designed in the areas imaged: no abnormal heat-loss or moisture signatures were found.' if not ano_list else f'The envelope is not fully performing: {len(ano_list)} thermal anomaly/anomalies indicate heat loss and/or moisture paths that will raise energy cost and can seed concealed deterioration (hidden moisture is the precursor of corrosion and mould). These are repairable envelope works, not structural concerns.')

        _para('CONCLUSION - COMPLIANCE WITH DESIGN',
              'Compliance with design could not be assessed because the BIM comparison has not been run.' if mean_dev is None else ('Compliance with design could not be determined: the comparison data failed its own consistency check (likely coordinate-system error) and must be re-run.' if frame_mismatch else ('The constructed work is NOT in its designed position. This is the most serious finding class in this report and it invalidates dimension-critical work until corrected.' if bim_alert else 'The constructed work is within the designed positional tolerance.')))

        _para('CONCLUSION - DATA CONFIDENCE AND LIMITATIONS',
              f'All findings in this report were produced by AI-assisted analysis under the Nexucon Digital Eye platform with an overall confidence of {conf_pct}. AI findings are indicative: they must be reviewed and validated by a COREN-registered structural engineer before any remedial works are commissioned or rejected. The survey covered only what the scanner captured - surfaces obscured from the sensor were not assessed. Standards applied: Nigeria National Building Code 2006; NIS 87; NIS 439; NIS 412; SON general construction standards; ISO 19650.')

        # ---------- RECOMMENDATIONS (NARRATIVE) ----------
        if pdf.get_y() + 30 > pdf.page_break_trigger:
            pdf.add_page()
        cls._subsection_bar(pdf, '9.3  Recommendations')

        pdf.set_font('helvetica', 'I', 8)
        pdf.set_text_color(*DARK_GREY)
        pdf.multi_cell(0, 4.3, 'The prioritised action table in Section 8 lists every recommendation with its SLA and accountable party. The paragraphs below explain the reasoning behind each action and how to close it out. SLA definitions: CRITICAL = 2 days, URGENT/HIGH = 7 days, ROUTINE/MEDIUM = 10 days, LOW = 14 days.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_text_color(*BRAND_DARK)

        if recommendations:
            for i, rec in enumerate(recommendations, 1):
                if isinstance(rec, dict):
                    rec_text = str(rec.get('recommendation', '')).encode('ascii', 'ignore').decode('ascii')
                    priority = str(rec.get('priority', 'Low')).lower()
                    linked = str(rec.get('related_finding_id', ''))
                else:
                    rec_text = str(rec).encode('ascii', 'ignore').decode('ascii')
                    priority = 'low'
                    linked = ''
                sev_color = cls._severity_color(priority)
                tl_label, _ = TRAFFIC_LIGHT.get(priority, ('ROUTINE', SEV_LOW))
                sla = SLA_DAYS.get(priority, 14)
                acct = ACCOUNTABILITY.get(priority, 'Site Supervisor')
                risk_labels = {'critical': 'structural and life-safety risk', 'high': 'safety and quality risk', 'medium': 'quality risk', 'low': 'low residual risk'}
                body = (f'{rec_text} '
                        f'Why: this action carries {risk_labels.get(priority, "low residual risk")} if left unaddressed, and it derives directly from the observations above'
                        + (f' (linked finding {linked})' if linked else '')
                        + f'. Who: {acct}. Deadline: within {sla} days (priority {tl_label}). '
                          f'Closure: the action is closed when the responsible party completes the work and it is verified in the next Nexucon scan pass.')
                _para(f'RECOMMENDATION {i} - {tl_label} (SLA {sla} DAYS)', body, title_color=sev_color)
        else:
            _para('RECOMMENDATION 1 - MAINTAIN THE QUALITY PLAN',
                  'No specific AI recommendations were generated for this scan, which reflects a clean result set rather than an incomplete analysis. Continue scheduled monitoring in line with the project quality plan and re-scan after the next construction milestone.')
        if bim_alert:
            _para('RECOMMENDATION - POSITION RE-VERIFICATION (OVERRIDING)',
                  'Have the site surveyor re-verify the building grid and control points immediately, before any further dimension-critical work (concrete pours, wall setting-out). Once coordinates are confirmed, re-scan and re-run the BIM alignment so the deviation is either quantified properly or cleared.', title_color=SEV_CRITICAL)
        if frame_mismatch:
            _para('RECOMMENDATION - GEOREFERENCING CHECK (OVERRIDING)',
                  'Before any positional conclusion is drawn, confirm that the scan and the BIM model share one coordinate frame and that the correct project control points were used on ingest, then re-run "Align to BIM". A comparison that fails its own consistency check must never be used to justify site works.', title_color=SEV_MEDIUM)
        if ano_list:
            _para('RECOMMENDATION - ENVELOPE REMEDIATION',
                  'Commission the roofing/envelope contractor to reinstate insulation and seal the leakage paths identified in Section 5, then re-scan thermally to confirm the anomalies have cleared. Left open, these paths will continue to cost energy and can conceal moisture-driven deterioration.', title_color=SEV_HIGH)
        if clashes:
            _para('RECOMMENDATION - DESIGN COORDINATION REVIEW',
                  'Convene the design team to resolve the model clashes listed in Section 7 and re-issue the affected drawings before construction reaches those floors. Resolving a clash on the model costs a drawing revision; resolving it on site costs demolition and rebuilding.', title_color=SEV_MEDIUM)

        _para('FOLLOW-UP',
              'The Project Manager should assign every recommendation above, track each to closure against its SLA, and schedule a follow-up Nexucon scan once the surveyor and contractor have completed their works. The follow-up scan verifies the remediation - a finding that no longer appears in the next report is the objective evidence that the action was effective.')

        # ================= SECTION 10 =================
        if pdf.get_y() + 60 > pdf.page_break_trigger:
            pdf.add_page()
        else:
            pdf.ln(8)
        cls._section_heading(pdf, '10', 'Final Site Health Check & Conclusion')
        pdf.set_font('helvetica', 'I', 8.5)
        pdf.set_text_color(*DARK_GREY)
        pdf.multi_cell(0, 5, "This is the verdict. The Project Manager should assign the recommended actions and schedule a follow-up scan after the surveyor and contractor complete their work. All findings must be reviewed and validated by a certified structural engineer (COREN registered) before remedial works are commissioned.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        pdf.set_text_color(*BRAND_DARK)
        cls._subsection_bar(pdf, 'Final Site Health Check')
        health_verdicts = [('STRUCTURAL INTEGRITY', 'PASSED' if not has_critical else 'FAILED', SEV_LOW if not has_critical else SEV_CRITICAL, 'The main structure is sound. No major cracks or weaknesses found.' if not has_critical else f'{critical_count} critical structural issue(s) detected - see Defect Findings section.'), ('BUILDING ENVELOPE', 'CONDITIONAL' if ano_list else 'PASSED', SEV_HIGH if ano_list else SEV_LOW, 'Well-sealed except for areas identified in the thermal analysis (see heat-loss map). Urgent to fix.' if ano_list else 'No thermal anomalies detected. Building envelope is intact.'), ('COMPLIANCE WITH DESIGN', 'FAILED - CRITICAL' if bim_alert else 'PASSED' if mean_dev is not None else 'NOT ASSESSED', SEV_CRITICAL if bim_alert else SEV_LOW, f'Position error of {mean_dev:.2f}m is a serious safety risk that must be corrected immediately before further work.' if bim_alert else 'Building is in the correct position as per design.' if mean_dev is not None else 'BIM comparison was not performed for this scan.'), ('3D MODEL CONFLICTS', 'FOUND' if clashes else 'CLEAR', SEV_MEDIUM if clashes else SEV_LOW, f'{len(clashes)} conflict(s) found - fix the designs before construction reaches those floors.' if clashes else 'No design conflicts detected.')]
        for chk_label, chk_status, chk_color, chk_desc in health_verdicts:
            if pdf.get_y() + 16 > pdf.page_break_trigger:
                pdf.add_page()
            vy = pdf.get_y()
            pdf.set_draw_color(*HAIRLINE)
            pdf.set_line_width(0.2)
            pdf.rect(pdf.l_margin, vy, card_w, 13)
            cls._swatch(pdf, pdf.l_margin + 2, vy + 4.5, chk_color, size=3.2)
            pdf.set_xy(pdf.l_margin + 8, vy + 1.5)
            pdf.set_font('helvetica', 'B', 8.5)
            pdf.set_text_color(*BRAND_NAVY)
            pdf.cell(58, 5, chk_label)
            cls._status_tag(pdf, pdf.l_margin + 66, vy + 1.5, chk_status, chk_color, font_size=7)
            pdf.set_xy(pdf.l_margin + 130, vy + 1.5)
            pdf.set_font('helvetica', '', 7.8)
            pdf.set_text_color(*DARK_GREY)
            pdf.multi_cell(card_w - 133, 3.7, chk_desc)
            pdf.set_y(vy + 15)
            pdf.set_text_color(*BRAND_DARK)
        pdf.ln(2)
        pdf.set_draw_color(*BRAND_NAVY)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(1.5)
        pdf.set_font('courier', 'B', 8.5)
        pdf.set_text_color(*BRAND_NAVY)
        pdf.cell(0, 6, 'RECOMMENDED OVERALL ACTION', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font('helvetica', '', 8)
        pdf.set_text_color(*BRAND_DARK)
        actions = []
        if bim_alert:
            actions.append(('URGENT', "Have the site surveyor re-verify the building's coordinates immediately."))
        if ano_list:
            actions.append(('URGENT', 'Contact the roofing/building contractor to address the thermal heat loss.'))
        if def_list:
            actions.append(('ROUTINE', 'Monitor the crack(s) and resolve any surface defects at the next site visit.'))
        if clashes:
            actions.append(('PLANNING', 'Design team must fix the BIM model conflicts before work reaches those floors.'))
        if not actions:
            actions.append(('ROUTINE', 'Continue scheduled monitoring as per the project quality plan.'))
        for j, (label, action) in enumerate(actions, 1):
            pdf.set_font('courier', 'B', 7.5)
            pdf.set_text_color(*BRAND_BLUE)
            pdf.cell(20, 5.5, f'{j}. {label}')
            pdf.set_font('helvetica', '', 8)
            pdf.set_text_color(*BRAND_DARK)
            pdf.cell(0, 5.5, action, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_draw_color(*BRAND_CYAN)
        pdf.set_line_width(0.2)
        wy = pdf.get_y()
        pdf.line(pdf.l_margin, wy, pdf.l_margin, wy + 6)
        pdf.set_xy(pdf.l_margin + 2.5, wy)
        pdf.set_font('courier', 'B', 7.5)
        pdf.set_text_color(*BRAND_BLUE)
        pdf.multi_cell(card_w - 3, 3.6, 'NEXT STEPS: the Project Manager should assign these actions and schedule a follow-up scan after the surveyor and contractor complete their work.')
        pdf.set_text_color(*BRAND_DARK)

        # ================= SECTION 11 =================
        if pdf.get_y() + 60 > pdf.page_break_trigger:
            pdf.add_page()
        else:
            pdf.ln(8)
        cls._section_heading(pdf, '11', 'Professional Engineer Sign-Off')
        pdf.set_font('helvetica', 'I', 8.5)
        pdf.set_text_color(*DARK_GREY)
        pdf.multi_cell(0, 5, 'This report was prepared using AI-assisted analysis under the Nexucon Digital Eye platform. All AI findings carry a confidence score and must be reviewed and validated by a COREN-registered structural engineer before any remedial works are commissioned. Standards applied: SON General Construction Standards; Nigeria National Building Code 2006; NIS 87; NIS 439; NIS 412; ISO 19650.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5)
        pdf.set_text_color(*BRAND_DARK)
        cls._subsection_bar(pdf, 'Engineer Review & Sign-Off')
        pdf.ln(1)
        pdf.set_draw_color(*BRAND_DARK)
        for label in ['REVIEWED BY (NAME)', 'PROFESSIONAL QUALIFICATION', 'COREN REGISTRATION NO.', 'SIGNATURE', 'DATE OF REVIEW', 'COMPANY / FIRM']:
            pdf.set_font('courier', 'B', 8)
            pdf.set_text_color(*BRAND_BLUE)
            pdf.cell(65, 9, f'{label}:')
            pdf.set_draw_color(*HAIRLINE)
            pdf.set_line_width(0.25)
            pdf.cell(120, 9, '', border='B')
            pdf.ln(9)
        pdf.set_draw_color(*BRAND_DARK)
        pdf.ln(5)
        pdf.set_font('courier', 'I', 6.8)
        pdf.set_text_color(*DARK_GREY)
        pdf.cell(0, 5, 'GENERATED BY NEXUCON-AI v1.2. AI FINDINGS ARE INDICATIVE ONLY. PROFESSIONAL ENGINEERING VALIDATION IS REQUIRED BEFORE ACTION IS TAKEN.', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return pdf

    # ------------------------------------------------------------------
    # Public entry points (business logic - unchanged)
    # ------------------------------------------------------------------

    @classmethod
    def generate_qaqc_report(cls, scan: ScanSession, report_type: str = 'qaqc') -> QualityReport:
        """
        Gather scan metrics, defects, anomalies, deviations, and compile a QualityReport.

        ``report_type`` selects the template (see TEMPLATE_SECTIONS): it
        controls which sections the generated PDF contains.
        """
        if report_type not in TEMPLATE_SECTIONS:
            report_type = 'qaqc'
        logger.info('Generating QA/QC Report for ScanSession: %s (template: %s)', scan.id, report_type)
        defects = Defect.objects.filter(session=scan)
        anomalies = ThermalAnomaly.objects.filter(session=scan)
        project = getattr(scan, 'project', None)
        try:
            alignment = BIMAlignmentResult.objects.get(session=scan)
            mean_dev = alignment.mean_deviation
            top_devs = alignment.top_deviations
        except BIMAlignmentResult.DoesNotExist:
            mean_dev = None
            top_devs = None
        try:
            progress_val = ProgressValidationResult.objects.get(session=scan)
        except ProgressValidationResult.DoesNotExist:
            # No fabricated metrics — the report states plainly that progress
            # validation has not been run for this session.
            progress_val = None
        summary = {'scanner_id': scan.scanner_id, 'status': 'completed', 'sensors_used': scan.sensors_used, 'defects': [{'id': str(d.id), 'type': d.type, 'severity': d.severity} for d in defects], 'thermal_anomalies': [{'id': str(a.id), 'temperature_variance': a.temperature_variance, 'severity': a.severity} for a in anomalies]}
        from apps.common.ai_service import AIService
        defects_list = [{'type': d.type, 'severity': d.severity, 'description': d.description} for d in defects]
        anomalies_list = [{'temperature_variance': a.temperature_variance, 'severity': a.severity} for a in anomalies]
        # The AI prompt narrates the deviation in metres ("{deviation} m"),
        # while mean_dev is persisted millimetres — convert so the
        # generated recommendations don't call a 140.4mm mean a "140.4 m"
        # position error.
        recommendations_data = AIService.generate_recommendations(
            defects=defects_list, anomalies=anomalies_list,
            deviation=(mean_dev / 1000.0) if mean_dev is not None else 0.0,
        )
        recommendations = recommendations_data.get('recommendations', []) if isinstance(recommendations_data, dict) else recommendations_data
        text_confidence = recommendations_data.get('text_confidence') if isinstance(recommendations_data, dict) else None
        defect_confs = [d.confidence_score for d in defects if getattr(d, 'confidence_score', None) is not None]
        anomaly_confs = [a.confidence_score for a in anomalies if getattr(a, 'confidence_score', None) is not None]
        all_confs = defect_confs + anomaly_confs
        if all_confs:
            avg = sum(all_confs) / len(all_confs)
            overall_confidence = (avg + text_confidence) / 2.0 if text_confidence is not None else avg
        else:
            overall_confidence = text_confidence
        clashes = cls._fetch_clashes(scan)
        try:
            import os, tempfile
            pdf = cls._build_fpdf_document(scan, defects, anomalies, mean_dev, top_devs, recommendations, progress_val, clashes=clashes, overall_confidence=overall_confidence, project=project, include_sections=TEMPLATE_SECTIONS.get(report_type))
            fd, path = tempfile.mkstemp(suffix='.pdf')
            os.close(fd)
            pdf.output(path)
            from apps.storage.cloudinary_service import CloudinaryService
            report_url = CloudinaryService.upload_file(path, folder='reports')
            os.remove(path)
        except Exception as e:
            import traceback
            logger.error('Failed to generate PDF: %s\n%s', e, traceback.format_exc())
            report_url = None
        # One stored report per (scan, template): replace any prior version.
        QualityReport.objects.filter(scan=scan, report_type=report_type).delete()
        report = QualityReport.objects.create(
            scan=scan,
            project_id=scan.project_id,
            status='completed',
            report_type=report_type,
            summary=summary,
            recommendations=recommendations,
            report_url=report_url,
            defect_count=defects.count(),
            anomaly_count=anomalies.count(),
            mean_deviation=mean_dev,
            overall_ai_confidence=overall_confidence,
        )
        logger.info('QA/QC Report %s generated successfully.', report.id)
        try:
            from apps.inspections.services import InspectionService
            InspectionService.auto_generate_ncrs_for_scan(scan)
        except Exception as e:
            logger.error('Failed to auto-generate NCRs: %s', e)
        from apps.audit.services import AuditService
        AuditService.log_event(
            action='report_generated',
            resource_type='quality_report',
            resource_id=str(report.id),
            metadata={'session_id': str(scan.id), 'description': f'QA/QC report generated with {report.defect_count} defects and {report.anomaly_count} anomalies.'},
            new_state={'status': 'completed'}
        )
        return report

    @classmethod
    def generate_pdf_bytes(cls, report, cover_overrides=None, include_sections=None) -> bytes:
        scan = report.scan
        defects = Defect.objects.filter(session=scan)
        anomalies = ThermalAnomaly.objects.filter(session=scan)
        mean_dev = report.mean_deviation
        alignment = BIMAlignmentResult.objects.filter(session=scan).first()
        top_devs = None
        if alignment:
            top_devs = alignment.top_deviations

        recommendations = report.recommendations if isinstance(report.recommendations, list) else []
        try:
            progress_val = ProgressValidationResult.objects.get(session=scan)
        except ProgressValidationResult.DoesNotExist:
            progress_val = None
        project = getattr(scan, 'project', None)
        clashes = cls._fetch_clashes(scan)
        if include_sections is None:
            include_sections = TEMPLATE_SECTIONS.get(getattr(report, 'report_type', 'qaqc') or 'qaqc')
        pdf = cls._build_fpdf_document(scan, defects, anomalies, mean_dev, top_devs, recommendations, progress_val, clashes=clashes, overall_confidence=report.overall_ai_confidence, project=project, cover_overrides=cover_overrides, include_sections=include_sections)
        return bytes(pdf.output())

    @classmethod
    def _fetch_clashes(cls, scan) -> list:
        """Serve the clash results persisted by the last "Align to BIM" run.

        The DB is the single source of truth for the report: clash data is
        only recomputed when the alignment pipeline is re-run, never while
        rendering a report.
        """
        try:
            alignment = BIMAlignmentResult.objects.filter(session=scan).first()
            if not alignment:
                return []
            clashes = alignment.clashes or []
            logger.info('Clash data served from BIMAlignmentResult %s: %d clashes', alignment.id, len(clashes))
            return clashes
        except Exception as e:
            logger.warning('Could not read stored clash data: %s', e)
            return []
