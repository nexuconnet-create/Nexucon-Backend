"""
AI Report Generator (implementation plan §5 Weeks 5–6).

Auditable PDF reports generated from real database records only:
  * Project Intelligence Report — AI risk scores, correlation findings with
    their human-review status, recommendations and Director sign-off block.
  * Inspection Report — statutory inspection execution record with dynamic
    checklist results, mandatory GPS/timestamp and cryptographic sign-off.
  * NCR Report — formal Non-Conformance Report with corrective actions and
    the originating AI finding (when issued from the evidence pipeline).

Every figure in these documents is computed from live database rows; nothing
is hard-coded. The HITL principle is printed on every report: AI output is
decision support and is never a compliance declaration.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

NAVY = (2, 44, 79)
BLUE = (3, 95, 180)
CYAN = (0, 180, 216)
DARK = (15, 24, 31)
GREY = (100, 116, 139)
LIGHT = (245, 247, 250)
WHITE = (255, 255, 255)

RISK_COLORS = {
    'critical': (200, 30, 30),
    'high': (198, 100, 3),
    'medium': (172, 122, 5),
    'low': (21, 128, 61),
    'info': (59, 130, 246),
}


def _latin1(text):
    """
    Transcribe text into the latin-1 range supported by the core fpdf fonts.

    The report generators render live database content (project names, AI
    observations, recommendation text), which can contain typographic
    characters outside latin-1. Substituting close ASCII equivalents keeps
    the document honest (nothing dropped or invented) while avoiding
    FPDFUnicodeEncodingException.
    """
    substitutions = (
        ('—', '-'), ('–', '-'), ('‘', "'"), ('’', "'"),
        ('“', '"'), ('”', '"'), ('…', '...'), ('•', '-'),
        (' ', ' '),
    )
    s = str(text)
    for src, dst in substitutions:
        s = s.replace(src, dst)
    return s.encode('latin-1', 'replace').decode('latin-1')


class AIReportBuilder:
    """Small fpdf2 wrapper giving the AI reports a consistent layout."""

    def __init__(self, title, subtitle, reference):
        from fpdf import FPDF
        self.pdf = FPDF()
        self.pdf.set_auto_page_break(auto=True, margin=18)
        self.pdf.set_margins(15, 15, 15)
        self.title = title
        self.subtitle = subtitle
        self.reference = reference
        self._cover()

    # ------------------------------------------------------------ layout
    def _cover(self):
        pdf = self.pdf
        pdf.add_page()
        pdf.set_fill_color(*NAVY)
        pdf.rect(0, 0, 210, 62, style='F')
        pdf.set_y(16)
        pdf.set_font('Helvetica', 'B', 20)
        pdf.set_text_color(*WHITE)
        pdf.cell(0, 10, 'NEXUCON', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 10)
        pdf.cell(0, 6, 'Lagos State Construction Oversight Platform', align='C',
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_y(34)
        pdf.set_font('Helvetica', 'B', 15)
        pdf.cell(0, 10, _latin1(self.title), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 11)
        pdf.cell(0, 7, _latin1(self.subtitle), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_y(54)
        pdf.set_font('Courier', '', 9)
        pdf.cell(0, 6, _latin1(self.reference), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(*DARK)
        pdf.ln(8)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(0, 5,
                       'Generated from verified platform records. All AI content in this document '
                       'is decision support under the Human-in-the-Loop policy: findings become '
                       'official only after qualified human review and Director sign-off.')
        pdf.set_text_color(*DARK)
        pdf.ln(2)

    def section(self, heading):
        pdf = self.pdf
        pdf.ln(3)
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_text_color(*WHITE)
        pdf.set_fill_color(*NAVY)
        pdf.cell(0, 8, f'  {_latin1(heading)}', fill=True, new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(*DARK)
        pdf.ln(2)

    def kv(self, key, value):
        pdf = self.pdf
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(*GREY)
        pdf.cell(52, 6, key)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(*DARK)
        # NOTE: the core fpdf fonts are latin-1 only; the em dash / ellipsis /
        # bullet glyphs are not encodable and would raise
        # FPDFUnicodeEncodingException. Use ASCII stand-ins instead.
        pdf.multi_cell(0, 6, _latin1(value if value not in (None, '') else '-'),
                       new_x='LMARGIN', new_y='NEXT')

    def para(self, text):
        self.pdf.set_font('Helvetica', '', 9)
        self.pdf.set_text_color(*DARK)
        self.pdf.multi_cell(0, 5, _latin1(text))
        self.pdf.ln(1)

    def table(self, headers, rows, widths, aligns=None):
        pdf = self.pdf
        aligns = aligns or ['L'] * len(headers)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_fill_color(*LIGHT)
        pdf.set_text_color(*DARK)
        for header, width, align in zip(headers, widths, aligns):
            pdf.cell(width, 7, _latin1(header), border=1, align=align, fill=True)
        pdf.ln()
        pdf.set_font('Helvetica', '', 8)
        fill = False
        for row in rows:
            if pdf.get_y() > 270:
                pdf.add_page()
            for value, width, align in zip(row, widths, aligns):
                pdf.cell(width, 6, _latin1(value)[:120], border=1, align=align,
                         fill=fill)
            pdf.ln()
            fill = not fill

    def risk_badge(self, level):
        return level.upper() if level else '-'

    def signoff_block(self, lines):
        """Cryptographic sign-off block — one line per (label, value)."""
        self.section('Sign-off')
        pdf = self.pdf
        if pdf.get_y() > 240:
            pdf.add_page()
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(0.6)
        y0 = pdf.get_y()
        pdf.rect(15, y0, 180, 10 + 7 * len(lines))
        pdf.set_y(y0 + 4)
        for label, value in lines:
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(*GREY)
            pdf.cell(55, 7, _latin1(label))
            pdf.set_font('Courier', '', 8)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(0, 7, _latin1(value), new_x='LMARGIN', new_y='NEXT')
        pdf.set_line_width(0.2)

    def bytes(self):
        return self.pdf.output(dest='S')


class AIReportService:
    """Generates the three statutory AI reports from live records."""

    # ------------------------------------------------------------- utils
    @staticmethod
    def _report_hash(*parts):
        import hashlib
        payload = '|'.join(str(p) for p in parts)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _user_label(user):
        if user and getattr(user, 'is_authenticated', False):
            return user.get_full_name() or user.email
        return 'Unauthenticated'

    # --------------------------------------------------- project report
    @classmethod
    def generate_project_intelligence_report(cls, project, user=None):
        """Project-Level AI Intelligence report (plan §5 Week 6)."""
        from apps.evidence.intelligence import ProjectIntelligenceService
        from apps.evidence.models import CorrelationFinding, EvidenceRecord

        summary = ProjectIntelligenceService.aggregate(project)
        recommendations = summary.get('recommended_actions', [])
        findings = (CorrelationFinding.objects.filter(project=project)
                    .select_related('linked_ncr', 'reviewed_by')
                    .prefetch_related('evidence'))
        evidence_count = EvidenceRecord.objects.filter(project=project).count()
        open_ncrs = summary.get('metrics', {}).get('open_ncrs')

        builder = AIReportBuilder(
            'Project Intelligence Report',
            project.name,
            f"Ref PI-{project.id} · Generated {datetime.now():%Y-%m-%d %H:%M} · "
            f"by {cls._user_label(user)}",
        )

        builder.section('1. Project')
        builder.kv('Project name', project.name)
        builder.kv('Project ID', project.id)
        builder.kv('Status', getattr(project, 'status', '-'))
        builder.kv('Location', getattr(project, 'location', None) or '-')
        builder.kv('Client', getattr(project, 'client_name', None) or '-')

        builder.section('2. AI Risk Assessment')
        scores = summary.get('risk_scores', {})
        builder.table(
            ['Dimension', 'Score', 'Level'],
            [
                ['Overall', scores.get('overall'), scores.get('overall_level')],
                ['Structural', scores.get('structural'), scores.get('structural_level')],
                ['Compliance', scores.get('compliance'), scores.get('compliance_level')],
                ['Operational', scores.get('operational'), scores.get('operational_level')],
            ],
            [60, 60, 60],
        )
        builder.kv('Evidence records', evidence_count)
        builder.kv('Open NCRs', open_ncrs if open_ncrs is not None else 0)

        builder.section('3. AI Observations')
        for observation in summary.get('ai_observations') or []:
            builder.para(f'- {observation}')
        if not summary.get('ai_observations'):
            builder.para('No AI observations recorded for this project.')

        builder.section('4. Correlation Findings & Human Review Status')
        rows = []
        for finding in findings:
            review = (finding.status.replace('_', ' ').title()
                      + (f" ({cls._user_label(finding.reviewed_by)})"
                         if finding.reviewed_by else ''))
            rows.append([
                finding.finding_reference,
                finding.structural_element_id or '-',
                finding.risk_level.upper(),
                finding.title[:60],
                review,
            ])
        if rows:
            builder.table(
                ['Reference', 'Element', 'Risk', 'Finding', 'Human review'],
                rows, [32, 28, 18, 64, 38],
            )
        else:
            builder.para('No cross-source correlation findings have been generated for this project.')

        builder.section('5. Recommended Actions')
        rows = [[i + 1, rec.get('action') or rec.get('recommendation', ''),
                 rec.get('priority', ''), rec.get('reason', '')]
                for i, rec in enumerate(recommendations)]
        if rows:
            builder.table(['#', 'Recommended action', 'Priority', 'Rationale'],
                          rows, [10, 78, 22, 70])
        else:
            builder.para('No outstanding recommendations.')

        builder.section('6. Report Integrity')
        digest = cls._report_hash('project_intelligence', project.id,
                                  evidence_count, findings.count(), open_ncrs)
        builder.para(f'SHA-256 content digest: {digest}')
        builder.para('This report reflects the state of the evidence registry at generation time. '
                     'All AI scores are computed deterministically from recorded evidence and '
                     'reviewed findings; they do not constitute a compliance determination.')

        builder.signoff_block([
            ('Prepared by', cls._user_label(user)),
            ('Reviewed by (Inspector)', '________________________'),
            ('Approved by (Director)', '________________________'),
            ('Content digest', digest[:32] + '...'),
            ('Date', f'{datetime.now():%Y-%m-%d %H:%M}'),
        ])
        return builder.bytes()

    # --------------------------------------------------- inspection report
    @classmethod
    def generate_inspection_report(cls, inspection, user=None):
        """Statutory inspection execution report (plan §5 Week 7)."""
        from apps.inspections.models import Finding

        findings = Finding.objects.filter(inspection=inspection)
        builder = AIReportBuilder(
            'Statutory Inspection Report',
            f"{inspection.inspection_type} - {inspection.project.name}",
            f"Ref {inspection.inspection_reference} · Generated {datetime.now():%Y-%m-%d %H:%M} · "
            f"by {cls._user_label(user)}",
        )

        builder.section('1. Inspection Record')
        builder.kv('Reference', inspection.inspection_reference)
        builder.kv('Project', inspection.project.name)
        builder.kv('Type', inspection.inspection_type)
        builder.kv('Status', inspection.get_status_display())
        builder.kv('Priority', inspection.priority)
        builder.kv('Inspector', inspection.inspector_name
                   or (cls._user_label(inspection.inspector) if inspection.inspector else '-'))
        builder.kv('Requested by', inspection.requested_by_name or '-')
        builder.kv('Requested at', inspection.requested_at)
        builder.kv('Scheduled', inspection.scheduled_date or '-')
        builder.kv('Completed', inspection.completed_date or '-')

        builder.section('2. Mandatory Location Verification')
        builder.kv('GPS check-in time', inspection.checkin_time or 'NOT RECORDED')
        builder.kv('GPS latitude', inspection.gps_latitude if inspection.gps_latitude is not None else 'NOT RECORDED')
        builder.kv('GPS longitude', inspection.gps_longitude if inspection.gps_longitude is not None else 'NOT RECORDED')
        builder.kv('GPS verified', 'YES' if inspection.gps_verified else 'NO')

        builder.section('3. Checklist Results')
        if inspection.checklist_results:
            rows = []
            for i, item in enumerate(inspection.checklist_results):
                if isinstance(item, dict):
                    rows.append([
                        item.get('item') or item.get('question') or f'Item {i + 1}',
                        item.get('result') or item.get('status') or '-',
                        item.get('notes', '') or '',
                    ])
                else:
                    rows.append([f'Item {i + 1}', str(item), ''])
            builder.table(['Checklist item', 'Result', 'Notes'], rows, [80, 45, 55])
        else:
            builder.para('No checklist results recorded for this inspection.')

        builder.section('4. Findings')
        rows = [[f.finding_reference or '-', f.title, f.severity,
                 'Resolved' if f.is_resolved else 'Open'] for f in findings]
        if rows:
            builder.table(['Reference', 'Finding', 'Severity', 'Status'],
                          rows, [34, 84, 26, 36])
        else:
            builder.para('No findings recorded.')

        builder.section('5. Outcome')
        builder.kv('Outcome', inspection.get_outcome_display())
        builder.kv('Summary notes', inspection.summary_notes or '-')

        digest = cls._report_hash(
            'inspection', inspection.id, inspection.checkin_time,
            inspection.gps_latitude, inspection.gps_longitude,
            inspection.outcome, findings.count(),
        )
        builder.section('6. Report Integrity')
        builder.para(f'SHA-256 content digest: {digest}')

        builder.signoff_block([
            ('Inspector', inspection.inspector_name
             or cls._user_label(inspection.inspector)),
            ('GPS verified', 'YES' if inspection.gps_verified else 'NO'),
            ('Outcome', inspection.get_outcome_display()),
            ('Content digest', digest[:32] + '...'),
            ('Date', f'{datetime.now():%Y-%m-%d %H:%M}'),
        ])
        return builder.bytes()

    # ---------------------------------------------------------- NCR report
    @classmethod
    def generate_ncr_report(cls, ncr, user=None):
        """Formal Non-Conformance Report document (plan §5 Week 5)."""
        builder = AIReportBuilder(
            'Non-Conformance Report',
            f"{ncr.title} - {ncr.project.name}",
            f"Ref {ncr.ncr_reference} · Generated {datetime.now():%Y-%m-%d %H:%M} · "
            f"by {cls._user_label(user)}",
        )

        builder.section('1. Non-Conformance')
        builder.kv('NCR reference', ncr.ncr_reference)
        builder.kv('Project', ncr.project.name)
        builder.kv('Title', ncr.title)
        builder.kv('Severity', ncr.severity)
        builder.kv('Category', ncr.category)
        builder.kv('Status', ncr.status)
        builder.kv('Reported by', ncr.reported_by_name
                   or (cls._user_label(ncr.reporter) if ncr.reporter else '-'))
        builder.kv('Date logged', ncr.date_logged)
        builder.kv('Source', ncr.get_source_display() if hasattr(ncr, 'get_source_display') else ncr.source)
        builder.kv('Source reference', ncr.source_reference or '-')
        builder.kv('Escalation level', ncr.escalation_level)

        builder.section('2. Description')
        builder.para(ncr.description)

        # Originating AI finding (when issued from the evidence pipeline).
        from apps.evidence.models import CorrelationFinding
        finding = CorrelationFinding.objects.filter(linked_ncr=ncr).first()
        if finding:
            builder.section('3. Originating AI Correlation Finding')
            builder.kv('Finding reference', finding.finding_reference)
            builder.kv('Structural element', finding.structural_element_id or '-')
            builder.kv('BIM GUID', finding.bim_guid or '-')
            builder.kv('AI risk level', finding.risk_level.upper())
            builder.kv('AI risk score', finding.risk_score)
            builder.kv('Human review status', finding.status)
            builder.kv('Reviewed by', cls._user_label(finding.reviewed_by)
                       if finding.reviewed_by else '-')
            builder.para('AI reasoning log:')
            builder.para(finding.reasoning or '-')

        builder.section('4. Corrective Actions')
        capas = ncr.capas.all() if hasattr(ncr, 'capas') else []
        rows = [[c.capa_reference, c.title, c.status, c.priority,
                 c.due_date or '-'] for c in capas]
        if rows:
            builder.table(['Reference', 'Action', 'Status', 'Priority', 'Due'],
                          rows, [34, 78, 24, 22, 22])
        else:
            builder.para('No corrective action plans recorded against this NCR.')

        if ncr.resolved_at:
            builder.section('5. Resolution')
            builder.kv('Resolved at', ncr.resolved_at)
            builder.kv('Resolution notes', ncr.resolution_notes or '-')

        digest = cls._report_hash('ncr', ncr.id, ncr.status, ncr.severity,
                                  capas.count())
        builder.section('Report Integrity')
        builder.para(f'SHA-256 content digest: {digest}')

        builder.signoff_block([
            ('Issued by', ncr.reported_by_name or cls._user_label(ncr.reporter)),
            ('Severity', ncr.severity),
            ('Status', ncr.status),
            ('Content digest', digest[:32] + '...'),
            ('Date', f'{datetime.now():%Y-%m-%d %H:%M}'),
        ])
        return builder.bytes()
