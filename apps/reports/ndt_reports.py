"""
Lagos State Materials Testing Laboratory-style NDT (PUNDIT) report.

Visual format replicates the laboratory's in-situ integrity test report
("BOTANICAL GARDEN ROAD EBUTE METTA, LAGOS STATE (0481)"): US Letter
portrait, 72pt side margins, the laboratory watermark + blue serial box
running header on every page, typewriter cover block, Cambria body with a
Trebuchet MS note/signature block, table of contents with Title-Case
entries, numbered sections and ruled data tables.

Every figure is rendered from live PUNDITTest / FieldDevice / Project rows.
Where a value has not been recorded it is shown honestly ('-',
'NOT RECORDED') — nothing is ever fabricated. Estimated compressive
strength (E.C.S) is derived from a single fixed calibration curve that is
disclosed in full in Section 3.0 of the rendered report.
"""
import hashlib
import io
import logging
import os
import re
from datetime import datetime

from fpdf import FPDF

from .ai_reports import _latin1
from .report_cms import cms_list_items, cms_paragraphs, get_cms_text

logger = logging.getLogger(__name__)

INK = (0, 0, 0)
RULE_GREY = (60, 60, 60)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(MODULE_DIR, 'assets')
FONTS_DIR = os.path.join(MODULE_DIR, 'fonts')
WATERMARK_IMAGE = os.path.join(ASSETS_DIR, 'lsmtl_watermark.png')
COVER_LOGO_IMAGE = os.path.join(ASSETS_DIR, 'lagos_state_coat_of_arms.png')


def _cover_qr_png(url):
    """Render a verification QR code for the report cover into a PNG
    BytesIO the fpdf2 image() call can embed. Returns None when the qrcode
    library is unavailable — the cover renders without the QR rather than
    failing the whole statutory report."""
    import io
    try:
        import qrcode
        import qrcode.image.pil
    except ImportError:
        logger.warning('qrcode library unavailable — cover QR skipped')
        return None
    try:
        qr = qrcode.QRCode(
            version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.pil.PilImage)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf
    except Exception as exc:               # noqa: BLE001 — never break the
        logger.error('QR generation failed: %s', exc)   # report render
        return None


def report_verification_url(report_reference, content_digest):
    """Public URL a cover QR resolves to: the platform's report
    verification page for one archived dossier (reference + content
    digest). FRONTEND_URL is the configured public origin."""
    from urllib.parse import quote
    from django.conf import settings
    base = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    if not base:
        return None
    return (f'{base}/verify/report/'
            f'?ref={quote(report_reference or "")}'
            f'&digest={quote(content_digest or "")}')

# Reference header serial/reference box: light blue fill, darker blue border.
SERIAL_BOX_FILL = (91, 155, 213)
SERIAL_BOX_BORDER = (46, 117, 182)

# Reference chart colours: blue GOOD bars, red POOR bars.
CHART_GOOD = (91, 155, 213)
CHART_POOR = (255, 0, 0)

# Reference TOC entries are Title Case and diverge from the body headings
# in several places ("Field Work/Equipment Status Check" for the body's
# "FIELD WORK", plural "Recommendations", ...).
TOC_TITLES = {
    '1.0': 'Introduction',
    '2.0': 'Purpose of investigation',
    '3.0': 'Literature Review',
    '3.1': 'Location Map/ Weather Condition',
    '4.0': 'Field Work/Equipment Status Check',
    '4.1': 'Visual Test',
    '4.2': 'Methodology',
    '4.3': 'Reinforcing Bar (Rebar) Assessment',
    '4.4': 'Equipment/Rebar Assessment Table',
    '5.0': 'Analysis of Test Results',
    '6.0': 'Recommendations',
    '7.0': 'Conclusion',
}

# Appendix item TOC wordings (roman page ranges are appended at render time).
APPENDIX_ITEM_TITLES = {
    'DRAWING OF THE BUILDING': 'Drawing of the Building',
    'PHOTOGRAPHS': 'Photographs of the Building',
}

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
    "f_cu = 8.961 x V - 7.97 (f_cu in N/mm2, V in km/s), established by "
    "least-squares regression over the laboratory's reference control pairs "
    "(2.9, 19), (3.9, 25), (4.0, 27), (4.2, 29) and (4.4, 34) and valid over "
    "the range 2.0 - 5.0 km/s. BS 1881-203:1999 notes that no unique "
    "velocity-strength relationship exists for all concretes; the curve above "
    "is the documented calibration applied by this laboratory for Nigerian "
    "site concrete and is applied uniformly to every result in Section 5.0. "
    "Velocities outside the calibrated range are reported without an E.C.S "
    "estimate rather than extrapolated."
)

ECS_FORMULA_LINE = "E.C.S: f_cu = 8.961 x V - 7.97  (f_cu in N/mm2, V in km/s; valid 2.0 - 5.0 km/s)"


def estimated_compressive_strength(velocity_km_s):
    """
    E.C.S (N/mm2) from pulse velocity via the fixed calibration curve.
    Returns None when the velocity is missing or outside the calibrated
    2.0 - 5.0 km/s range (values are never extrapolated).

    Historical note: this fixed path is now only the built-in fallback of
    the Nexucon Link module (apps/digital_eye/strength_curves.py) — every
    platform E.C.S flows through apply_active_curve(), which resolves the
    project's active calibration curve and falls back to maths identical
    to this function.
    """
    if velocity_km_s is None:
        return None
    if not (ECS_VALID_MIN_KM_S <= velocity_km_s <= ECS_VALID_MAX_KM_S):
        return None
    return ECS_SLOPE * velocity_km_s + ECS_INTERCEPT


def ecs_report_disclosure(project):
    """
    (calibration_paragraph, derivation_sentence) for Section 3.0 — the
    project-specific Nexucon Link calibration when one is active, else the
    documented laboratory default text. The formula printed here is the
    formula actually applied to every Section 5.0 figure.
    """
    from apps.digital_eye.strength_curves import (
        curve_snapshot, formula_display, resolve_active_curve)

    default_derivation = (
        'f_cu = 8.961 x V - 7.97, with V expressed in km/s')
    try:
        curve = resolve_active_curve(project)
    except Exception:
        curve = None
    if curve is None or not curve.project_id:
        # No project-specific curve: the laboratory default disclosure.
        return ECS_CALIBRATION_SOURCE, default_derivation

    snap = curve_snapshot(curve)
    formula = snap.get('formula') or formula_display(
        curve.curve_type, curve.formula_params or {})
    parts = [
        "Estimated compressive strength (E.C.S) values in this report are "
        "derived from the project-specific calibration curve "
        f"established for this project under the Nexucon Link procedure: "
        f"{formula} (f_cu in N/mm2, V the pulse velocity in m/s"
        + (", R the rebound number)" if curve.curve_type == 'sonreb' else ")")
        + ".",
    ]
    if curve.standard:
        parts.append(f"Reference: {curve.standard}.")
    n_points = len(curve.data_points or [])
    if n_points:
        parts.append(
            f"The curve was fitted from {n_points} real calibration pair(s) "
            "(cube/core tests against field pulse velocity measurements).")
    if curve.r2_score is not None:
        parts.append(f"Coefficient of determination R² = {curve.r2_score:.4f}.")
    if curve.standard_error is not None:
        parts.append(
            f"Standard error of estimate = {curve.standard_error:.2f} N/mm2.")
    if curve.valid_range_min_ms is not None and curve.valid_range_max_ms is not None:
        parts.append(
            "The curve is valid over the calibrated velocity range "
            f"{curve.valid_range_min_ms:.0f} - {curve.valid_range_max_ms:.0f} m/s; "
            "velocities outside that range are reported without an E.C.S "
            "estimate rather than extrapolated.")
    source = (curve.provenance or {}).get('source')
    if source:
        parts.append(f"Source: {source}.")
    parts.append(
        "BS 1881-203:1999 notes that no unique velocity-strength relationship "
        "exists for all concretes; the curve above is the project-specific "
        "calibration applied to every result in Section 5.0.")
    derivation = (
        f"the project-specific calibration above, {formula}, with V "
        "expressed in m/s")
    return ' '.join(parts), derivation


def _wrap_lines(pdf, text, width):
    """
    Word-wrap ``text`` into lines no wider than ``width`` mm using the PDF's
    current font. Explicit newlines are respected as hard line breaks (cell
    content may pre-format e.g. Revit 'Family:Type:Tag' names one segment
    per line). A single token wider than the column (e.g. a 64-character
    content digest) is hard-broken — no characters are ever inserted or
    dropped, so cell contents can never spill into the neighbouring column.
    """
    # Transcribe first: fpdf2's get_string_width encodes strictly latin-1,
    # so measuring raw live-DB text (em-dashes, curly quotes, ...) would
    # raise — and wrapping must measure exactly the text that gets drawn.
    text = _latin1('-' if text in (None, '') else text)
    lines = []
    for part in text.split('\n'):
        words = [w for w in part.split(' ') if w]
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


def _element_display(name):
    """
    BIM element names follow Revit's 'Family:Type:Tag' convention — each
    colon-separated segment gets its own line so the name reads as whole
    words in the narrow report columns instead of breaking mid-token
    ('M_Footing-Rectangula / r:900').
    """
    name = (name or '').strip()
    if not name:
        return '-'
    segments = [s.strip() for s in name.split(':') if s.strip()]
    return '\n'.join(segments) if segments else '-'


def _to_roman(n):
    out = ''
    for value, symbol in ((10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):
        while n >= value:
            out += symbol
            n -= value
    return out or 'I'


def _ordinal_suffix(n):
    if 10 <= n % 100 <= 20:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


def _prose_date(d):
    """'27th April, 2026' — the reference's prose date form."""
    return f'{d.day}{_ordinal_suffix(d.day)} {d.strftime("%B")}, {d.year}'


def _cover_date(d):
    """'27TH APRIL, 2026.' — the reference cover's single date line."""
    return (f'{d.day}{_ordinal_suffix(d.day).upper()} '
            f'{d.strftime("%B").upper()}, {d.year}.')


def _pretty_ifc_category(element_type):
    """
    'IfcSlab' -> 'Slab', 'IfcBuildingElementProxy' -> 'Building Element
    Proxy'. The report is read by non-technical reviewers, so the CATEGORY
    column shows plain words; the raw IFC class names stay in the
    platform's model preview.
    """
    name = (element_type or '').strip()
    if not name:
        return ''
    if name.startswith('Ifc'):
        name = name[3:]
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)


# ---------------------------------------------------------------------------
# PDF subclass — Letter portrait, reference running header/footer
# ---------------------------------------------------------------------------
class MTLReportPDF(FPDF):
    """US Letter portrait document in the reference's typographic system.
    Every page (the cover included) carries the laboratory watermark
    centred on the page, 'MTL/NDT/<year>' Times-Bold 16 top-right and the
    blue serial box in the top-right corner; a plain Times 12 page number
    sits bottom-centre — none on the cover/TOC/summary pages, roman
    numerals inside the appendix. Body text is Cambria; the note and
    signature blocks are Trebuchet MS."""

    def __init__(self, running_header, *args, **kwargs):
        super().__init__(orientation='P', unit='mm', format='Letter', *args, **kwargs)
        self.running_header = running_header
        # Physical pages before body numbering starts (cover + TOC +
        # executive summary) — set when section 1.0 is emitted.
        self.body_page_offset = None
        # Physical page where the appendix (roman numbering) begins.
        self.roman_from_page = None
        # Optional project branding (REFINED EXECUTIVE SUMMARY §2.3):
        # (logo bytes, position, width_mm) stamped on the cover, and a
        # watermark (bytes, opacity 0-1) that REPLACES the laboratory
        # watermark on every page. None/empty = the standard laboratory
        # layout, unchanged. The images are held as in-memory buffers read
        # through the FieldFile API so remote storages (R2/S3, where
        # ``.path`` raises NotImplementedError) render identically to disk.
        self.branding_logo = None      # (BytesIO, 'top-left'|..., width_mm)
        self.branding_watermark = None  # (BytesIO, opacity_pct 0-100)
        # Reference fonts (bundled in apps/reports/fonts).
        for family, styles in (
            ('Cambria', (('', 'Cambria.ttf'), ('B', 'Cambria-Bold.ttf'),
                         ('I', 'Cambria-Italic.ttf'))),
            ('Calibri', (('', 'Calibri.ttf'), ('B', 'Calibri-Bold.ttf'))),
            ('Trebuchet', (('', 'trebuc.ttf'), ('B', 'trebucbd.ttf'),
                           ('BI', 'trebucbi.ttf'))),
        ):
            for style, fname in styles:
                self.add_font(family, style, os.path.join(FONTS_DIR, fname))
        self.set_margins(25.4, 20.7, 25.4)   # reference: 72pt sides, 20.7mm top
        self.set_auto_page_break(auto=True, margin=22)

    def _apply_branding_watermark(self):
        """Project watermark in the laboratory watermark's position (the
        default is skipped whenever one is configured — see header()).
        Rendering order in header() puts this first so body content stays
        on top."""
        if not self.branding_watermark:
            return
        buf, opacity_pct = self.branding_watermark
        try:
            with self.local_context(fill_opacity=opacity_pct / 100.0,
                                    stroke_opacity=opacity_pct / 100.0):
                buf.seek(0)  # reusable across pages: every header() re-reads
                self.image(buf, x=43.4, y=76.2, w=120.4)
        except Exception as exc:               # noqa: BLE001 — never break a
            logger.error('branding watermark embed failed: %s', exc)

    def header(self):
        # Reference: laboratory watermark centred on the page, 'MTL/NDT/<year>'
        # Times-Bold 16 top-right (right edge 195.5mm), and a filled blue
        # serial box in the top-right corner — on every page, cover included.
        serial, _, lab_ref = self.running_header.partition(' / ')
        self._apply_branding_watermark()
        # Reference: laboratory watermark centred on the page. A project's
        # uploaded watermark REPLACES it (drawing both would stack the
        # default on top at the same position and hide the upload).
        if not self.branding_watermark:
            try:
                self.image(WATERMARK_IMAGE, x=43.4, y=76.2, w=120.4)
            except Exception as exc:               # noqa: BLE001 — never break a
                logger.error('watermark embed failed: %s', exc)   # report render
        self.set_text_color(*INK)
        self.set_font('Times', 'B', 16)
        self.set_xy(self.l_margin, 3.6)
        self.cell(170.1, 8, _latin1(lab_ref or self.running_header), align='R')
        self.set_fill_color(*SERIAL_BOX_FILL)
        self.set_draw_color(*SERIAL_BOX_BORDER)
        self.set_line_width(0.35)
        self.rect(196.9, 1.6, 17.6, 11.2, 'DF')
        self.set_text_color(255, 255, 255)
        self.set_xy(196.9, 1.6)
        self.cell(17.6, 11.2, _latin1(serial), align='C')
        # Restore the document's drawing state.
        self.set_text_color(*INK)
        self.set_fill_color(*INK)
        self.set_draw_color(*INK)
        self.set_line_width(0.2)
        # fpdf2 leaves the cursor wherever header() ends — park it at the
        # top margin so the first body content after ANY page break (auto or
        # manual) is drawn at the margin, never on top of this header.
        self.set_xy(self.l_margin, self.t_margin)

    def footer(self):
        # Reference: plain page number, bottom-centre, Times 12. None on the
        # cover/TOC/summary pages; roman numerals inside the appendix.
        page = self.page_no()
        if self.body_page_offset is None or page <= self.body_page_offset:
            return
        if (self.roman_from_page is not None
                and page == self.roman_from_page - 1):
            return        # the APPENDIX divider page carries no number
        if self.roman_from_page is not None and page >= self.roman_from_page:
            label = _to_roman(page - self.roman_from_page + 1)
        else:
            label = str(page - self.body_page_offset)
        self.set_y(-17)
        self.set_font('Times', '', 12)
        self.set_text_color(*INK)
        self.cell(0, 6, label, align='C')


