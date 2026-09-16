from django.conf import settings
from django.db import models
from django.db.models import Q
import uuid
from apps.scans.models import ScanSession


class ReportTemplate(models.Model):
    """
    Catalogue of report types the platform can produce. Drives the template
    picker on the reports dashboard instead of a hardcoded client-side list.
    """
    REPORT_TYPES = [
        ('progress', 'Progress Report'),
        ('deviation', 'Deviation Analysis'),
        ('qaqc', 'QA/QC Summary'),
        ('earthworks', 'Earthworks Volume'),
        ('compliance', 'Compliance Summary'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='')
    report_type = models.CharField(max_length=30, choices=REPORT_TYPES)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class QualityReport(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('generating', 'Generating'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scan = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='reports')
    project_id = models.UUIDField(null=True, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    report_type = models.CharField(
        max_length=30, choices=ReportTemplate.REPORT_TYPES, default='qaqc',
        help_text="Which template the report was generated from; drives the sections included in the PDF.",
    )
    summary = models.JSONField(null=True, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    report_url = models.URLField(max_length=500, blank=True, null=True)
    report_version = models.CharField(max_length=20, default='1.0')
    ai_model_version = models.CharField(max_length=50, default='NEXUCON-AI v1.2')
    
    # Aggregated metrics for report ease-of-use
    defect_count = models.IntegerField(default=0)
    anomaly_count = models.IntegerField(default=0)
    mean_deviation = models.FloatField(null=True, blank=True)
    overall_ai_confidence = models.FloatField(null=True, blank=True, help_text="AI confidence for the generated text/recommendations")

    def __str__(self):
        return f"Report {self.id} for Scan {self.scan.id} ({self.status})"


class ArchivedReport(models.Model):
    """
    A statutory dossier archived exactly as generated: the precise PDF bytes
    the platform produced, sealed with a SHA-256 checksum so an auditor can
    prove the archived document was never altered. ``content_key`` is the
    deterministic digest of the underlying records (the same parts the
    report's integrity section hashes) — PDF bytes alone are not reproducible
    because the PDF info dictionary carries a creation timestamp, so identical
    content is deduped on ``content_key``; the byte checksum remains as
    tamper evidence for the stored file. Regenerating a report after new
    field data arrives produces a new archive row.
    """
    KIND_CHOICES = [
        ('ndt', 'NDT / PUNDIT (BS 1881-203)'),
    ]
    COMPLIANCE_CHOICES = [
        ('COMPLIANT', 'All strength-assessed tests pass the 25 MPa threshold'),
        ('FLAGGED_DEFECTS', 'Deficient tests recorded'),
        ('NOT_ASSESSED', 'No strength-assessed tests in this dossier'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE,
                                related_name='archived_reports')
    report_kind = models.CharField(max_length=30, choices=KIND_CHOICES, default='ndt')
    report_reference = models.CharField(max_length=100)
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='reports/ndt/%Y/%m/', blank=True)
    file_size_bytes = models.BigIntegerField(default=0)
    sha256_checksum = models.CharField(max_length=64, db_index=True)
    content_key = models.CharField(
        max_length=64, db_index=True,
        help_text='Deterministic digest of the underlying records (what the '
                  "report's integrity section hashes) — used to dedupe "
                  're-generations of identical content.')
    test_count = models.IntegerField(default=0)
    assessed_count = models.IntegerField(default=0)
    passed_count = models.IntegerField(default=0)
    compliance_status = models.CharField(max_length=20, choices=COMPLIANCE_CHOICES,
                                         default='NOT_ASSESSED')
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='archived_reports')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['project', 'report_kind', 'content_key'],
                                    name='uniq_archived_report_content'),
        ]

    def __str__(self):
        return f"{self.report_reference} [{self.report_kind}] ({self.project_id})"


class ReportVersion(models.Model):
    """
    Version Control & Feedback Loop for Reports.
    Allows tracking revisions of reports, with AI feedback capturing context for regenerations.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    archived_report = models.ForeignKey(ArchivedReport, on_delete=models.CASCADE, related_name='versions')
    version_string = models.CharField(max_length=20, default='1.0')
    ai_feedback_context = models.TextField(blank=True, default='', help_text="User feedback/context for AI regeneration")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.archived_report.report_reference} v{self.version_string}"


class ReportSectionOverride(models.Model):
    """
    Report CMS (8 Sep meeting H7 / 4 Sep C4): an editable override for
    one boilerplate prose section of the NDT report. ``project`` NULL
    means the override applies platform-wide (every project without
    its own override); a project FK scopes it to that project alone.
    Valid keys live in ``apps.reports.report_cms.CMS_SECTIONS`` — the
    registry, not this table, holds the defaults.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE,
                                null=True, blank=True,
                                related_name='report_section_overrides')
    section_key = models.CharField(max_length=60)
    body = models.TextField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name='report_section_overrides')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['section_key']
        constraints = [
            models.UniqueConstraint(
                fields=['section_key'],
                condition=Q(project__isnull=True),
                name='uniq_platform_report_section_override'),
            models.UniqueConstraint(
                fields=['project', 'section_key'],
                name='uniq_project_report_section_override'),
        ]

    def __str__(self):
        scope = f'project {self.project_id}' if self.project_id else 'platform'
        return f'{self.section_key} ({scope})'


