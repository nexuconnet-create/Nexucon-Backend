from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
import uuid
import datetime

def generate_report_ref():
    return f"REP-{datetime.datetime.now().year}-{uuid.uuid4().hex[:4].upper()}"


class GeneratedReport(models.Model):
    """
    Exported or generated comprehensive PDF/CSV analytics reports.
    """
    TYPE_CHOICES = (
        ('Executive', 'Executive Summary Report'),
        ('Project', 'Project Performance & EVM Report'),
        ('Inspection', 'Inspection Analytics Report'),
        ('Compliance', 'Compliance & Regulatory Report'),
        ('Financial', 'Financial & Revenue Report'),
        ('Performance', 'Agency SLAs & Performance Report'),
        ('Custom', 'Custom Multi-Module Report'),
    )

    FORMAT_CHOICES = (
        ('PDF', 'PDF Document'),
        ('CSV', 'CSV Data Sheet'),
        ('JSON', 'JSON Data Payload'),
    )

    STATUS_CHOICES = (
        ('Pending', 'Pending Queue'),
        ('Generating', 'Generating Data'),
        ('Ready', 'Ready For Download'),
        ('Failed', 'Generation Failed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_report_ref)
    title = models.CharField(max_length=255)
    report_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default='Custom')
    format = models.CharField(max_length=20, choices=FORMAT_CHOICES, default='PDF')
    
    modules_included = models.JSONField(default=list, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Ready')
    file_url = models.CharField(max_length=500, blank=True, null=True)
    file_size = models.CharField(max_length=50, default='1.4 MB')
    
    generated_by_name = models.CharField(max_length=255, default='Director General')
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_reports')
    
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.report_reference}: {self.title} ({self.format} - {self.status})"


class DepartmentPerformanceMetric(models.Model):
    """
    Departmental turnaround metrics, review SLAs, and active workload counts.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department_name = models.CharField(max_length=255, unique=True)
    turnaround_days = models.DecimalField(max_digits=5, decimal_places=1, default=10.0)
    target_days = models.DecimalField(max_digits=5, decimal_places=1, default=14.0)
    efficiency_percentage = models.IntegerField(default=90)
    workload_level = models.CharField(max_length=50, default='High')
    pending_reviews_count = models.IntegerField(default=12)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['department_name']

    def __str__(self):
        return f"{self.department_name} ({self.efficiency_percentage}% Efficiency)"


class OfficerPerformanceRecord(models.Model):
    """
    Officer and inspector performance ranking and SLA completion rates.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    officer_name = models.CharField(max_length=255)
    role = models.CharField(max_length=255, default='Senior Inspector')
    inspections_completed = models.IntegerField(default=45)
    sla_adherence_rate = models.IntegerField(default=95)
    average_review_days = models.DecimalField(max_digits=4, decimal_places=1, default=3.2)
    rank = models.IntegerField(default=1)

    class Meta:
        ordering = ['rank']

    def __str__(self):
        return f"#{self.rank} {self.officer_name} ({self.role})"


class RiskAssessmentAlert(models.Model):
    """
    Structural and environmental collapse risk alerts across active projects.
    """
    RISK_LEVELS = (
        ('Critical', 'Critical Risk'),
        ('High', 'High Risk'),
        ('Medium', 'Medium Risk'),
        ('Low', 'Low Risk'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='risk_alerts', null=True, blank=True)
    structure_name = models.CharField(max_length=255)
    risk_score = models.IntegerField(default=75)
    risk_level = models.CharField(max_length=50, choices=RISK_LEVELS, default='High')
    primary_vulnerability = models.CharField(max_length=255, default='Foundation Settlement Anomaly')
    status = models.CharField(max_length=50, default='Active Alert')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-risk_score']

    def __str__(self):
        return f"{self.structure_name} (Score: {self.risk_score} - {self.risk_level})"