# ---------------------------------------------------------------------------
# Builder — typewriter layout primitives
# ---------------------------------------------------------------------------
class NDTReportBuilder:
    """Ruled, typewriter-style MTL layout primitives."""

    def __init__(self, running_header):
        self.pdf = MTLReportPDF(running_header)
        self.running_header = running_header

    # ------------------------------------------------------------ cover
    def cover(self, serial, project_name, site_line, client_name, date_line,
              verify_url=None):
        """Reference cover, absolutely positioned at the measured y's: title
        block from 20.6mm (18pt, 7.8mm leading), BY 59.3, laboratory 82.6 /
        90.2, ON 113.7, site lines from 129.3, FOR 152.4, client 28pt from
        173.1 (12.1mm leading), address from 196.7, date line at 256.1; the
        Lagos State coat of arms sits top-left. Site/client/address are
        upper-cased like the reference. When a verification URL is given a
        QR code is drawn bottom-right of the date line — it resolves to the
        platform's public report-verification page for this dossier."""
        pdf = self.pdf
        pdf.add_page()
        # The cover is absolutely positioned at the reference's measured
        # y's (the date line sits at 256.1mm, past the body auto-break
        # limit) — suspend automatic page breaks for the whole cover.
        pdf.set_auto_page_break(False)
        pdf.set_text_color(*INK)
        try:
            pdf.image(COVER_LOGO_IMAGE, x=5.8, y=2.4, w=30)
        except Exception as exc:                # noqa: BLE001 — never break
            logger.error('cover logo embed failed: %s', exc)
        avail = pdf.w - pdf.l_margin - pdf.r_margin

        pdf.set_font('Times', 'B', 18)
        y = 20.6
        for line in ('REPORT ON AN IN-SITU INTEGRITY TEST',
                     '(NON-DESTRUCTIVE) OF COMPRESSIVE',
                     'STRENGTH OF STRUCTURAL ELEMENTS'):
            pdf.set_xy(pdf.l_margin, y)
            pdf.cell(0, 7.8, line, align='C')
            y += 7.8

        pdf.set_xy(pdf.l_margin, 59.3)
        pdf.cell(0, 7.8, 'BY', align='C')
        pdf.set_xy(pdf.l_margin, 82.6)
        pdf.cell(0, 7.8, 'LAGOS STATE MATERIALS TESTING LABORATORY', align='C')
        pdf.set_xy(pdf.l_margin, 90.2)
        pdf.cell(0, 7.8, 'OJODU BERGER, LAGOS.', align='C')

        pdf.set_xy(pdf.l_margin, 113.7)
        pdf.cell(0, 7.8, 'ON', align='C')
        name = _latin1((project_name or 'AN EXISTING SITE').upper())
        name_lines = _wrap_lines(pdf, name, avail)
        y = 129.3
        for ln in name_lines:
            pdf.set_xy(pdf.l_margin, y)
            pdf.cell(0, 7.8, _latin1(ln), align='C')
            y += 7.8

        for_y = max(152.4, y + 10.8)
        pdf.set_xy(pdf.l_margin, for_y)
        pdf.cell(0, 7.8, 'FOR', align='C')

        client_y = max(173.1, for_y + 13)
        pdf.set_font('Times', 'B', 28)
        client = _latin1((client_name or 'NOT PROVIDED').upper())
        y = client_y
        for ln in _wrap_lines(pdf, client, avail):
            pdf.set_xy(pdf.l_margin, y)
            pdf.cell(0, 12, _latin1(ln), align='C')
            y += 12.1

        pdf.set_font('Times', 'B', 18)
        if site_line:
            addr_y = max(196.7, y + 2)
            y = addr_y
            for ln in _wrap_lines(pdf, _latin1(site_line.upper()), avail):
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(0, 7.8, _latin1(ln), align='C')
                y += 7.8

        pdf.set_xy(pdf.l_margin, 256.1)
        pdf.cell(0, 10, _latin1(date_line), align='C')

        # ---- Project branding logo (REFINED EXECUTIVE SUMMARY §2.3):
        # stamped in the chosen corner at the chosen size. The statutory
        # laboratory layout (coat of arms, title block) is unchanged.
        if pdf.branding_logo:
            logo, position, width_mm = pdf.branding_logo
            page_w, page_h = pdf.w, pdf.h
            # Corner coordinates with a 10mm inset; aspect ratio kept.
            try:
                from PIL import Image as PILImage
                logo.seek(0)
                with PILImage.open(logo) as im:
                    aspect = im.height / im.width
                height_mm = width_mm * aspect
                x = (page_w - width_mm - 10.0 if 'right' in position
                     else 10.0)
                y = (page_h - height_mm - 10.0 if 'bottom' in position
                     else 10.0)
                logo.seek(0)
                pdf.image(logo, x=x, y=y, w=width_mm, h=height_mm)
            except Exception as exc:            # noqa: BLE001 — never break
                logger.error('branding logo embed failed: %s', exc)

        # ---- Verification QR (11 Sep 2026, PART B missing item 2): the
        # code resolves to the public verification page for this dossier —
        # its reference and content digest — so a recipient can confirm the
        # report matches the archived original without platform access.
        if verify_url:
            qr_png = _cover_qr_png(verify_url)
            if qr_png is not None:
                try:
                    pdf.image(qr_png, x=170.0, y=250.0, w=24)
                except Exception as exc:        # noqa: BLE001 — never break
                    logger.error('cover QR embed failed: %s', exc)
        pdf.set_auto_page_break(auto=True, margin=22)

    # ------------------------------------------------------- TOC placeholder
    def toc_page(self):
        """Reserve the Table of Contents page; entries are filled
        automatically from the registered sections at output() time.
        insert_toc_placeholder() records the CURRENT page for the TOC and
        then performs its own page break — so the add_page() below puts the
        TOC on page 2 and the cursor ends up on page 3 for the executive
        summary (no blank page in between)."""
        self.pdf.add_page()
        self.pdf.insert_toc_placeholder(self._render_toc, pages=1)

    @staticmethod
    def _render_toc(pdf, outline):
        # CRITICAL: this runs during output(), when no font state is active
        # and the cursor is wherever generation left it — reset both.
        # Reference p2: 'TABLE OF CONTENT' Cambria-Bold 12 centred UNDERLINED
        # (rule under the text only), a right-aligned 'Page' header at
        # 183.7mm / y 30.6, then bold 12 Title-Case entries from y 40.5
        # advancing 9.9mm: main number at the margin WITH a trailing period,
        # sub number 12.7mm in without one, title 25.4mm across, page number
        # right-aligned at ~187mm. The reference TOC carries no 5.x entries
        # and no charts entry. APPENDIX (regular, underlined, one blank
        # advance before it) is followed by its items bold at the margin with
        # roman page ranges and no arabic number.
        pdf.set_xy(pdf.l_margin, pdf.t_margin)
        pdf.set_text_color(*INK)
        pdf.set_font('Cambria', 'BU', 12)
        pdf.cell(0, 9.9, 'TABLE OF CONTENT', align='C',
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', 'B', 12)
        pdf.set_xy(163.7, 30.6)
        pdf.cell(20, 7.5, 'Page', align='R')
        offset = pdf.body_page_offset
        if offset is None:
            offset = outline[0].page_number - 1 if outline else 0
        entries = [s for s in outline
                   if not _latin1(s.name).startswith('BAR CHART')
                   and not (getattr(s, 'level', 0)
                            and s.name.partition(' ')[0].startswith('5.'))]
        y = 40.5
        for idx, section in enumerate(entries):
            name = _latin1(section.name)
            num, _, title = name.partition(' ')
            level = getattr(section, 'level', 0)
            in_appendix = (pdf.roman_from_page is not None
                           and section.page_number >= pdf.roman_from_page)
            if name == 'APPENDIX':
                y += 9.9          # one blank advance before APPENDIX
                pdf.set_font('Cambria', 'U', 12)
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(0, 7.5, 'APPENDIX')
                y += 12.4
                continue
            if in_appendix:
                start_n = section.page_number - pdf.roman_from_page + 1
                end_page = (entries[idx + 1].page_number - 1
                            if idx + 1 < len(entries) else len(pdf.pages))
                # An item that ends on or before its start page still shows
                # a valid (never inverted) single-page range.
                end = _to_roman(max(start_n,
                                    end_page - pdf.roman_from_page + 1))
                disp = APPENDIX_ITEM_TITLES.get(name, name.title())
                pdf.set_font('Cambria', 'B', 12)
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(0, 7.5, f'{disp} ({_to_roman(start_n)}-{end})')
                y += 9.9
                continue
            indent = 12.7 if level else 0
            title_x = pdf.l_margin + 25.4
            avail = (pdf.w - pdf.r_margin - 26) - title_x
            toc_title = TOC_TITLES.get(num) or (title or name).title()
            pdf.set_font('Cambria', 'B', 12)
            pdf.set_xy(pdf.l_margin + indent, y)
            pdf.cell(25.4 - indent, 7.5, f'{num}.' if not level else num)
            lines = _wrap_lines(pdf, toc_title, avail)
            pdf.set_xy(title_x, y)
            pdf.multi_cell(avail, 7.5, _latin1(toc_title))
            pdf.set_xy(172.2, y)
            pdf.cell(15, 7.5, str(section.page_number - offset), align='R')
            y += 9.9 if len(lines) == 1 else len(lines) * 7.5 + 2.4
        pdf.set_xy(pdf.l_margin, pdf.t_margin)

    # ------------------------------------------------------------- sections
    def section(self, number, heading, size=12, centered=False, sub=False,
                underline=True):
        """Registers a TOC entry and outputs the reference's section
        heading: number in regular Cambria at the margin, title in bold
        UNDERLINED starting 63.5mm across — subsections 50.8mm — both on
        the 9.9mm double-spaced leading (heading height 9.9mm lands the
        first body line at the measured 86.7pt). Main sections always start
        a fresh page (reference: one numbered section per page);
        subsections only break near the bottom. ``centered`` renders the
        reference's free-standing centred headings (map page 14pt, charts
        page 18pt) without the number column."""
        if sub:
            if self.pdf.will_page_break(15):
                self.pdf.add_page()
        else:
            self.pdf.add_page()
        if self.pdf.body_page_offset is None:
            # Body page numbering starts at the first numbered section
            # (reference: Introduction = page 1).
            self.pdf.body_page_offset = self.pdf.page_no() - 1
        title_text = f'{number} {heading}'.strip()
        self.pdf.start_section(title_text, level=1 if sub else 0)
        self.pdf.set_text_color(*INK)
        if centered:
            self.pdf.set_font('Cambria', 'BU' if underline else 'B', size)
            self.pdf.multi_cell(0, 9.9, _latin1(heading), align='C',
                                new_x='LMARGIN', new_y='NEXT')
        else:
            if number:
                self.pdf.set_font('Cambria', '', size)
                self.pdf.cell(50.8 if sub else 63.5, 9.9, _latin1(number))
            self.pdf.set_font('Cambria', 'BU' if underline else 'B', size)
            self.pdf.multi_cell(0, 9.9, _latin1(heading),
                                new_x='LMARGIN', new_y='NEXT')
        self.pdf.set_font('Cambria', '', 12)

    def para(self, text, leading=9.9, markdown=False):
        """Body prose, Cambria 12. The reference's dominant leading is
        double-spaced (28pt = 9.9mm); §3.0 literature runs single-spaced
        (16.2pt = 5.7mm) and the §4 field-work pages 21pt = 7.5mm. With
        ``markdown=True`` the text may carry **bold** runs, rendered in
        Cambria-Bold like the reference's emphasised names/dates."""
        pdf = self.pdf
        pdf.set_font('Cambria', '', 12)
        pdf.set_text_color(*INK)
        if not markdown:
            pdf.multi_cell(0, leading, _latin1(text))
        else:
            import re
            for part in re.split(r'(\*\*[^*]+\*\*)', _latin1(text)):
                if not part:
                    continue
                if part.startswith('**') and part.endswith('**'):
                    pdf.set_font('Cambria', 'B', 12)
                    pdf.write(leading, part[2:-2])
                    pdf.set_font('Cambria', '', 12)
                else:
                    pdf.write(leading, part)
            pdf.ln(leading)
        pdf.ln(4 if leading > 8 else 2)

    def heading(self, text, size=12, page_break=True, underline=False):
        """Un-numbered, un-TOC'd heading (executive summary, equipment status,
        integrity block) — the reference's free-standing centred bold-12
        headings; the executive summary is the underlined one."""
        pdf = self.pdf
        if page_break and pdf.will_page_break(20):
            pdf.add_page()
        pdf.set_font('Cambria', 'BU' if underline else 'B', size)
        pdf.set_text_color(*INK)
        pdf.cell(0, 9.9, _latin1(text), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', '', 12)
        pdf.ln(2)

    def subheading(self, text):
        """Bold centred block heading (reference's per-floor result table
        headers, e.g. 'GROUND FLOOR COLUMNS OF ...')."""
        pdf = self.pdf
        if pdf.will_page_break(18):
            pdf.add_page()
        pdf.set_font('Cambria', 'B', 12)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 9.9, _latin1(text), align='C',
                       new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', '', 12)
        pdf.ln(1)

    def display_line(self, text, size=18):
        """Centred bold display line (reference map-page address: Cambria
        bold 18, 8.5mm leading)."""
        pdf = self.pdf
        pdf.set_font('Cambria', 'B', size)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 8.5, _latin1(text), align='C',
                       new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', '', 12)

    def inner_heading(self, text, centered=True, underline=True):
        """Bold free-standing heading inside a section (reference §4.2:
        'NON-DESTRUCTIVE CONCRETE STRENGTH ... DETERMINATION.' centred
        underlined; 'CONCRETE' at the margin, not underlined)."""
        pdf = self.pdf
        if pdf.will_page_break(15):
            pdf.add_page()
        pdf.set_font('Cambria', 'BU' if underline else 'B', 12)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 9.9, _latin1(text),
                       align='C' if centered else 'L',
                       new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', '', 12)

    def note_block(self, text):
        """Reference §7.0 note: 'Note:' Trebuchet-Bold at the margin, the
        note text Trebuchet-BoldItalic 25.4mm across, 5.6mm leading."""
        pdf = self.pdf
        y = pdf.get_y()
        pdf.set_text_color(*INK)
        pdf.set_font('Trebuchet', 'B', 12)
        pdf.set_xy(pdf.l_margin, y)
        pdf.cell(25.4, 5.6, 'Note:')
        pdf.set_font('Trebuchet', 'BI', 12)
        pdf.set_xy(pdf.l_margin + 25.4, y)
        pdf.multi_cell(0, 5.6, _latin1(text),
                       new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Cambria', '', 12)

    def numbered(self, items, num_indent=0.0, text_indent=6.35, leading=9.9):
        """Numbered clause list. Reference §2.0: number at the margin, text
        6.35mm across, 9.9mm leading; §4.0 scope: number 1.6mm in, text
        12.7mm across, 7.5mm leading; §7.0 conclusion: text 12.7mm across,
        7.5mm leading."""
        for i, item in enumerate(items, 1):
            self.pdf.set_font('Cambria', '', 12)
            self.pdf.set_text_color(*INK)
            self.pdf.set_x(self.pdf.l_margin + num_indent)
            self.pdf.cell(max(6.0, text_indent - num_indent), leading,
                          f'{i}.')
            self.pdf.multi_cell(0, leading, _latin1(item),
                                new_x='LMARGIN', new_y='NEXT')

    def lettered(self, items):
        """Lettered observation list (reference §4.1 visual test a., b., c.):
        letter 6.35mm in, text 12.7mm in, 7.5mm leading."""
        for i, item in enumerate(items):
            self.pdf.set_font('Cambria', '', 12)
            self.pdf.set_text_color(*INK)
            self.pdf.set_x(self.pdf.l_margin + 6.35)
            self.pdf.cell(6.35, 7.5, f'{chr(ord("a") + i)}.')
            self.pdf.multi_cell(0, 7.5, _latin1(item),
                                new_x='LMARGIN', new_y='NEXT')

    def centered_formula(self, formula):
        """Centered symbolic formula line (reference §3.0 'UPV = L / t')."""
        self.pdf.ln(2)
        self.pdf.set_font('Cambria', 'B', 13)
        self.pdf.set_text_color(*INK)
        self.pdf.cell(0, 8, _latin1(formula), align='C',
                      new_x='LMARGIN', new_y='NEXT')
        self.pdf.set_font('Cambria', '', 12)
        self.pdf.ln(2)

    def divider_page(self, title):
        """Full-page divider. Reference p13: '5.0' Cambria 21 at 29.4mm
        top-left, the words of the title stacked centred in Cambria-Italic
        20 between 87.3mm and 194.9mm. Reference p38: 'APPENDIX' alone,
        Cambria-Bold 25.2 centred at ~122mm. Registers the section so it
        appears in the TOC, and marks the start of roman page numbering
        when the title is the appendix."""
        pdf = self.pdf
        pdf.add_page()
        pdf.start_section(_latin1(title), level=0)
        pdf.set_text_color(*INK)
        if title.strip() == 'APPENDIX':
            # Reference p38: the divider page itself carries NO page number;
            # roman numbering starts at I on the first content page after.
            pdf.roman_from_page = pdf.page_no() + 1
            pdf.set_y(122)
            pdf.set_font('Cambria', 'B', 25.2)
            pdf.cell(0, 12, 'APPENDIX', align='C')
            pdf.set_font('Cambria', '', 12)
            # The reference's divider pages carry nothing else.
            pdf.add_page()
            return
        words = title.split()
        number, rest = (words[0], words[1:]) if len(words) > 1 else ('', words)
        if number:
            pdf.set_y(29.4)              # reference: '5.0' at 83pt
            pdf.set_font('Cambria', '', 21)
            pdf.cell(0, 10, _latin1(number))
        if rest:
            pdf.set_font('Cambria', 'I', 20)
            top, bottom = 87.3, 194.9    # reference word positions, in mm
            n = len(rest)
            for i, word in enumerate(rest):
                y = (top + (bottom - top) * i / (n - 1) if n > 1
                     else (top + bottom) / 2)
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(0, 10, _latin1(word), align='C')
        pdf.set_font('Cambria', '', 12)
        # The reference's divider pages carry nothing else — start the
        # following content on a fresh page.
        pdf.add_page()

    def signature_lines(self, entries):
        """Two-up dotted signature lines (reference §7.0 sign-off, Trebuchet
        MS bold): dotted line, name 4.9mm under it, registration/role line
        9.8mm under it; the right column starts 124.2mm across. Each entry
        is (label, sublabel) — values are real platform records; absent
        values stay honest dashes."""
        pdf = self.pdf
        pdf.ln(10)
        entries = list(entries) or [('TESTED BY', ''), ('APPROVED BY', '')]
        if len(entries) % 2:
            entries.append(('', ''))
        for i in range(0, len(entries), 2):
            y = pdf.get_y()
            if y > pdf.h - pdf.b_margin - 20:
                pdf.add_page()
                y = pdf.get_y()
            for col, (label, sublabel) in enumerate(entries[i:i + 2]):
                x = pdf.l_margin + col * 124.2
                pdf.set_font('Trebuchet', 'B', 12)
                pdf.set_text_color(*INK)
                pdf.set_xy(x, y)
                # Cell boxes are tighter than the 4.9/9.8mm baselines they
                # carry — only the glyphs matter, and loose boxes trip the
                # overlap guard on adjacent lines.
                pdf.cell(45, 4.4, '…' * 14)
                pdf.set_xy(x, y + 4.9)
                pdf.cell(60, 4.4, _latin1(label))
                pdf.set_xy(x, y + 9.8)
                pdf.cell(60, 4.4, _latin1(sublabel))
            pdf.set_y(y + 18)

    def placeholder_box(self, message, height=80):
        """Ruled placeholder box for an expected-but-missing attachment (e.g.
        site location map) — honest, never fabricated."""
        pdf = self.pdf
        if pdf.get_y() + height > pdf.h - pdf.b_margin:
            pdf.add_page()
        y = pdf.get_y() + 4
        pdf.set_draw_color(*RULE_GREY)
        pdf.set_line_width(0.3)
        pdf.rect(pdf.l_margin, y, pdf.w - pdf.l_margin - pdf.r_margin, height)
        pdf.set_font('Cambria', 'B', 12)
        pdf.set_text_color(*RULE_GREY)
        pdf.set_xy(pdf.l_margin, y + height / 2 - 3)
        pdf.cell(pdf.w - pdf.l_margin - pdf.r_margin, 6,
                 _latin1(message), align='C')
        pdf.set_text_color(*INK)
        pdf.set_line_width(0.2)
        pdf.set_y(y + height + 4)

    def centered_image(self, image_source, max_w=150):
        """Embed an image (map, drawing, photo) at the current position.
        Returns True when embedded."""
        from .services import ReportService
        return bool(ReportService._try_embed_image(self.pdf, image_source,
                                                   w=max_w))

    def photo_caption(self, numeral, caption):
        """'PIC <roman>: <caption>' — bold 12, centred below the photograph
        (reference appendix)."""
        pdf = self.pdf
        pdf.set_font('Cambria', 'B', 12)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 7.5, _latin1(f'PIC {numeral}: {caption}'),
                       align='C', new_x='LMARGIN', new_y='NEXT')

    def bullet(self, text, leading=5.7):
        """Bulleted line — reference: a ~3mm black filled circle at the
        margin, text 7.4mm across, leading matches the surrounding section
        (5.7mm in §3.0, 7.5mm in §4.0)."""
        pdf = self.pdf
        pdf.set_font('Cambria', '', 12)
        pdf.set_text_color(*INK)
        pdf.set_fill_color(*INK)
        y = pdf.get_y()
        pdf.ellipse(pdf.l_margin + 0.2, y + 1.2, 3.0, 3.0, 'F')
        pdf.set_xy(pdf.l_margin + 7.4, y)
        pdf.multi_cell(0, leading, _latin1(text),
                       new_x='LMARGIN', new_y='NEXT')

    def ln_gap(self, height=4):
        self.pdf.ln(height)

    def kv(self, key, value):
        pdf = self.pdf
        pdf.set_font('Cambria', 'B', 12)
        pdf.set_text_color(*INK)
        pdf.cell(60, 6.5, _latin1(key))
        pdf.set_font('Cambria', '', 12)
        pdf.multi_cell(0, 6.5, _latin1(value if value not in (None, '') else '-'),
                       new_x='LMARGIN', new_y='NEXT')

    def kv_table(self, rows):
        """Reference p9 equipment status grid: table runs 36.1 -> 192.5mm,
        columns split at 115.5mm, 7.2mm rows, text centred in both columns
        (Cambria 12). ``rows`` = [(label, value), ...]; every value is a
        real platform record."""
        pdf = self.pdf
        x0, split, x1 = 36.1, 115.5, 192.5
        line_h = 7.2
        label_w = split - x0
        value_w = x1 - split
        pad = 2 * pdf.c_margin
        for label, value in rows:
            pdf.set_font('Cambria', '', 12)
            pdf.set_text_color(*INK)
            label_lines = _wrap_lines(pdf, label, label_w - pad)
            value_lines = _wrap_lines(
                pdf, value if value not in (None, '') else '-', value_w - pad)
            h = max(len(label_lines), len(value_lines), 1) * line_h
            if pdf.get_y() + h > pdf.h - pdf.b_margin:
                pdf.add_page()
            y = pdf.get_y()
            for x, w, lines in ((x0, label_w, label_lines),
                                (split, value_w, value_lines)):
                pdf.set_xy(x, y)
                pdf.cell(w, h, border=1)
                ty = y + (h - len(lines) * line_h) / 2
                for ln in lines:
                    pdf.set_xy(x, ty)
                    pdf.cell(w, line_h, _latin1(ln), align='C')
                    ty += line_h
            # Return the cursor to the margin so following prose starts
            # at the margin, not the table's inset edge.
            pdf.set_xy(pdf.l_margin, y + h)

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
        line_h = 5.5                # reference tables: 11pt text, 15.5pt rows

        def _draw_row(cells, is_header):
            pdf.set_font('Cambria', 'B' if is_header else '', 11)
            pdf.set_text_color(*INK)
            wrapped = [_wrap_lines(pdf, c, w - pad)
                       for c, w in zip(cells, widths)]
            n_lines = max(len(lines) for lines in wrapped)
            row_h = n_lines * line_h
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
        pdf.set_font('Cambria', '', 11)
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
            pdf.set_font('Cambria', 'B', 11)
            pdf.cell(label_w, line_h, _latin1(label))
            pdf.set_font('Cambria', '', 11)
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

    @classmethod
    def _effective_report_number(cls, project, tests):
        """
        The report reference the emitters actually print: the deterministic
        serial unless a saved project CMS override replaces it (11 Sep 2026
        client request — the reference is editable in the CMS). Both the PDF
        and the archive must resolve through this, or the archived
        reference and the printed/QR-digested one would diverge.
        """
        report_no, year = cls._report_number(project, tests)
        ref, _src = get_cms_text(project, 'report_reference',
                                 computed={'report_reference': report_no})
        return ((ref or report_no).strip(), year)

    @staticmethod
    def _content_hash(parts):
        payload = '|'.join(str(p) for p in parts)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @classmethod
    def _generate_charts(cls, element_data):
        """
        The reference's two separate stacked GOOD/POOR bar charts (p33):
        A — number of structural members tested per category;
        B — strength of structural members tested in percentage %.
        Categories derive from each element's real floor + member type
        (reference GFC/FFC/FFB/FFS style). Returns (png buffer, png buffer).
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import io

        plt.rcParams['font.family'] = 'Calibri'

        categories = {}
        code_labels = {}
        for e in element_data:
            code = cls._floor_code(e['floor_label']) + cls._member_code(e['member_type'])
            good, poor = categories.get(code, (0, 0))
            if e['remark'] == 'GOOD':
                good += 1
            elif e['remark'] == 'POOR':
                poor += 1
            categories[code] = (good, poor)
            code_labels.setdefault(
                code, f'{e["floor_label"]} {e["member_type"]}'.title())
        codes = sorted(categories)
        goods = [categories[c][0] for c in codes]
        poors = [categories[c][1] for c in codes]
        totals = [g + p for g, p in zip(goods, poors)]
        good_c = tuple(c / 255 for c in CHART_GOOD)
        poor_c = tuple(c / 255 for c in CHART_POOR)

        # Chart A — reference figure 4.93 x 3.0 in.
        fig, ax = plt.subplots(figsize=(4.93, 3.0))
        ax.bar(codes, goods, color=good_c, label='GOOD')
        ax.bar(codes, poors, bottom=goods, color=poor_c, label='POOR')
        ax.set_title('BAR CHART A', fontsize=14)
        ax.set_ylabel('NUMBER OF STRUCTURAL MEMBERS TESTED', fontsize=10)
        ax.set_xlabel('STRUCTURAL MEMBERS', fontsize=10)
        ax.tick_params(labelsize=9)
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.40), ncol=2,
                  frameon=False, fontsize=9)
        for spine in ax.spines.values():
            spine.set_linewidth(0.75)
        fig.subplots_adjust(top=0.83, bottom=0.30, left=0.14, right=0.97)
        buf_a = io.BytesIO()
        fig.savefig(buf_a, format='png', dpi=150)
        buf_a.seek(0)
        plt.close(fig)

        # Chart B — reference figure 4.93 x 3.52 in, y-axis 0-100% step 10.
        pct_good = [100 * g / t if t else 0 for g, t in zip(goods, totals)]
        pct_poor = [100 * p / t if t else 0 for p, t in zip(poors, totals)]
        fig, ax = plt.subplots(figsize=(4.93, 3.52))
        ax.bar(codes, pct_good, color=good_c, label='GOOD')
        ax.bar(codes, pct_poor, bottom=pct_good, color=poor_c, label='POOR')
        ax.set_title('BAR CHART B', fontsize=14)
        ax.set_ylabel('STRENGTH OF STRUCTURAL MEMBERS TESTED\n'
                      'IN PERCENTAGE %', fontsize=10)
        ax.set_xlabel('STRUCTURAL MEMBERS', fontsize=10)
        ax.set_ylim(0, 100)
        ax.set_yticks(range(0, 101, 10))
        ax.set_yticklabels([f'{v}%' for v in range(0, 101, 10)])
        ax.tick_params(labelsize=9)
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.35), ncol=2,
                  frameon=False, fontsize=9)
        for spine in ax.spines.values():
            spine.set_linewidth(0.75)
        fig.subplots_adjust(top=0.86, bottom=0.26, left=0.16, right=0.97)
        buf_b = io.BytesIO()
        fig.savefig(buf_b, format='png', dpi=150)
        buf_b.seek(0)
        plt.close(fig)
        return buf_a, buf_b

    @staticmethod
    def _generate_bim_plan_view(project):
        """
        Plan-view drawing of the project's imported BIM model, rendered with
        matplotlib from the same tessellated geometry the data-collection 3D
        preview shows — so the report's site plan follows whichever model was
        imported for the project. Returns (png buffer, caption); (None, '')
        when the project has no imported model geometry (nothing is drawn
        from made-up coordinates).
        """
        from apps.digital_eye.models import BIMModelGeometry
        geo = BIMModelGeometry.objects.filter(project=project).first()
        if geo is None or not geo.elements:
            return None, ''

        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.collections import PolyCollection
        import io

        # elements: [{"guid", "name", "type", "verts": [x,y,z,...],
        #             "faces": [i,j,k,...]}, ...] — project onto the plan
        # (x, y) plane and draw each triangle of every mesh.
        polys = []
        for el in geo.elements:
            verts = el.get('verts') or []
            faces = el.get('faces') or []
            pts = [(verts[i], verts[i + 1])
                   for i in range(0, len(verts) - 2, 3)]
            for f in range(0, len(faces) - 2, 3):
                tri = [pts[faces[f + k]] for k in (0, 1, 2)
                       if faces[f + k] < len(pts)]
                if len(tri) == 3:
                    polys.append(tri)
        if not polys:
            return None, ''

        xs = [p[0] for tri in polys for p in tri]
        ys = [p[1] for tri in polys for p in tri]
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            return None, ''      # degenerate footprint — nothing to draw

        fig, ax = plt.subplots(figsize=(8.2, 6.2))
        ax.add_collection(PolyCollection(
            polys, facecolor='#d8dee9', edgecolor='#222222', linewidth=0.12))
        pad_x = 0.02 * (max(xs) - min(xs)) or 1.0
        pad_y = 0.02 * (max(ys) - min(ys)) or 1.0
        ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
        ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
        ax.set_aspect('equal')
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_linewidth(0.4)
        ax.set_title('SITE PLAN — IMPORTED BIM MODEL', fontsize=10)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)

        source = geo.source_file or 'imported BIM model'
        caption = (
            f"Plan generated from the project's imported BIM model "
            f'({source}, {geo.element_count or len(geo.elements)} elements) '
            '— a plan projection of the model geometry, not a georeferenced '
            'survey map.'
        )
        return buf, caption

    @staticmethod
    def _generate_bim_floor_plans(project, floor_labels):
        """
        Per-floor plan views (7 Sep review, item 7): one top-down drawing
        per tested floor, rendered from the imported model's tessellated
        geometry and grouped by the element levels recorded in the BIM
        mappings. Returns a list of (floor_label, png buffer, caption);
        empty when the model's levels cannot support the split — the
        caller then keeps the single whole-model plan, honestly. No floor
        is ever drawn from invented geometry.
        """
        from apps.digital_eye.models import (BIMModelGeometry,
                                             BIMElementMapping)
        try:
            geo = BIMModelGeometry.objects.get(project=project)
        except BIMModelGeometry.DoesNotExist:
            return []
        if not geo.elements:
            return []

        level_by_guid = {}
        for m in BIMElementMapping.objects.filter(
                project=project).exclude(bim_guid=''):
            if (m.level or '').strip():
                level_by_guid[m.bim_guid] = m.level.strip()

        # Geometry elements grouped by their mapped level name.
        by_level = {}
        for el in geo.elements:
            level = level_by_guid.get(el.get('guid') or '')
            if level:
                by_level.setdefault(level, []).append(el)
        if len(by_level) < 2:
            return []    # a single-level split adds nothing over the whole plan

        def _norm(s):
            return ' '.join((s or '').lower().split())

        level_keys = {level: _norm(level) for level in by_level}

        # Match each tested floor label to a recorded level: exact
        # normalised match first, then a containment match. Unmatched
        # floors are skipped — their level cannot be identified honestly.
        wanted = []
        seen_levels = set()
        for label in floor_labels:
            key = _norm(label)
            if not key or key == 'floor not recorded':
                continue
            match = next((lvl for lvl, norm in level_keys.items()
                          if norm == key), None)
            if match is None:
                match = next((lvl for lvl, norm in level_keys.items()
                              if key in norm or norm in key), None)
            if match and match not in seen_levels:
                seen_levels.add(match)
                wanted.append((label, match))
        if not wanted:
            return []

        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.collections import PolyCollection
        import io

        source = geo.source_file or 'imported BIM model'
        plans = []
        for floor_label, level in wanted:
            polys = []
            for el in by_level[level]:
                verts = el.get('verts') or []
                faces = el.get('faces') or []
                pts = [(verts[i], verts[i + 1])
                       for i in range(0, len(verts) - 2, 3)]
                for f in range(0, len(faces) - 2, 3):
                    tri = [pts[faces[f + k]] for k in (0, 1, 2)
                           if faces[f + k] < len(pts)]
                    if len(tri) == 3:
                        polys.append(tri)
            if not polys:
                continue
            xs = [p[0] for tri in polys for p in tri]
            ys = [p[1] for tri in polys for p in tri]
            if len(set(xs)) < 2 or len(set(ys)) < 2:
                continue    # degenerate footprint — skip this level
            fig, ax = plt.subplots(figsize=(8.2, 6.2))
            ax.add_collection(PolyCollection(
                polys, facecolor='#d8dee9', edgecolor='#222222',
                linewidth=0.12))
            pad_x = 0.02 * (max(xs) - min(xs)) or 1.0
            pad_y = 0.02 * (max(ys) - min(ys)) or 1.0
            ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
            ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
            ax.set_aspect('equal')
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_linewidth(0.4)
            ax.set_title(f'FLOOR PLAN — {level.upper()}', fontsize=10)
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close(fig)
            caption = (
                f'Floor plan projected from the imported BIM model '
                f'({source}, level "{level}", '
                f'{len(by_level[level])} elements) — a plan projection of '
                'the model geometry, not a construction drawing.'
            )
            plans.append((floor_label, buf, caption))
        return plans

    @classmethod
    def _google_static_map(cls, project):
        """A 500 m radius Google Maps static-image around the project's
        recorded GNSS coordinates (review meeting C6), fetched with the
        server's GOOGLE_MAPS_API_KEY when one is configured. Returns a
        BytesIO (PNG) or None — no coordinates or no key means no map, and
        the caller falls through to the honest fallbacks. A failed fetch
        also returns None (never a fabricated map)."""
        import io
        import urllib.parse
        import urllib.request
        lat = getattr(project, 'latitude', None)
        lon = getattr(project, 'longitude', None)
        if lat is None or lon is None:
            return None
        from django.conf import settings as dj_settings
        api_key = getattr(dj_settings, 'GOOGLE_MAPS_API_KEY', '') or ''
        if not api_key:
            return None
        # ~500 m radius at Lagos latitudes: 0.0045° latitude span.
        params = urllib.parse.urlencode({
            'center': f'{lat},{lon}',
            'zoom': '17',
            'size': '640x640',
            'scale': '2',
            'maptype': 'roadmap',
            'markers': f'color:red|{lat},{lon}',
            'key': api_key,
        })
        url = f'https://maps.googleapis.com/maps/api/staticmap?{params}'
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0'})
            return io.BytesIO(urllib.request.urlopen(req, timeout=15).read())
        except Exception as exc:            # noqa: BLE001 — no map, honestly
            logger.error('Google static map fetch failed: %s', exc)
            return None

    @staticmethod
    def _maps_link(project):
        """The Google Maps link for the site (C6): only when the project
        carries real GNSS coordinates — never a guessed location."""
        lat = getattr(project, 'latitude', None)
        lon = getattr(project, 'longitude', None)
        if lat is None or lon is None:
            return None
        return (f'https://www.google.com/maps/search/?api=1&query={lat},{lon}')

    @staticmethod
    def _boxed_image(pdf, source, x, y, w, h):
        """Embed an image inside a fixed box (reference map page's two
        side-by-side image frames), preserving aspect ratio. ``source`` is a
        URL, a '/media/...' path, a filesystem path or an image buffer.
        Returns True when embedded."""
        import io
        if not source:
            return False
        try:
            if isinstance(source, str) and source.startswith('/media/'):
                from django.conf import settings
                source = os.path.join(settings.MEDIA_ROOT,
                                      source[len('/media/'):])
            elif isinstance(source, str) and source.startswith('http'):
                import urllib.request
                req = urllib.request.Request(
                    source, headers={'User-Agent': 'Mozilla/5.0'})
                source = io.BytesIO(
                    urllib.request.urlopen(req, timeout=15).read())
            pdf.image(source, x=x, y=y, w=w, h=h, keep_aspect_ratio=True)
            return True
        except Exception as exc:            # noqa: BLE001 — a missing image
            logger.error('boxed image embed failed: %s', exc)
            return False

    @staticmethod
    def _bim_capture_url(project):
        """URL of the operator's captured screenshot of the BIM model 3D
        preview — a project-level SensorDataFile photo whose description
        marks it as a BIM 3D model view. The most recent capture wins;
        None when no capture exists."""
        from apps.digital_eye.models import SensorDataFile
        capture = (SensorDataFile.objects
                   .filter(project=project, file_type='photo',
                           description__icontains='bim 3d model view')
                   .order_by('-created_at').first())
        if not capture:
            return None
        try:
            return capture.file.url or None
        except Exception:                      # noqa: BLE001 — remote storage
            return None

    @classmethod
    def _embed_bim_plan(cls, builder, project, x=34.9, y=26.8,
                        w=146.3, h=101.6):
        """Render and embed the BIM model plan view (appendix drawing) at
        the reference's drawing-image slot (x 34.9mm, first image at
        y 26.8mm, ~146.3mm wide). Returns True when a drawing was
        embedded."""
        try:
            buf, _caption = cls._generate_bim_plan_view(project)
        except Exception as exc:            # noqa: BLE001 — drawing must never
            logger.error(                    # break the report
                'BIM plan view rendering failed: %s', exc)
            return False
        if not buf:
            return False
        # fpdf2's keep_aspect_ratio CENTRES the image in the w×h box; the
        # reference's drawing images are top-anchored, so shrink the box
        # height to the image's fitted height before placing it.
        try:
            from PIL import Image
            with Image.open(buf) as im:
                aspect = im.width / im.height
            if aspect:
                h = min(h, w / aspect)
        except Exception:   # noqa: BLE001 — fall back to the slot as given
            pass
        try:
            buf.seek(0)
        except Exception:   # noqa: BLE001 — not all sources are seekable
            pass
        return bool(cls._boxed_image(builder.pdf, buf, x, y, w, h))

    @classmethod
    def _embed_bim_floor_plans(cls, builder, project, floor_labels):
        """
        One appendix page per tested floor (7 Sep review, item 7): each
        page carries the floor label and that level's plan view. Returns
        the number of plans embedded; 0 leaves the report exactly as it
        was (the single whole-model plan stands)."""
        try:
            plans = cls._generate_bim_floor_plans(project, floor_labels)
        except Exception as exc:        # noqa: BLE001 — drawing must never
            logger.error(               # break the report
                'BIM floor plan rendering failed: %s', exc)
            return 0
        pdf = builder.pdf
        embedded = 0
        for floor_label, buf, caption in plans:
            pdf.add_page()
            pdf.start_section('FLOOR PLANS', level=1)
            pdf.set_font('Cambria', '', 12)
            pdf.set_text_color(*INK)
            pdf.set_xy(pdf.l_margin, 16.9)
            pdf.cell(0, 7.5,
                     _latin1(f'{project.name} — {floor_label.title()}'),
                     new_x='LMARGIN', new_y='NEXT')
            # Same top-anchored aspect handling as the whole-model plan:
            # shrink the slot height to the image's fitted height.
            w, h = 146.3, 101.6
            try:
                from PIL import Image
                with Image.open(buf) as im:
                    aspect = im.width / im.height
                if aspect:
                    h = min(h, w / aspect)
            except Exception:   # noqa: BLE001 — fall back to slot as given
                pass
            try:
                buf.seek(0)
            except Exception:   # noqa: BLE001 — not all sources are seekable
                pass
            if cls._boxed_image(pdf, buf, 34.9, 26.8, w, h):
                embedded += 1
                builder.para(caption, leading=6.5)
        return embedded

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

    # ------------------------------------------------ element classification
    @staticmethod
    def _member_type(element):
        """Column / Beam / Slab / Wall / Foundation ... from the element's
        own label (e.g. 'COL-C24' -> COLUMN, 'Floor:200THK RC SLAB:781094'
        -> SLAB). Real data only — nothing is re-typed."""
        label = (element or '').strip().upper()
        if not label:
            return 'UNSPECIFIED'
        token = re.split(r'[-\s]', label)[0]
        mapping = {
            'COL': 'COLUMN', 'COLUMN': 'COLUMN', 'CS': 'COLUMN',
            'BM': 'BEAM', 'BEAM': 'BEAM',
            'SL': 'SLAB', 'SLAB': 'SLAB',
            'WL': 'WALL', 'WALL': 'WALL',
            'FDN': 'FOUNDATION', 'FOUNDATION': 'FOUNDATION',
        }
        if token in mapping:
            return mapping[token]
        # BIM-style names ('M_Footing-Rectangular:900 x 900 x 200mm:803711',
        # 'Floor:200THK RC SLAB:781094') carry the member word anywhere in
        # the label — find it instead of printing a raw name fragment like
        # 'M_FOOTING' or 'FLOOR:200THK' in the group headings.
        for word, member in (
                ('COLUMN', 'COLUMN'), ('BEAM', 'BEAM'), ('SLAB', 'SLAB'),
                ('WALL', 'WALL'), ('FOOTING', 'FOUNDATION'),
                ('FOUNDATION', 'FOUNDATION'), ('PILE', 'FOUNDATION'),
                ('STAIR', 'STAIR')):
            if word in label:
                return member
        return token or 'UNSPECIFIED'

    @staticmethod
    def _floor_label(floor):
        return (floor or '').strip() or 'FLOOR NOT RECORDED'

    @staticmethod
    def _floor_code(floor_label):
        """Chart category prefix (reference GFC/FFC/FFB/FFS style)."""
        f = (floor_label or '').lower()
        if 'ground' in f:
            return 'GF'
        if 'first' in f or '1st' in f:
            return 'FF'
        if 'second' in f or '2nd' in f:
            return 'SF'
        if 'third' in f or '3rd' in f:
            return 'TF'
        if 'roof' in f:
            return 'RF'
        if 'not recorded' in f:
            return 'UF'
        return ''.join(w[0] for w in f.split()[:2]).upper() or 'UF'

    @staticmethod
    def _member_code(member_type):
        return {'COLUMN': 'C', 'BEAM': 'B', 'SLAB': 'S', 'WALL': 'W',
                'FOUNDATION': 'F'}.get(member_type, member_type[:1] or 'X')

    @classmethod
    def _element_data(cls, pulse_tests):
        """
        One verdict block per tested element from its real A/B/C readings:
        mean pulse velocity, mean E.C.S and the GOOD/POOR remark at the
        statutory 25 N/mm2 threshold.
        """
        out = []
        for t in pulse_tests:
            rows = t.reading_rows()
            velocities = [r['velocity_km_s'] for r in rows
                          if r['velocity_km_s'] is not None]
            mean_v = (sum(velocities) / len(velocities)) if velocities else None
            # Nexucon Link (8 Sep meeting): the E.C.S flows through the
            # project's active calibration curve — never a single fixed
            # formula. Same path the platform computes every E.C.S with.
            from apps.digital_eye.strength_curves import apply_active_curve
            if mean_v is None:
                mean_ecs = None
            else:
                mean_ecs, _curve_snapshot = apply_active_curve(
                    t.project, mean_v,
                    rebound_number=t.rebound_number,
                    temperature_c=t.surface_temperature_c)
            remark = ('GOOD' if mean_ecs is not None and mean_ecs >= 25.0
                      else 'POOR' if mean_ecs is not None else 'NOT ASSESSED')
            # Within-element spread (7 Sep meeting: the client wants the
            # ±variance between a member's points visible, not silently
            # averaged). Spread > 2% of the mean flags the remark.
            spread_km_s = (max(velocities) - min(velocities)
                           if len(velocities) > 1 else None)
            spread_pct = (spread_km_s / mean_v * 100
                          if spread_km_s is not None and mean_v else None)
            out.append({
                'test': t,
                'element': t.structural_element or 'UNSPECIFIED',
                'member_type': cls._member_type(t.structural_element),
                'floor_label': cls._floor_label(t.floor),
                'rows': rows,
                'mean_v': mean_v,
                'mean_ecs': mean_ecs,
                'remark': remark,
                'n_points': len(rows),
                'spread_km_s': spread_km_s,
                'spread_pct': spread_pct,
            })
        return out

    @classmethod
    def _worked_example(cls, project, element_data, active_curve=None):
        """
        The hand-recomputable arithmetic for the first element tested
        (7 Sep meeting: every figure must be recomputable by hand). Shared
        by the PDF renderer and the Word export so the two can never show
        different working. Returns None when no element carries a velocity.
        """
        if not element_data:
            return None
        if active_curve is None:
            from apps.digital_eye.strength_curves import resolve_active_curve
            try:
                active_curve = resolve_active_curve(project)
            except Exception:  # noqa: BLE001 — example must never kill export
                active_curve = None
        e0 = element_data[0]
        pts = [r for r in e0['rows']
               if r['velocity_km_s'] is not None]
        if not pts:
            return None
        parts = [
            f"V{r['label']} = {r['path_mm']:g} mm / "
            f"{r['transit_us']:.1f} us = "
            f"{r['velocity_km_s'] * 1000:.2f} m/s"
            for r in pts
        ]
        example = (f"Worked example (first element tested, "
                   f"{e0['element']}): "
                   + '; '.join(parts) + '.')
        if e0['mean_v'] is not None:
            example += (f" V(element) = mean of {len(pts)} point"
                        f"{'s' if len(pts) != 1 else ''} = "
                        f"{e0['mean_v'] * 1000:.2f} m/s "
                        f"({e0['mean_v']:.3f} km/s).")
            if e0['mean_ecs'] is not None:
                if (active_curve is not None
                        and active_curve.project_id
                        and active_curve.curve_type == 'linear'):
                    # Project calibration: substitute its real
                    # parameters (V in m/s).
                    p = active_curve.formula_params or {}
                    sign = '-' if p.get('c', 0) < 0 else '+'
                    example += (f" f_cu = {p.get('m', 0):g} x "
                                f"{e0['mean_v'] * 1000:.2f} {sign} "
                                f"{abs(p.get('c', 0)):g} = "
                                f"{e0['mean_ecs']:.2f} N/mm2 "
                                "(V in m/s).")
                elif (active_curve is not None
                      and active_curve.project_id):
                    example += (f" f_cu from the project calibration "
                                f"above at V = "
                                f"{e0['mean_v'] * 1000:.2f} m/s = "
                                f"{e0['mean_ecs']:.2f} N/mm2.")
                else:
                    example += (f" f_cu = 8.961 x {e0['mean_v']:.3f} "
                                f"- 7.97 = {e0['mean_ecs']:.2f} "
                                f"N/mm2.")
        return example

    # ---------------------------------------------------------------
    # Generated-content CMS bodies (11 Sep 2026 client request): the
    # computed wording of the report, pre-filled into the CMS so the
    # operator can review and reword everything before generating. A key
    # maps to None when the project lacks the recorded data that section
    # states — the CMS then refuses edits rather than let an override
    # invent results (no-fabrication rule).
    # ---------------------------------------------------------------
    @classmethod
    def _computed_bodies(cls, project, *, tests, rebar_tests, element_data,
                         good_members, poor_members, visual_notes,
                         floors_present, bim_levels, has_drawings, tested,
                         same_day, date_min, date_max):
        bodies = {}

        # -- report reference (11 Sep client request): the deterministic
        # serial, editable per project through the CMS. Every render site
        # (cover serial box, running header, archive record, QR verify
        # digest) resolves through this body, so the CMS pre-fill, the PDF
        # and the Word export can never disagree.
        bodies['report_reference'] = cls._report_number(project, tests)[0]

        # -- executive summary (needs test results: it states them)
        if element_data:
            storeys = cls._storey_count(floors_present, bim_levels)
            building = (f'an existing {storeys}-floor building' if storeys
                        else 'an existing building')
            proj = _latin1(project.name or 'unnamed project').replace('*', '')
            client = _latin1(project.client_name or '').replace('*', '')
            site = _latin1(', '.join(
                p for p in (project.site_address, project.lga, project.state)
                if p)).replace('*', '')
            prose_dates = (_prose_date(date_max) if same_day
                           else f'{_prose_date(date_min)} and '
                                f'{_prose_date(date_max)}')
            para1 = (
                'In situ Integrity Test (Non-Destructive) of compressive '
                f'strength of structural members of {building} '
                f'("**{proj}**")'
                + (f' belonging to **{client}**' if client else '')
                + (f', at **{site}**' if site else '')
                + '.'
            )
            para2 = (
                'The Non-Destructive Integrity Test was carried out '
                + (f'on **{prose_dates}**' if tested
                   else 'on dates not recorded')
                + ' with the intention to determine the residual compressive '
                  'strength of concrete component of the structural members '
                  'considered to be critical to stability, robustness and '
                  'general safety of the entire structure in its present '
                  'state.'
            )
            if visual_notes:
                visual_sentence = (
                    'The visual inspection revealed structural defects as '
                    'recorded in Section 4.1 of this report')
            else:
                visual_sentence = (
                    'The visual inspection did not record any structural '
                    'defects')
            analysis_sentence = (
                'and the Non-Destructive test analysis shows that '
                f'{len(good_members)} of the {len(element_data)} structural '
                'members tested were good in strength (average compressive '
                'strength at or above the assumed 25 N/mm2)'
                + (f', while {len(poor_members)} member(s) fell below it and '
                   'require technical advice.' if poor_members
                   else ' at the time of test.'))
            if has_drawings:
                arrangement_sentence = (
                    'The general structural arrangement of the building was '
                    'referenced from the structural information available on '
                    'the platform (reproduced in the Appendix of this report).')
            else:
                arrangement_sentence = (
                    'The general structural arrangement of the buildings '
                    'could not be completely ascertained; as no structural '
                    'drawing was provided.')
            para3 = ' '.join([visual_sentence, analysis_sentence,
                              arrangement_sentence])
            para4 = (
                'In view of the above, it is advised that a qualified '
                'structural engineer should be engaged to proffer solution '
                'to the defects observed, give technical advice on the poor '
                'structural members tested and further analyse the '
                'structural arrangement to guarantee the stability, '
                'integrity and the serviceability of the structure.'
            )
            bodies['executive_summary'] = '\n\n'.join(
                [para1, para2, para3, para4])

        # -- introduction, computed project paragraphs (always available)
        site_line = ', '.join(
            p for p in (project.site_address, project.lga, project.state)
            if p) or 'site address not recorded'
        intro_1 = (
            'In compliance with the Mandatory Non-Destructive Test '
            'requirement of the Lagos State Government, a Non-Destructive '
            'compressive strength test (Structural Integrity Test) was '
            f'conducted on the project "{project.name or "-"}"'
            + (f' for {project.client_name}' if project.client_name else '')
            + f' located at {site_line}. The map showing the exact location '
              'of the site is on the Location Map page of this report.'
        )
        if has_drawings:
            intro_2 = (
                'The drawings of the building available on the platform are '
                'reproduced in the Appendix of this report. Structural '
                'drawing was not provided to the platform; therefore, '
                '**assumed strength of 25N/mm2** was used for the analysis '
                'of the structural elements.')
        else:
            intro_2 = (
                'No structural drawing was provided on the platform for this '
                'project. Therefore, **assumed strength of 25N/mm2** was '
                'used for the analysis of the structural elements.')
        bodies['introduction_project'] = '\n\n'.join([intro_1, intro_2])

        # -- visual observations (field data; only editable when recorded)
        if visual_notes:
            bodies['visual_observations'] = '\n'.join(visual_notes)

        # -- methodology equipment paragraphs (always available)
        method_1 = (
            'This test is determined by using the Portable Ultrasonic '
            'Non-Destructive Digital Indicating Tester (PUNDIT)'
            + (' and Profoscope' if rebar_tests else '')
            + '. Non-Destructive, as the name implies, means that the '
              'materials being tested are not damaged during the test.'
        )
        method_2 = (
            'In the Non-Destructive Test, some properties of concrete'
            + ('and Rebar (the reinforcing steel used as rod in concrete to '
               'give additional strength)' if rebar_tests else '')
            + ' were measured. These were used to estimate the strength of '
              'the concrete, its elastic behavior and durability, hence '
              'determining the integrity of the structural member.'
        )
        bodies['methodology_equipment'] = '\n\n'.join([method_1, method_2])

        # -- rebar statement (only editable when a survey was recorded; the
        #    honest "Not Applicable" wording stays fixed otherwise)
        if rebar_tests:
            bodies['rebar_statement'] = (
                'During the testing, Profoscope was used to check the cover '
                'depth of the reinforcement (concrete cover), locate the '
                'Rebar\'s exact position within the structural member and '
                'the estimated diameter of the Rebar.'
            )

        # -- findings statement (needs results: it states them)
        if element_data:
            strength_sentence = (
                f'the test analysis revealed that {len(good_members)} of the '
                f'{len(element_data)} structural members tested in the '
                f'building were good in strength at the time of test'
                + (f', while {len(poor_members)} fell below the statutory '
                   f'25 N/mm2 strength and require technical advice'
                   if poor_members else '')
                + '.'
            )
            advice_sentence = (
                'It is advised that '
                + (project.client_name.upper() if project.client_name
                   else 'the client')
                + ' engage a qualified structural engineer and other '
                  'relevant professionals in the built environment to '
                  'proffer solution to the defects observed, technical '
                  'advice on the poor structural members tested and further '
                  'analyse the structural arrangement to guarantee the '
                  'stability, integrity and the serviceability of the '
                  'building.'
            )
            bodies['findings_statement'] = (strength_sentence + ' '
                                            + advice_sentence)

        # -- conclusion items (needs results: they state percentages)
        if element_data:
            total = len(element_data)
            good_pct = round(len(good_members) * 100 / total, 1)
            bodies['conclusion_items'] = '\n'.join([
                'The Non-Destructive Test analysis as shown in the summary '
                'of test result (Section 5.0) shows the percentage of '
                f'strength for the structural elements tested in the '
                f'building: {len(good_members)} of {total} elements '
                f'({good_pct}%) attained the assumed 25 N/mm2 strength at '
                'the time of test'
                + (f', while {len(poor_members)} '
                   f'element{"s" if len(poor_members) != 1 else ""} '
                   f'({round(len(poor_members) * 100 / total, 1)}%) fell '
                   'below it.' if poor_members else '.'),
                'However, it is imperative to state clearly that '
                'non-adherence to the recommendation excludes the testing '
                'laboratory of any responsibility.',
            ])

        return bodies

    @classmethod
    def computed_section_bodies(cls, project):
        """
        The generated-content CMS bodies for a project, gathered from the
        recorded data — the same queries and the same wording the report
        renders, so the CMS pre-fills with exactly what Generate will
        print. Used by the CMS API view and the Word export.
        """
        from apps.digital_eye.models import PUNDITTest, RebarTest
        from apps.digital_eye.models import BIMElementMapping
        tests = list(
            PUNDITTest.objects
            .filter(project=project)
            .select_related('device', 'operator')
            .prefetch_related('files', 'readings')
            .order_by('structural_element', 'tested_at')
        )
        rebar_tests = list(
            RebarTest.objects.filter(project=project).order_by('recorded_at')
        )
        pulse_tests = [t for t in tests if t.test_type == 'pulse_velocity']
        element_data = cls._element_data(pulse_tests)
        visual_notes = cls._visual_observations(tests)
        floors_present = sorted({e['floor_label'] for e in element_data})
        bim_levels = sorted(set(
            BIMElementMapping.objects.filter(project=project)
            .exclude(level='').values_list('level', flat=True)))
        has_drawings = BIMElementMapping.objects.filter(
            project=project).exists()
        tested = [t.tested_at for t in tests if t.tested_at]
        same_day = bool(tested) and min(tested).date() == max(tested).date()
        if tested:
            date_min, date_max = min(tested).date(), max(tested).date()
        else:
            date_min = date_max = datetime.now().date()
        return cls._computed_bodies(
            project, tests=tests, rebar_tests=rebar_tests,
            element_data=element_data,
            good_members=[e for e in element_data if e['remark'] == 'GOOD'],
            poor_members=[e for e in element_data if e['remark'] == 'POOR'],
            visual_notes=visual_notes, floors_present=floors_present,
            bim_levels=bim_levels, has_drawings=has_drawings, tested=tested,
            same_day=same_day, date_min=date_min, date_max=date_max)

    @staticmethod
    def _f1(value):
        """One-decimal figure (E.C.S, transit times)."""
        return '-' if value is None else f'{value:.1f}'

    @staticmethod
    def _f2(value):
        """Two-decimal figure — pulse velocities print in m/s (client
        decision, 7 Sep 2026: 4285.71, decimal points, never commas)."""
        return '-' if value is None else f'{value:.2f}'

    @staticmethod
    def _fms(velocity_km_s):
        """km/s -> m/s display string (4.28571 -> '4285.71')."""
        return '-' if velocity_km_s is None else f'{velocity_km_s * 1000:.2f}'

    @staticmethod
    def _fp(value):
        """Path length like the reference: 120 (no trailing .0)."""
        return '-' if value is None else f'{value:g}'

    @staticmethod
    def _roman(n):
        return _to_roman(n)

    @classmethod
    def _statutory_digest(cls, project, tests, report_no):
        """SHA-256 over the underlying test identifiers, results AND reading
        rows — printed in the PDF and keyed by the archive, so "archived
        once" and "digest in the document" always agree."""
        reading_material = [
            (str(t.id), [(r['label'], r['transit_us'], r['path_mm'])
                         for r in t.reading_rows()])
            for t in tests
        ]
        return cls._content_hash(
            ['ndt', project.id, sorted(str(t.id) for t in tests),
             len(tests),
             [round(cls._velocity(t), 6) if cls._velocity(t) is not None
              else None for t in tests],
             reading_material,
             report_no])

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

        tests = list(PUNDITTest.objects.filter(project=project)
                     .prefetch_related('readings'))
        # The effective reference (CMS override honoured) — the archived
        # reference and content_key must match what the report printed and
        # what the cover QR digests, never the un-overridden serial.
        report_no, _ = cls._effective_report_number(project, tests)
        # Exactly the parts the report's own integrity section hashes, so
        # "archived once" and "digest printed in the PDF" always agree.
        content_key = cls._statutory_digest(project, tests, report_no)
        existing = ArchivedReport.objects.filter(
            project=project, report_kind='ndt', content_key=content_key,
        ).first()
        if existing is not None:
            return existing

        # Same strength basis as the report itself: a test is
        # strength-assessed when the project's active calibration curve
        # yields an E.C.S for its velocity; passing means fcu >= 25 MPa.
        from apps.digital_eye.strength_curves import apply_active_curve
        assessed = 0
        passed = 0
        for t in tests:
            velocity = t.velocity_km_s
            if velocity is None:
                from apps.digital_eye.adapters import PUNDITAdapter
                velocity = PUNDITAdapter.compute_velocity_km_s(
                    t.path_length_mm, t.pulse_time_us)
            if velocity is None:
                continue
            strength, _snapshot = apply_active_curve(
                t.project, velocity,
                rebound_number=t.rebound_number,
                temperature_c=t.surface_temperature_c)
            if strength is not None:
                assessed += 1
                if strength >= 25.0:
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
        from apps.digital_eye.models import PUNDITTest, RebarTest

        tests = list(
            PUNDITTest.objects
            .filter(project=project)
            .select_related('device', 'operator')
            .prefetch_related('files', 'readings')
            .order_by('structural_element', 'tested_at')
        )
        
        rebar_tests = list(
            RebarTest.objects
            .filter(project=project)
            .order_by('recorded_at')
        )

        report_no, year = cls._effective_report_number(project, tests)
        serial = report_no.split(' / ')[0]
        builder = NDTReportBuilder(report_no)

        # ---- Project branding (REFINED EXECUTIVE SUMMARY §2.3): the
        # project's uploaded logo/watermark, when configured. Storage
        # failures degrade to the standard layout — never a failed report.
        # The images are read through the FieldFile API (``.read()``), which
        # works on every storage backend — ``.path`` raises
        # NotImplementedError on remote storages (R2/S3) and the branding
        # silently never rendered there.
        try:
            branding = getattr(project, 'report_branding', None)
            if branding is not None:
                if branding.logo:
                    branding.logo.open('rb')
                    try:
                        builder.pdf.branding_logo = (
                            io.BytesIO(branding.logo.read()),
                            branding.logo_position,
                            type(branding).SIZE_WIDTHS_MM.get(
                                branding.logo_size, 30.0))
                    finally:
                        branding.logo.close()
                if branding.watermark:
                    branding.watermark.open('rb')
                    try:
                        builder.pdf.branding_watermark = (
                            io.BytesIO(branding.watermark.read()),
                            branding.watermark_opacity_pct)
                    finally:
                        branding.watermark.close()
        except Exception as exc:  # noqa: BLE001 — branding is cosmetic
            logger.error('report branding could not be applied: %s', exc)

        # ------------------------------------------------------------ cover
        # Client address block: street + LGA, then LAGOS STATE on its own
        # clear line (11 Sep client note — the state never shares a line
        # with the address). Only the parts that are recorded appear.
        site_parts = [p for p in (project.site_address, project.lga)
                      if p]
        tested = [t.tested_at for t in tests if t.tested_at]
        same_day = bool(tested) and min(tested).date() == max(tested).date()
        if tested:
            date_min, date_max = min(tested).date(), max(tested).date()
        else:
            date_min = date_max = datetime.now().date()
        builder.cover(serial, project.name,
                      ', '.join(site_parts), project.client_name,
                      _cover_date(date_max),
                      verify_url=report_verification_url(
                          report_no,
                          cls._statutory_digest(project, tests, report_no)))
        # LAGOS STATE on its own clear line below the site address (the
        # laboratory's jurisdiction; recorded state honoured when present).
        builder.pdf.set_font('Times', 'B', 18)
        builder.pdf.set_auto_page_break(False)
        builder.pdf.set_xy(builder.pdf.l_margin, 267.0)
        builder.pdf.cell(0, 7.8, _latin1(
            (project.state or 'LAGOS STATE').upper()), align='C')
        builder.pdf.set_auto_page_break(auto=True, margin=22)

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

        # ------------------------------------ shared data for the body sections
        element_data = cls._element_data(pulse_tests)
        good_members = [e for e in element_data if e['remark'] == 'GOOD']
        poor_members = [e for e in element_data if e['remark'] == 'POOR']
        floors_present = sorted({e['floor_label'] for e in element_data})
        # §4.1 observations, reference-style sentences (provenance stamps
        # stripped, element/location context, appendix-pic cross-refs).
        visual_notes = cls._visual_observations(tests)
        operators = []
        for t in tests:
            label = (t.operator_name
                     or (t.operator.get_full_name() or t.operator.email
                         if t.operator else None))
            if label and label not in operators:
                operators.append(label)
        from apps.digital_eye.models import BIMElementMapping
        # The real imported elements: IFC/RVT imports upsert
        # BIMElementMapping rows (the legacy BIMStructuralElement table is
        # never written by the import flow and stays empty on live
        # projects). Levels feed the building profile below.
        bim_levels = sorted(set(
            BIMElementMapping.objects.filter(project=project)
            .exclude(level='').values_list('level', flat=True)))
        has_drawings = BIMElementMapping.objects.filter(
            project=project).exists()

        # Generated-content CMS bodies (11 Sep client request): the computed
        # wording below resolves through get_cms_text so a saved project
        # override rewords the printed report; the bodies are the exact
        # wording computed from the recorded data.
        cms = cls._computed_bodies(
            project, tests=tests, rebar_tests=rebar_tests,
            element_data=element_data, good_members=good_members,
            poor_members=poor_members, visual_notes=visual_notes,
            floors_present=floors_present, bim_levels=bim_levels,
            has_drawings=has_drawings, tested=tested, same_day=same_day,
            date_min=date_min, date_max=date_max)

        # ------------------------------- EXECUTIVE SUMMARY (p3, un-TOC'd, C1)
        # toc_page() left the cursor on this fresh page via the TOC
        # placeholder's own page break — no add_page() here (that was the
        # stray blank page).
        builder.heading('EXECUTIVE SUMMARY', page_break=False, underline=True)
        exec_body, _src = get_cms_text(project, 'executive_summary',
                                       computed=cms)
        if exec_body is not None:
            # Generated-content CMS section: a project override rewords it;
            # the computed body is the exact wording from the recorded data.
            for para in cms_paragraphs(exec_body):
                builder.para(para, markdown=True)
        else:
            # No data behind this section yet and no override: the honest
            # unquantified rendering stays (never an invented summary).
            proj = _latin1(project.name or 'unnamed project').replace('*', '')
            client = _latin1(project.client_name or '').replace('*', '')
            site = _latin1(', '.join(
                p for p in (project.site_address, project.lga, project.state)
                if p)).replace('*', '')
            prose_dates = (_prose_date(date_max) if same_day
                           else f'{_prose_date(date_min)} and '
                                f'{_prose_date(date_max)}')
            # Reference p3 paragraph 1 opens with the building profile ("an
            # existing 2-floor building (A, B &C) belonging to …, at …") — the
            # storey count comes only from recorded levels/floors; when nothing
            # is recorded the profile stays unquantified rather than guessed.
            storeys = cls._storey_count(floors_present, bim_levels)
            building = (f'an existing {storeys}-floor building' if storeys
                        else 'an existing building')
            builder.para(
                'In situ Integrity Test (Non-Destructive) of compressive '
                f'strength of structural members of {building} '
                f'("**{proj}**")'
                + (f' belonging to **{client}**' if client else '')
                + (f', at **{site}**' if site else '')
                + '.',
                markdown=True)
            builder.para(
                'The Non-Destructive Integrity Test was carried out '
                + (f'on **{prose_dates}**' if tested else 'on dates not recorded')
                + ' with the intention to determine the residual compressive '
                  'strength of concrete component of the structural members '
                  'considered to be critical to stability, robustness and general '
                  'safety of the entire structure in its present state.',
                markdown=True)
            # Reference p3 paragraph 3 is ONE paragraph: visual findings + the
            # Non-Destructive analysis outcome + the structural-arrangement /
            # drawing-availability statement.
            if visual_notes:
                visual_sentence = (
                    'The visual inspection revealed structural defects as '
                    'recorded in Section 4.1 of this report')
            else:
                visual_sentence = (
                    'The visual inspection did not record any structural '
                    'defects')
            if element_data:
                analysis_sentence = (
                    'and the Non-Destructive test analysis shows that '
                    f'{len(good_members)} of the {len(element_data)} structural '
                    'members tested were good in strength (average compressive '
                    'strength at or above the assumed 25 N/mm2)'
                    + (f', while {len(poor_members)} member(s) fell below it and '
                       'require technical advice.' if poor_members
                       else ' at the time of test.'))
            else:
                analysis_sentence = (
                    'and no ultrasonic pulse velocity results are available '
                    'for this project.')
            if has_drawings:
                arrangement_sentence = (
                    'The general structural arrangement of the building was '
                    'referenced from the structural information available on '
                    'the platform (reproduced in the Appendix of this report).')
            else:
                arrangement_sentence = (
                    'The general structural arrangement of the buildings could '
                    'not be completely ascertained; as no structural drawing '
                    'was provided.')
            builder.para(' '.join(
                [visual_sentence, analysis_sentence, arrangement_sentence]))
            builder.para(
                'In view of the above, it is advised that a qualified structural '
                'engineer should be engaged to proffer solution to the defects '
                'observed, give technical advice on the poor structural members '
                'tested and further analyse the structural arrangement to '
                'guarantee the stability, integrity and the serviceability of the '
                'structure.'
            )

        # ------------------------------------------------------ 1.0 INTRO
        # section() always starts a main section on a fresh page, so the
        # executive summary keeps its own page and body numbering
        # (Introduction = page 1) starts deterministically.
        builder.section('1.0', 'INTRODUCTION')
        # Report CMS (8 Sep meeting H7): editable template sections resolve
        # through report_cms.get_cms_text — project override > platform
        # override > the registry default (verbatim the old hardcoded text).
        for para in cms_paragraphs(get_cms_text(project, 'introduction')[0]):
            builder.para(para)
        # Computed project paragraphs — generated-content CMS section
        # (11 Sep): a project override rewords them.
        intro_body, _src = get_cms_text(project, 'introduction_project',
                                        computed=cms)
        for para in cms_paragraphs(intro_body):
            builder.para(para, markdown=True)

        # ------------------------------------------------------ 2.0 PURPOSE
        builder.section('2.0', 'PURPOSE OF INVESTIGATION')
        builder.para('The purpose of the investigation is to:')
        builder.numbered(cms_list_items(
            get_cms_text(project, 'purpose_items')[0]))

        # -------------------------------------------------- 3.0 LITERATURE
        builder.section('3.0', 'LITERATURE REVIEW')
        for para in cms_paragraphs(get_cms_text(project,
                                                'literature_review')[0]):
            builder.para(para, leading=5.7)
        builder.para('The pulse velocity is calculated using the relationship:',
                     leading=5.7)
        builder.centered_formula('UPV = L / t')
        builder.bullet('L = Distance between transducers (mm)')
        builder.bullet('t = Pulse transit time (microseconds)')
        builder.para(
            'UPV testing is widely used for evaluating concrete homogeneity, '
            'detecting internal discontinuities such as cracks and voids, and '
            'supporting qualitative assessment of structural integrity.',
            leading=5.7,
        )
        builder.para(
            'The testing procedure and interpretation of results are '
            'conducted in accordance with international standards including:',
            leading=5.7,
        )
        builder.bullet('ASTM C597 - Standard Test Method for Pulse Velocity '
                       'Through Concrete (2020).')
        builder.bullet('BS EN 12504-4:2004 - Standard Test Method for '
                       'determination of the velocity of propagation of '
                       'pulses of ultrasonic longitudinal waves in concrete.')
        builder.bullet('ACI 228.2R - Report on Nondestructive Test Methods '
                       'for Evaluation of Concrete in Structures (2018).')
        builder.para(
            'Compliance with these standards ensures that equipment '
            'calibration, test configuration (direct, semi-direct, or indirect '
            'transmission), surface preparation, and reporting procedures '
            'meet current international best practice.',
            leading=5.7,
        )
        builder.para(
            'It is important to note that pulse velocity is influenced by '
            'moisture condition, temperature, aggregate type, and stress '
            'level. Therefore, interpretation must consider site conditions '
            'and, where necessary, be supported by complementary test methods.',
            leading=5.7,
        )
        builder.para(
            'The ultrasonic pulse velocity test is a non-destructive '
            'screening method and does not directly determine compressive '
            'strength of the reinforced concrete element unless calibrated '
            'correlation models are established for the specific concrete mix '
            'used in the structure.',
            leading=5.7,
        )
        # ---- Calibration disclosure (Nexucon Link, 8 Sep meeting): the
        # formula printed here is the formula actually applied to every
        # Section 5.0 figure — the project's active calibration curve when
        # one is set, else the documented laboratory default.
        from apps.digital_eye.strength_curves import resolve_active_curve
        try:
            active_curve = resolve_active_curve(project)
        except Exception:  # noqa: BLE001 — disclosure must never kill the report
            active_curve = None
        ecs_disclosure, ecs_derivation = ecs_report_disclosure(project)
        builder.para(ecs_disclosure, leading=5.7)
        # ---- Derivation of the reported results (7 Sep 2026 meeting:
        # every figure must be recomputable by hand). Element means are
        # the mean of the PER-POINT velocities (BS EN 12504-4 practice),
        # not the velocity of the mean transit time — stating the method
        # is what reconciles manual and system arithmetic.
        builder.para(
            'Derivation of the reported results: each test point velocity '
            'is V = L / t; the element pulse velocity is the arithmetic '
            'mean of its point velocities, V(element) = (V1 + V2 + ... + '
            f'Vn) / n; and the estimated compressive strength follows '
            f'{ecs_derivation}. Pulse velocities in the Section 5.0 tables '
            'are reported in metres per second (m/s); 1 km/s = 1000 m/s.',
            leading=5.7,
        )
        example = cls._worked_example(project, element_data, active_curve)
        if example:
            builder.para(example, leading=5.7)

        # ---------------------------- 3.1 LOCATION MAP / WEATHER (ref p7)
        # Reference: heading centred bold 14 underlined (no number on the
        # page itself), address centred bold 18, then TWO images side by
        # side in fixed frames — left 31.0/74.2mm wide, right 106.6/79.4mm
        # wide, both from 48.3mm, ~139mm tall — with no captions.
        builder.pdf.add_page()
        builder.section('3.1', 'LOCATION MAP/ WEATHER CONDITION',
                        size=14, centered=True, sub=True)
        builder.display_line(
            (project.site_address or 'SITE ADDRESS NOT RECORDED').upper()
            + (f', {(project.lga or "").upper()}' if project.lga else '')
            + (f', {(project.state or "").upper()}.' if project.state else '.')
        )
        # Map image sources, honest ones only: operator-attached map photo,
        # the operator's captured BIM 3D preview, then the plan view
        # rendered from the imported model geometry — two frames are filled,
        # a single one is centred, none gives the placeholder box.
        map_sources = []
        for t in tests:
            for f in t.files.all():
                blob = f'{f.file_name or ""} {f.description or ""}'.lower()
                if f.file_type == 'photo' and 'map' in blob:
                    try:
                        url = f.file.url if f.file else ''
                    except Exception:  # noqa: BLE001 — remote storage
                        url = ''
                    if url:
                        map_sources.append(url)
                        break
            if map_sources:
                break
        capture_url = cls._bim_capture_url(project)
        if capture_url:
            map_sources.append(capture_url)
        # Google static map around the project's recorded GNSS coordinates
        # (C6) — a real 500 m radius map image when coordinates + API key
        # exist; absent otherwise, never fabricated.
        static_map = cls._google_static_map(project)
        if static_map is not None:
            map_sources.insert(0, static_map)
        if len(map_sources) < 2:
            try:
                plan_buf, _cap = cls._generate_bim_plan_view(project)
            except Exception as exc:        # noqa: BLE001 — never break
                logger.error('BIM plan view rendering failed: %s', exc)
                plan_buf = None
            if plan_buf:
                map_sources.append(plan_buf)
        pdf = builder.pdf
        if not map_sources:
            builder.placeholder_box('SITE LOCATION MAP NOT PROVIDED',
                                    height=100)
        elif len(map_sources) == 1:
            cls._boxed_image(pdf, map_sources[0], (pdf.w - 120) / 2,
                             48.3, 120, 138.9)
        else:
            cls._boxed_image(pdf, map_sources[0], 31.0, 48.3, 74.2, 138.9)
            cls._boxed_image(pdf, map_sources[1], 106.6, 48.3, 79.4, 138.6)
        # Weather is operator-recorded; the reference prints none, so a
        # recorded condition gets a single plain centred line below the
        # frames and nothing is invented when it is absent.
        weather_values = [t.weather_condition for t in tests
                          if t.weather_condition]
        if weather_values:
            pdf.set_xy(pdf.l_margin, 189)
            pdf.set_font('Cambria', '', 12)
            pdf.set_text_color(*INK)
            pdf.cell(0, 7.5, _latin1(', '.join(sorted(set(weather_values)))),
                     align='C')
        # Recorded GNSS coordinates + the Google Maps link to the site (C6).
        # Only when the project carries real coordinates — nothing is guessed.
        if project.latitude is not None and project.longitude is not None:
            link = cls._maps_link(project)
            pdf.set_xy(pdf.l_margin, 196)
            pdf.set_font('Cambria', 'I', 10)
            pdf.set_text_color(*INK)
            pdf.multi_cell(
                0, 5.0,
                _latin1(
                    f'Coordinates: {project.latitude:.6f}°, '
                    f'{project.longitude:.6f}°  —  view location on Google Maps:'
                    f' {link}'),
                align='C', new_x='LMARGIN', new_y='NEXT')

        # ------------------------------------------------------ 4.0 FIELD WORK
        builder.section('4.0', 'FIELD WORK')
        if tested:
            if same_day:
                builder.para(
                    'The field work was carried out on '
                    f'**{_prose_date(date_max)}** and was completed same day.',
                    leading=7.5, markdown=True)
            else:
                builder.para(
                    'The field work was carried out between '
                    f'**{_prose_date(date_min)}** and '
                    f'**{_prose_date(date_max)}**.',
                    leading=7.5, markdown=True)
        else:
            builder.para('The dates of the field work were not recorded.',
                         leading=7.5)
        builder.para(
            'Visual test was carried out on the structure to ascertain any '
            'possible structural defects (e.g. cracks, differential '
            'settlement, spalling, honeycombs, hogging and sagging). This is '
            'a vital aspect of non-destructive test. The Standard Portable '
            'Ultrasonic Non-Destructive Digital Indicating Tester (Pundit) '
            'was employed for the estimation of compressive strength of the '
            'hardened concrete on the structural elements.'
            + (' Profoscope was used to locate the position of reinforcement '
               'bars (Rebar).' if rebar_tests else ''),
            leading=7.5,
        )
        builder.para(
            'It is important to note that during the test some factors were '
            'taken into consideration, which may impact on the result of the '
            'compressive strength of the structural members. These are as '
            'follows:',
            leading=7.5,
        )
        builder.bullet('Surface conditions, temperature and moisture content '
                       'of the existing concrete.', leading=7.5)
        builder.bullet('Path length, shape and size of the concrete member.',
                       leading=7.5)
        builder.bullet('Concrete stress.', leading=7.5)
        builder.bullet('Effect of reinforcing bars.', leading=7.5)
        # Concrete maturity (7 Sep review, item 18): stated only when ages
        # were actually recorded — no age is assumed.
        ages = sorted({t.concrete_age_days for t in tests
                       if t.concrete_age_days})
        if ages:
            age_clause = (f'{ages[0]} days' if len(ages) == 1
                          else f'between {ages[0]} and {ages[-1]} days')
            builder.para(
                'The age of the concrete at the time of test was recorded as '
                f'{age_clause}. Strength gain beyond 28 days is minimal, so '
                'ages within the typical 14-52 day testing window are '
                'reported for information; no age-based correction is '
                'applied to the estimated strengths.', leading=7.5)
        builder.para('The scope of the work done is as follows:', leading=7.5)
        scope_items = [
            'Initial visual test was carried out on the building structure '
            'tested (i.e column, beam, slab, wall etc).',
        ]
        if rebar_tests:
            scope_items.append(
                'Profoscope, Rebar locator was used to locate the '
                'reinforcement position, concrete cover measurement and Rebar '
                'size embedded in the structural members.')
        if devices:
            scope_items.append(
                'Calibration of the Portable Ultrasonic Non-Destructive '
                'Digital Indicating Tester (PUNDIT) was done before '
                'commencing the test.')
        point_counts = sorted({e['n_points'] for e in element_data})
        if point_counts:
            if len(point_counts) == 1:
                n = point_counts[0]
                word = {1: 'one (1)', 2: 'two (2)', 3: 'three (3)',
                        4: 'four (4)', 5: 'five (5)'}.get(n, f'{n}')
                noun = 'test point was' if n == 1 else 'test points were'
                points_clause = f'{word} {noun} randomly selected'
            else:
                points_clause = (f'between {point_counts[0]} and '
                                 f'{point_counts[-1]} test points were '
                                 'randomly selected')
            scope_items.append(
                f'Indirect method was employed, then {points_clause} to get a '
                'good representation and result on each structural member, '
                'according to BS EN 12504-4:2021, for testing concrete.')
        # Reference §4.0 scope list: number 1.6mm in, text 12.7mm across,
        # 7.5mm leading.
        builder.numbered(scope_items, num_indent=1.6, text_indent=12.7,
                         leading=7.5)

        # ------------------------------- EQUIPMENT STATUS CHECK (ref p9)
        builder.heading('EQUIPMENT STATUS CHECK')
        if devices:
            freqs = sorted({t.transducer_frequency_khz for t in tests
                            if t.transducer_frequency_khz})
            for d in devices:
                rows = [
                    ('Name of Equipment', d.name or d.model or '-'),
                    ('Test Equipment ID', d.device_id or '-'),
                    ('Equipment Status', d.status or '-'),
                    ('Calibration Date',
                     d.calibration_date.strftime('%d/%m/%Y')
                     if d.calibration_date else '-'),
                ]
                if freqs:
                    rows.append(('Transducer',
                                 ' and '.join(f'{f} kHz' for f in freqs)))
                builder.kv_table(rows)
                builder.ln_gap(4)
        else:
            builder.para('No field device was recorded against these tests.',
                         leading=7.5)
        if operators:
            builder.para(
                'The test was conducted in the presence of the following '
                'recorded operator(s) and staff:', leading=7.5)
            pdf = builder.pdf
            pdf.set_font('Cambria', 'B', 12)
            pdf.set_text_color(*INK)
            for name in operators:
                pdf.set_x(pdf.l_margin + 12.7)
                pdf.cell(0, 9.9, f'{_latin1(name)} : ' + '…' * 19,
                         new_x='LMARGIN', new_y='NEXT')
            pdf.set_x(pdf.l_margin)
            pdf.set_font('Cambria', '', 12)
        else:
            builder.para('The operator(s) of the test were not recorded.',
                         leading=7.5)
        builder.para(
            'This is part of Lagos State Government\'s effort to reduce the '
            'incidence of building and civil engineering (construction) '
            'materials failure and building collapse within the geographical '
            'boundary of Lagos State.',
            leading=7.5,
        )

        # -------------------------------------------------- 4.1 VISUAL TEST
        builder.section('4.1', 'VISUAL TEST', sub=True)
        for para in cms_paragraphs(get_cms_text(project,
                                                'visual_preamble')[0]):
            builder.para(para, leading=7.5)
        # Generated-content CMS section (11 Sep): the lettered observations
        # pre-fill from the recorded notes; a project override rewords them.
        visual_body, visual_src = get_cms_text(project, 'visual_observations',
                                               computed=cms)
        if visual_body is not None:
            builder.lettered(cms_list_items(visual_body))
            builder.para(
                'Following the aforementioned, a Non-Destructive Test was '
                'conducted. The photographs in the appendix of this report '
                'show the physical state of the structure as at test time.',
                leading=7.5)
        elif visual_src == 'unavailable':
            builder.para('No visual/surface condition observations recorded.',
                         leading=7.5)

        # ------------------------------------------------ 4.2 METHODOLOGY
        builder.section('4.2', 'METHODOLOGY', sub=True)
        builder.inner_heading('NON-DESTRUCTIVE CONCRETE STRENGTH'
                              + (' AND REBAR DETERMINATION.' if rebar_tests
                                 else ' DETERMINATION.'))
        # Generated-content CMS section (11 Sep): the equipment paragraphs
        # pre-fill from the tests actually recorded.
        method_body, _src = get_cms_text(project, 'methodology_equipment',
                                         computed=cms)
        for para in cms_paragraphs(method_body):
            builder.para(para, leading=7.5)
        builder.inner_heading('CONCRETE', centered=False, underline=False)
        for para in cms_paragraphs(get_cms_text(project,
                                                'methodology_concrete')[0]):
            builder.para(para, leading=7.5)
        builder.para('The Pundit test equipment can also determine the '
                     'following:', leading=7.5)
        builder.bullet('The homogeneity and uniformity of the concrete.',
                       leading=7.5)
        builder.bullet('Changes in the strength of the concrete which may '
                       'occur with time.', leading=7.5)
        builder.bullet('The quality of the concrete in relation to standard '
                       'requirements.', leading=7.5)
        builder.bullet('The quality of one element of concrete in relation to '
                       'another.', leading=7.5)
        # Conversion statement mirrors the curve actually applied (Section
        # 3.0 carries the full disclosure).
        if active_curve is not None and active_curve.project_id:
            from apps.digital_eye.strength_curves import formula_display
            ecs_conversion_line = (
                formula_display(active_curve.curve_type,
                                active_curve.formula_params or {})
                + ' (f_cu in N/mm2, V in m/s'
                + (', R the rebound number)'
                   if active_curve.curve_type == 'sonreb' else ')'))
        else:
            ecs_conversion_line = ECS_FORMULA_LINE
        builder.para('Estimated compressive strength conversion: '
                     + ecs_conversion_line
                     + '. The full calibration statement is given in '
                       'Section 3.0.', leading=7.5)

        # ------------------------------------------------------- 4.3 REBAR
        builder.section('4.3', 'REINFORCING BAR (REBAR) ASSESSMENT', sub=True)
        # Generated-content CMS section (11 Sep): the rebar statement
        # pre-fills from the recorded survey; without a survey the honest
        # "Not Applicable" wording stays fixed (no override can invent one).
        rebar_body, rebar_src = get_cms_text(project, 'rebar_statement',
                                             computed=cms)
        if rebar_body is not None:
            builder.para(rebar_body, leading=7.5)
        else:
            builder.para(
                'Rebar Assessment: Not Applicable. No rebar survey was '
                'recorded during this investigation; the reported results '
                'are limited to the ultrasonic and visual indications of '
                'Sections 4.1 and 5.0.',
                leading=7.5,
            )

        # ------------------------------------------------------ 4.4 EQUIPMENT
        builder.section('4.4', 'EQUIPMENT/REBAR ASSESSMENT TABLE', sub=True)
        if devices:
            for d in devices:
                builder.kv('NAME OF EQUIPMENT', d.name or d.model or '-')
                builder.kv('EQUIPMENT ID', d.device_id or '-')
            builder.ln_gap()
        if rebar_tests:
            rebar_data = []
            for idx, rt in enumerate(rebar_tests):
                main_bar = str(int(rt.main_bar_mm)) if rt.main_bar_mm else '-'
                links = str(int(rt.links_mm)) if rt.links_mm else '-'
                spacing = str(rt.spacing_mm) if rt.spacing_mm else '-'
                cover = str(int(rt.cover_depth_mm)) if rt.cover_depth_mm else '-'
                rebar_data.append([
                    str(idx + 1),
                    (rt.structural_element or 'Unknown').upper(),
                    main_bar,
                    links,
                    spacing,
                    cover
                ])
            builder.ruled_table(
                ['S/N', 'STRUCTURAL MEMBER', 'MAIN BAR (MM)', 'LINKS (MM)',
                 'SPACING (MM)', 'COVER DEPTH (MM)'],
                rebar_data,
                [12, 45, 30, 28, 30, 31],
                ['C', 'L', 'C', 'C', 'C', 'C'],
            )
        else:
            builder.para('Rebar scanning: Not Applicable for this project.',
                         leading=7.5)
        builder.para('NOTE: This assessment does not cover for the '
                     'construction reinforcement design.', leading=7.5)

        if crack_tests:
            builder.para('Crack depth measurements are tabulated in '
                         'Section 5.0 (time-difference method).',
                         leading=7.5)
        else:
            builder.para('No crack depth measurements recorded.',
                         leading=7.5)

        # ------------------------------------------------ 5.0 ANALYSIS
        builder.divider_page('5.0 ANALYSIS OF TEST RESULT')
        pending_count = sum(1 for t in tests if t.quality_grade == 'pending')
        if pending_count:
            builder.para(
                f'{pending_count} test(s) have not been run through the '
                f'platform analysis endpoint; velocities shown are computed '
                f'directly from the recorded measurements using the same '
                f'deterministic BS 1881-203 relations.'
            )
        builder.para(
            'Crack depth measurements are analysed first (Section 5.1), '
            'ahead of the pulse velocity parameter tests, so defects that '
            'bias velocity readings are known before strengths are '
            'interpreted.'
        )
        builder.para(
            'Element average compressive strength is computed from the '
            'element mean pulse velocity through the Section 3.0 calibration '
            'curve, and members are remarked GOOD or POOR against the '
            'statutory 25 N/mm2 design strength. Pulse velocities are '
            'reported in metres per second (m/s). Velocities outside the '
            'calibrated 2.0 - 5.0 km/s range are reported without an E.C.S '
            'estimate rather than extrapolated. Where an element\'s test '
            'points disagree by more than 2% of the mean velocity, the '
            'remark carries the point spread so the variance is visible '
            'rather than silently averaged.'
        )

        if crack_tests:
            builder.section('5.1', 'CRACK DEPTH MEASUREMENTS '
                                   '(TIME-DIFFERENCE METHOD)', sub=True)
            # Same A/B/C point layout as the Section 5.0 velocity tables:
            # the element's name once, one row per test point (its t_c/t_0
            # pair and the computed per-point depth), and the element
            # verdict (mean depth + remark) on the middle row.
            for t in crack_tests:
                rows = t.reading_rows()
                mid = len(rows) // 2 if len(rows) > 1 else 0
                mean_depth = cls._crack_depth(t)
                table_rows = []
                for i, r in enumerate(rows):
                    table_rows.append([
                        _element_display(t.structural_element) if i == 0 else '',
                        r['label'] or '-',
                        cls._fmt(r['path_mm'], 1),
                        cls._fmt(r['transit_us'], 1),
                        cls._fmt(r['uncracked_us'], 1),
                        cls._fmt(r['crack_depth_mm'], 1),
                        (cls._fmt(mean_depth, 1) + '\n' + cls._crack_remark(t))
                        if i == mid else '',
                    ])
                builder.ruled_table(
                    ['ELEMENT', 'POINT', 'SPACING L (MM)',
                     'T CRACKED (US)', 'T UNCRACKED (US)',
                     'CRACK DEPTH (MM)', 'MEAN DEPTH (MM) / REMARK'],
                    table_rows,
                    [42, 12, 16, 20, 21, 20, 34],
                    ['L', 'C', 'C', 'C', 'C', 'C', 'L'],
                )
                builder.ln_gap(2)

        if not element_data:
            builder.para('No pulse velocity tests recorded for this project.')
        else:
            # ---- Summary of Test Analysis: counts from the real rows
            builder.heading('SUMMARY OF TEST ANALYSIS', page_break=False)
            analysis_groups = {}
            for e in element_data:
                key = (e['floor_label'], e['member_type'])
                g = analysis_groups.setdefault(
                    key, {'count': 0, 'points': 0})
                g['count'] += 1
                g['points'] += e['n_points']
            builder.ruled_table(
                ['STRUCTURAL MEMBER', 'NUMBER TESTED', 'LOCATION',
                 'NO OF POINT TAKEN'],
                [[member.title(), str(g['count']), floor.title(),
                  str(g['points'])]
                 for (floor, member), g in sorted(
                     analysis_groups.items())],
                [50, 32, 60, 40],
                ['C', 'C', 'C', 'C'],
            )
            builder.ln_gap(6)

            # ---- Per-floor / per-member result tables (reference layout:
            # element name once, A/B/C reading rows, average + remark on the
            # middle row)
            for floor in floors_present:
                floor_elements = [e for e in element_data
                                  if e['floor_label'] == floor]
                member_order = []
                for e in floor_elements:
                    if e['member_type'] not in member_order:
                        member_order.append(e['member_type'])
                for member in member_order:
                    group = [e for e in floor_elements
                             if e['member_type'] == member]
                    plural = member if member.endswith('S') else member + 'S'
                    builder.subheading(
                        f'{floor.upper()} {plural}'
                        + (f' OF {project.name.upper()}'
                           if project.name else ''))
                    for e in group:
                        rows = e['rows']
                        mid = len(rows) // 2 if len(rows) > 1 else 0
                        # The element's own name (its BIM identity), one
                        # Revit 'Family:Type:Tag' segment per line — the
                        # test serial is provenance and stays in the
                        # registry / integrity digest, not the results
                        # table.
                        element_cell = _element_display(e['element'])
                        remark = e['remark']
                        if (e['spread_pct'] is not None
                                and e['spread_pct'] > 2.0):
                            remark += (f"\nPOINT SPREAD "
                                       f"{e['spread_km_s'] * 1000:.0f} M/S "
                                       f"(±{e['spread_pct'] / 2:.1f}%)")
                        table_rows = []
                        for i, r in enumerate(rows):
                            table_rows.append([
                                element_cell if i == 0 else '',
                                cls._fp(r['path_mm']),
                                cls._f1(r['transit_us']),
                                cls._fms(r['velocity_km_s']),
                                cls._f1(r['ecs_mpa']),
                                cls._f1(e['mean_ecs']) if i == mid else '',
                                remark if i == mid else '',
                            ])
                        builder.ruled_table(
                            ['Structural Element', 'PATH LENGTH',
                             'TRANSIT TIME', 'PULSE VELOCITY (M/S)',
                             'E.C.S',
                             'AVERAGE COMPRESSIVE STRENGTH (N/mm2)',
                             'REMARK'],
                            table_rows,
                            [40, 17, 19, 26, 12, 29, 21],
                            ['L', 'C', 'C', 'C', 'C', 'C', 'C'],
                        )
                        builder.ln_gap(2)

            # ---- Summary of Test Results: GOOD / POOR per member & floor
            builder.heading('SUMMARY OF TEST RESULTS', page_break=False)
            result_groups = {}
            for e in element_data:
                key = (e['floor_label'], e['member_type'])
                g = result_groups.setdefault(
                    key, {'good': 0, 'poor': 0, 'total': 0})
                g['total'] += 1
                if e['remark'] == 'GOOD':
                    g['good'] += 1
                elif e['remark'] == 'POOR':
                    g['poor'] += 1
            result_rows = []
            for (floor, member), g in sorted(result_groups.items()):
                good_pct = round(g['good'] * 100 / g['total'], 1)
                poor_pct = round(g['poor'] * 100 / g['total'], 1)
                result_rows.append([
                    member.title(),
                    floor.title(),
                    f"{g['good']} ({good_pct}%)",
                    f"{g['poor']} ({poor_pct}%)",
                ])
            builder.ruled_table(
                ['STRUCTURAL MEMBER', 'LOCATION', 'GOOD (NO, %)',
                 'POOR (NO, %)'],
                result_rows,
                [50, 60, 34, 32],
                ['C', 'C', 'C', 'C'],
            )
            builder.ln_gap(4)

        if surface_tests:
            builder.section('5.2', 'SURFACE QUALITY OBSERVATIONS', sub=True)
            # One row per test point carrying the condition observed there;
            # the element's name and the test-level notes print once.
            for t in surface_tests:
                rows = t.reading_rows()
                # Observation words only — the '[MANUAL_FIELD_ENTRY —
                # Station …]' provenance stamp never prints.
                notes = (cls._PROVENANCE_STAMP_RE.sub('', t.notes or '').strip()
                         or '-')
                table_rows = []
                for i, r in enumerate(rows):
                    table_rows.append([
                        _element_display(t.structural_element) if i == 0 else '',
                        r['label'] or '-',
                        (r['surface_condition'] or '-'),
                        notes if i == 0 else '',
                    ])
                builder.ruled_table(
                    ['ELEMENT', 'POINT', 'SURFACE CONDITION', 'NOTES'],
                    table_rows,
                    [42, 14, 55, 54],
                    ['L', 'C', 'L', 'L'],
                )
                builder.ln_gap(2)

        # ------------------------------------------------ BAR CHARTS
        # Reference p33: 'BAR CHART SHOWING SUMMARY OF TEST RESULTS'
        # Cambria-Bold 18 centred (not underlined), chart A at
        # 45.3/35.6mm 125.1mm wide with its caption at 112.3mm, chart B at
        # 45.3/121.9mm with its caption at 211.8mm — captions Cambria 11
        # centred, drawn by the PDF (not inside the figures).
        if element_data:
            try:
                chart_a, chart_b = cls._generate_charts(element_data)
                if chart_a and chart_b:
                    builder.section(
                        '', 'BAR CHART SHOWING SUMMARY OF TEST RESULTS',
                        size=18, centered=True, underline=False)
                    pdf = builder.pdf
                    pdf.image(chart_a, x=45.3, y=35.6, w=125.1)
                    pdf.set_xy(pdf.l_margin, 112.3)
                    pdf.set_font('Cambria', '', 11)
                    pdf.cell(0, 6, '*CHART ILLUSTRATING THE NUMBER OF '
                                   'STRUCTURAL MEMBER TESTED', align='C')
                    pdf.image(chart_b, x=45.3, y=121.9, w=125.1)
                    pdf.set_xy(pdf.l_margin, 211.8)
                    pdf.cell(0, 6, '*CHART ILLUSTRATING THE PERCENTAGE OF '
                                   'STRENGTH OF STRUCTURAL MEMBER TESTED',
                             align='C')
                    pdf.set_font('Cambria', '', 12)
                    pdf.set_xy(pdf.l_margin, 220)
            except Exception as e:
                logger.error('Could not generate summary charts: %s', e)

        # ------------------------------- 5.3 AI-ASSISTED INTERPRETATION
        # The platform's AI analysis layer (analyze_project) stores its
        # narrative on the project's latest pundit AIAnalysisRecord — the
        # report surfaces it verbatim, labelled with its provider and
        # evidence-based confidence. Deterministic-only records (no LLM
        # configured/available) add nothing the counts prose does not
        # already say, so the section is skipped honestly.
        try:
            from apps.evidence.models import AIAnalysisRecord
            ai_record = (AIAnalysisRecord.objects
                         .filter(project=project, analysis_type='pundit')
                         .order_by('-created_at').first())
        except Exception as e:
            logger.error('Could not load AI analysis record: %s', e)
            ai_record = None
        if (ai_record and ai_record.observations
                and (ai_record.model_provider or 'deterministic')
                != 'deterministic'):
            builder.section('5.3', 'AI-ASSISTED INTERPRETATION', sub=True)
            conf_pct = ('not scored' if ai_record.confidence is None
                        else f'{round(ai_record.confidence * 100)}%')
            builder.para(
                'The platform analysis engine recorded the following '
                'interpretation of the field measurements, synthesised by '
                f'{ai_record.model_provider} '
                f'({ai_record.model_version or "model version not recorded"})'
                f', with an evidence-based confidence of {conf_pct}. It is '
                'derived solely from the recorded readings in Section 5.0 '
                'and serves as decision support for the responsible '
                'engineer, who reviews and signs off this report.')
            for obs in ai_record.observations:
                builder.bullet(str(obs))
            # ---- Confidence metrics (11 Sep 2026, PART B §2.2): per-element
            # intervals, probability below design strength, cross-element
            # outlier checks, data quality and the reasoning trace — computed
            # from the recorded data by the analysis engine and stored on the
            # record. Rendered verbatim; nothing here is editable prose.
            for m in (ai_record.correlations or []):
                if not isinstance(m, dict) or 'mean_ecs_n_mm2' not in m:
                    continue
                element = m.get('element') or 'element'
                floor = f" ({m['floor']})" if m.get('floor') else ''
                builder.inner_heading(
                    f"{_element_display(element).upper()}{floor}")
                rows = [
                    ('Mean pulse velocity',
                     '-' if m.get('mean_velocity_m_s') is None
                     else f"{m['mean_velocity_m_s']:.0f} m/s"),
                    ('Estimated compressive strength',
                     f"{m['mean_ecs_n_mm2']:.1f} N/mm2"),
                ]
                ci = m.get('confidence_interval_n_mm2')
                rows.append(('95% confidence interval',
                             f"{ci[0]:.1f} - {ci[1]:.1f} N/mm2"
                             if ci else
                             'Not available — the active calibration curve '
                             'carries no regression standard error'))
                p_below = m.get('probability_below_design')
                rows.append(('Probability of strength below the 25 N/mm2 '
                             'design strength',
                             f"{p_below * 100:.1f}%"
                             if p_below is not None else 'Not computable'))
                dq = m.get('data_quality')
                rows.append(('Data quality',
                             f"{dq['label']} — {dq['reason']}"
                             if dq else 'Not scored'))
                outlier = m.get('cross_element_outlier')
                rows.append(('Cross-element check',
                             (f"OUTLIER — deviates {outlier['deviation_pct']:+.1f}% "
                              f"from the {outlier['peer_median_m_s']:.0f} m/s "
                              f"median of its {outlier['group']}")
                             if outlier else
                             'Consistent with its peer group'))
                builder.kv_table(rows)
                builder.inner_heading('AI REASONING TRACE')
                for i, step in enumerate(m.get('reasoning_trace') or [], 1):
                    builder.para(f'{i}. {step}', leading=7.5)

        # ---------------------------------------------- 6.0 RECOMMENDATIONS
        builder.section('6.0', 'RECOMMENDATION')
        if element_data:
            # CMS override replaces the editable lead-in; the findings
            # statement is itself a generated-content CMS section (11 Sep)
            # whose computed default is the wording from the recorded data.
            lead_in = get_cms_text(project, 'recommendation_preamble')[0]
            findings_body, _src = get_cms_text(project, 'findings_statement',
                                               computed=cms)
            builder.para(
                lead_in.rstrip()
                + ' ' + findings_body
            )
        else:
            builder.para('No pulse velocity results are available for this '
                         'project; no recommendation on concrete quality can '
                         'be made.')
        # Stored analysis records carry the platform's official wording —
        # surface them when present.
        recommendations = []
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
            builder.para("The platform's stored analysis records add the "
                         'following recommendations:')
            for rec in recommendations:
                builder.bullet(rec)
        elif not poor_members and not crack_tests:
            builder.para('No adverse findings recorded; recorded concrete '
                         'quality falls within acceptable velocity bands.')

        # -------------------------------------------------- 7.0 CONCLUSION
        builder.section('7.0', 'CONCLUSION')
        if element_data:
            for para in cms_paragraphs(get_cms_text(project,
                                                    'conclusion_preamble')[0]):
                builder.para(para)
            # Generated-content CMS section (11 Sep): the numbered conclusion
            # items pre-fill with the computed percentages; a project
            # override rewords them.
            conclusion_body, _src = get_cms_text(project, 'conclusion_items',
                                                 computed=cms)
            # Reference §7.0: text 12.7mm across, 7.5mm leading.
            builder.numbered(cms_list_items(conclusion_body),
                             text_indent=12.7, leading=7.5)
            builder.ln_gap(3)
            builder.note_block(
                'The test assumed 25 N/mm2 as the strength of the '
                'structural members, however a substructure probe is '
                'required to ascertain the integrity of the building '
                'foundation.'
            )
        else:
            builder.para(
                'No ultrasonic pulse velocity results are available for this '
                'project; no conclusion on concrete quality can be drawn.'
            )
        # Reference sign-off: exactly TWO slots — tested by (left) and
        # approved by (right), dotted line above an ALL-CAPS name. Every
        # operator stays listed in the REPORT INTEGRITY block below.
        user_label = 'Unauthenticated'
        if user and getattr(user, 'is_authenticated', False):
            user_label = user.get_full_name() or user.email
        # Reference: the dotted lines sit at ~150mm (y 425pt) — park the
        # sign-off there when the conclusion ends higher up the page.
        if builder.pdf.get_y() < 140:
            builder.pdf.set_y(140)
        builder.signature_lines([
            (_latin1((operators[0] if operators else 'NOT RECORDED').upper()),
             'TESTED BY'),
            (_latin1(user_label.upper()), 'APPROVED BY'),
        ])

        # ------------------------ REPORT INTEGRITY (un-TOC'd, C11)
        builder.heading('REPORT INTEGRITY')
        digest = cls._statutory_digest(project, tests, report_no)
        builder.para(
            'This report was generated from verified platform records. Every '
            'figure is computed from live test records; the content digest '
            'below is a SHA-256 hash of the underlying test identifiers, '
            'results and reading rows and can be used to detect any later '
            'alteration.'
        )
        builder.signoff_block([
            ('Report No', report_no),
            ('Tested by', ', '.join(operators) if operators
             else 'NOT RECORDED'),
            ('Approved by', user_label),
            ('Content digest', digest),
            ('Date', f'{datetime.now():%Y-%m-%d %H:%M}'),
        ])

        # ------------------------------------------------------ APPENDIX
        builder.divider_page('APPENDIX')
        # Reference appendix I-III: a 'BUILDING A' label at the left margin
        # (Cambria 12) with the drawing images stacked beneath — NO centred
        # heading. The honest equivalent of the drawings is the imported BIM
        # model's plan view + structural element schedule; nothing is
        # fabricated when no model is imported.
        pdf = builder.pdf
        pdf.start_section('DRAWING OF THE BUILDING', level=1)
        pdf.set_font('Cambria', '', 12)
        pdf.set_text_color(*INK)
        pdf.set_xy(pdf.l_margin, 16.9)
        pdf.cell(0, 7.5, _latin1(project.name),
                 new_x='LMARGIN', new_y='NEXT')
        bim_elements = list(
            BIMElementMapping.objects.filter(
                project=project).order_by('level', 'element_id')[:400])
        # The drawing itself: the plan view rendered from the imported
        # model's geometry (C13 — "the architectural drawing is our BIM").
        plan_embedded = cls._embed_bim_plan(builder, project)
        pdf.set_xy(pdf.l_margin, 26.8 + (101.6 + 6 if plan_embedded else 4))
        if bim_elements:
            try:
                geometry = project.bim_model_geometry
                source = geometry.source_file or 'imported BIM model'
            except Exception:  # noqa: BLE001 — related row may be absent
                source = 'imported BIM model'
            builder.para(
                f'Structural element schedule extracted from {source} '
                f'({len(bim_elements)} of the imported elements listed '
                'below; the interactive 3D model is available on the '
                'platform).'
            )
            builder.ruled_table(
                ['ELEMENT', 'CATEGORY', 'LEVEL', 'MARK',
                 'MATERIAL / GRADE RECORDED'],
                [[_element_display(e.element_name or e.element_id),
                  _pretty_ifc_category(e.element_type) or '-',
                  e.level or '-',
                  e.properties.get('Tag', '-') if e.properties else '-',
                  _latin1(e.properties.get('Material', '-')
                          if e.properties else '-') or '-']
                 for e in bim_elements],
                [52, 20, 22, 18, 53],
                ['L', 'C', 'C', 'C', 'L'],
            )
        else:
            if not plan_embedded:
                builder.placeholder_box('STRUCTURAL DRAWINGS NOT PROVIDED',
                                        height=100)
        # Per-floor plans (7 Sep review, item 7): one plan page per tested
        # floor when the model's recorded levels support the split. When
        # they do not, nothing is added — the whole-model plan above and
        # the per-floor result tables carry the floor information instead.
        cls._embed_bim_floor_plans(
            builder, project,
            sorted({e['floor_label'] for e in element_data}))
        # PHOTOGRAPHS — the reference starts them on a fresh page with no
        # body heading; the TOC entry points at the first photograph page,
        # so the section is registered inside _render_appendix once that
        # page exists.
        shown = cls._render_appendix(builder, tests)
        if not shown:
            builder.pdf.add_page()
            builder.pdf.start_section('PHOTOGRAPHS', level=1)
            builder.para('No photographs recorded for these tests.')

        return builder.bytes()

    # ------------------------------------------------------------ helpers
    # Provenance stamps the entry forms put inside the notes field
    # ('[MANUAL_FIELD_ENTRY — Station UPV-FLD-2026-002]') — audit markers,
    # never part of the observation itself.
    _PROVENANCE_STAMP_RE = re.compile(r'^\s*\[[^\]\n]*\]\s*')

    @classmethod
    def _visual_observations(cls, tests):
        """
        Reference-style §4.1 lettered observations: 'Tacky floor observed
        on Floor:200THK RC SLAB:780904 (see pic i).' The observation words
        are the operator's own — sentence-cased, with any provenance stamp
        stripped — and the tested element plus its recorded location are
        appended as context. '(see pic N)' cross-references the appendix
        photograph when the test carries attached files (numbering
        simulated in the same order _render_appendix walks the tests, so
        the reference points at the photograph the reader will actually
        find). No observation text is ever invented or rewritten.
        """
        # Appendix photograph numbering: same walk as _render_appendix
        # (files deduped across tests, only files that can resolve a URL).
        pic_indices = {}
        seen_files = set()
        counter = 0
        for t in tests:
            indices = []
            for f in t.files.all():
                if f.id in seen_files or not f.file:
                    continue
                seen_files.add(f.id)
                counter += 1
                indices.append(counter)
            pic_indices[t.id] = indices

        observations = []
        for t in tests:
            raw = t.surface_condition
            if not raw and t.notes:
                raw = cls._PROVENANCE_STAMP_RE.sub('', t.notes)
            raw = (raw or '').strip()
            if not raw:
                continue
            text = raw[0].upper() + raw[1:]
            if text[-1:] not in ('.', '!', '?'):
                text += '.'
            context = []
            if (t.test_location or '').strip():
                context.append(f'at {t.test_location.strip()}')
            element = (t.structural_element or '').strip()
            if element:
                context.append(f'on {element}')
            if context:
                text = (text[:-1] + ' observed ' + ' '.join(context) + '.'
                        if 'observ' not in raw.lower()
                        else text[:-1] + ' ' + ' '.join(context) + '.')
            pics = pic_indices.get(t.id) or []
            if pics:
                refs = ' & '.join(_to_roman(n).lower() for n in pics)
                label = 'pic' if len(pics) == 1 else 'pics'
                text = f'{text[:-1]} (see {label} {refs}).'
            observations.append(text)
        return observations

    @staticmethod
    def _storey_count(floors_present, bim_levels):
        """
        Distinct storeys evidenced by real records: the floors the pulse
        tests were grouped by and the levels carried on the project's
        imported BIM elements (Revit levels read like '0. NGL'). Unmapped
        labels (Roof, FLOOR NOT RECORDED) contribute nothing. Returns None
        when nothing is recorded — the building profile then stays
        unquantified rather than guessed.
        """
        floor_numbers = {
            'BASEMENT': -1, 'GROUND FLOOR': 0, 'GROUND': 0,
            'FIRST FLOOR': 1, 'SECOND FLOOR': 2, 'THIRD FLOOR': 3,
            'FOURTH FLOOR': 4, 'FIFTH FLOOR': 5,
        }
        storeys = set()
        for label in floors_present:
            storeys.add(floor_numbers.get((label or '').strip().upper()))
        for level in bim_levels:
            match = re.match(r'\s*(-?\d+)', level or '')
            if match:
                storeys.add(int(match.group(1)))
        storeys.discard(None)
        return len(storeys) or None

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

    @classmethod
    def _render_appendix(cls, builder, tests, start_index=1):
        """Reference-style appendix photograph pages — TWO photographs
        stacked per page at the reference's slots (x 34.9mm, w 146.3mm,
        first at y 16.9mm, second at y 126.4mm), each with its own
        'PIC <roman>: <caption>' line centred beneath it. Only real
        attached files appear; returns the count shown."""
        items = []
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
                if not url:
                    continue
                caption = (f.file_name or f.description
                           or f'Photograph {len(items) + 1}')
                items.append((url, caption))
        pdf = builder.pdf
        shown = 0
        for i in range(0, len(items), 2):
            pdf.add_page()
            if i == 0:
                # The TOC entry points at the first photograph page —
                # register the section once that page exists.
                pdf.start_section('PHOTOGRAPHS', level=1)
            for slot, (url, caption) in enumerate(items[i:i + 2]):
                y_img = 16.9 + slot * 109.5
                embedded = cls._boxed_image(pdf, url, 34.9, y_img,
                                            146.3, 100.0)
                label = (caption if embedded
                         else f'{caption} (IMAGE FILE NOT AVAILABLE)')
                pdf.set_xy(pdf.l_margin, y_img + 100.4)
                builder.photo_caption(cls._roman(start_index + shown), label)
                shown += 1
        return shown