class ReportCMSPassword(models.Model):
    """
    Singleton credential guarding report-CMS edits (the password
    protection the 8 Sep client review asked for). Only the hash is
    stored — Django's make_password/check_password, same scheme as
    user passwords. One row at most; the first row set wins until it
    is changed through the API.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    password_hash = models.CharField(max_length=128)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                               on_delete=models.SET_NULL,
                               null=True, blank=True,
                               related_name='report_cms_passwords')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Report CMS password'


class ReportBranding(models.Model):
    """
    Optional logo / watermark branding for a project's statutory NDT
    report (REFINED EXECUTIVE SUMMARY §2.3). Only real uploaded images —
    nothing is seeded. One row per project at most; no row means the
    report renders with the platform's standard laboratory layout
    (the Lagos State coat of arms and the LSMTL watermark), exactly as
    the reference template requires.
    """
    LOGO_POSITIONS = [
        ('top-left', 'Top left'),
        ('top-right', 'Top right'),
        ('bottom-left', 'Bottom left'),
        ('bottom-right', 'Bottom right'),
        ('center', 'Centre'),
    ]
    SIZES = [('small', 'Small'), ('medium', 'Medium'), ('large', 'Large')]
    SIZE_WIDTHS_MM = {'small': 20.0, 'medium': 30.0, 'large': 42.0}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE,
                                   related_name='report_branding')
    logo = models.FileField(
        upload_to='reports/branding/%Y/%m/', blank=True, default='',
        help_text='Client/consultant logo stamped on the report (PNG with '
                  'transparency recommended)')
    logo_position = models.CharField(max_length=20, choices=LOGO_POSITIONS,
                                     default='top-right')
    logo_size = models.CharField(max_length=20, choices=SIZES,
                                 default='medium')
    # Cover logo (12 Sep 2026): the Lagos State coat of arms drawn top-left
    # of the cover is the laboratory default. A project may REPLACE it with
    # its own image or HIDE it entirely — three honest states, nothing
    # invented: custom image, no logo, or the statutory default.
    cover_logo = models.FileField(
        upload_to='reports/branding/%Y/%m/', blank=True, default='',
        help_text='Replaces the default Lagos State coat of arms on the '
                  'report cover (drawn in the same top-left position)')
    cover_logo_hidden = models.BooleanField(
        default=False,
        help_text='When True the cover carries no logo at all — the default '
                  'coat of arms is not drawn either')
    watermark = models.FileField(
        upload_to='reports/branding/%Y/%m/', blank=True, default='',
        help_text='Optional additional watermark image centred behind the '
                  'page body at the chosen opacity')
    watermark_opacity_pct = models.PositiveIntegerField(
        default=50,
        help_text='Watermark opacity 0-100 (applied at render time)')
    watermark_position = models.CharField(max_length=20,
                                          choices=[('center', 'Centre'),
                                                   ('top-left', 'Top left'),
                                                   ('top-right', 'Top right')],
                                          default='center')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name='report_branding_edits')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Report branding — {self.project_id}'


class ReportSectionConfig(models.Model):
    """
    Per-project report structure (REFINED EXECUTIVE SUMMARY §2.5 —
    document flexibility): the section order, the enable/disable state of
    each built-in section, and the project's own custom sections.

    A project with NO rows resolves to the canonical default structure
    (report_structure.DEFAULT_STRUCTURE — the template order, everything
    enabled), so pre-existing projects keep the exact document they always
    had. The first structure change materialises the complete default set
    (report_structure.ensure_structure_rows); from then on the rows are
    the truth. Built-in sections carry only key/is_enabled/display_order;
    custom sections (is_custom=True) additionally carry their title/body.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE,
                                related_name='report_section_configs')
    section_key = models.CharField(max_length=80)
    is_custom = models.BooleanField(default=False)
    title = models.CharField(max_length=200, blank=True, default='')
    body = models.TextField(blank=True, default='')
    is_enabled = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name='report_section_edits')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'section_key'],
                name='uniq_project_report_section_config'),
        ]

    def __str__(self):
        state = 'off' if not self.is_enabled else 'on'
        kind = 'custom' if self.is_custom else 'built-in'
        return f'{self.section_key} ({kind}, {state}) — {self.project_id}'


class ReportSignOff(models.Model):
    """
    Approving-engineer credentials for a project's statutory NDT report
    (C11, 4 Sep meeting): the corroborating COREN-registered engineer who
    gives final input before the report is issued (client principle 5).

    Every field is recorded by a Director for the project — nothing is
    seeded and nothing is derived. No row (or a blank field) means the
    sign-off block simply leaves that line blank, exactly as before; the
    engineer-review clause in §7.0 prints regardless because the review
    requirement itself is statutory, not a property of the data.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE,
                                   related_name='report_signoff')
    approved_by_name = models.CharField(
        max_length=150, blank=True, default='',
        help_text='The COREN-registered engineer approving the report')
    qualification = models.CharField(
        max_length=150, blank=True, default='',
        help_text="e.g. 'B.Sc (Eng), M.Sc, MNSE' — as recorded")
    coren_registration_no = models.CharField(
        max_length=60, blank=True, default='',
        help_text='COREN registration number of the approving engineer')
    firm_name = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Company / firm of the approving engineer')
    signature_image = models.FileField(
        upload_to='reports/signoff/%Y/%m/', blank=True, default='',
        help_text='Optional scanned signature image of the approving engineer')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name='report_signoff_edits')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Report sign-off — {self.project_id}'
