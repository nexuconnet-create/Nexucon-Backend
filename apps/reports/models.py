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
