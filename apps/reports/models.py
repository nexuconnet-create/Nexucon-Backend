from django.conf import settings
from django.db import models
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
