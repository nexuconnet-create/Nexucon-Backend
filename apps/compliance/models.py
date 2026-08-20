from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
import uuid
import datetime

def generate_ncr_ref():
    return f"NCR-{uuid.uuid4().hex[:3].upper()}"

def generate_capa_ref():
    return f"CAPA-{uuid.uuid4().hex[:3].upper()}"

def generate_rev_ref():
    return f"REV-{datetime.datetime.now().year % 100}-{uuid.uuid4().hex[:3].upper()}"

def generate_cert_ref():
    return f"CERT-{datetime.datetime.now().year}-{uuid.uuid4().hex[:4].upper()}"


class NonConformanceReport(models.Model):
    """
    Tracks safety, environmental, structural, and regulatory deviations (NCRs).
    """
    SEVERITY_CHOICES = (
        ('Critical', 'Critical (Stop-Work Risk)'),
        ('Major', 'Major (High Non-Conformance)'),
        ('Minor', 'Minor (Standard Deviation)'),
    )

    CATEGORY_CHOICES = (
        ('Environmental', 'Environmental'),
        ('Safety', 'Safety & Health'),
        ('Quality', 'Quality Assurance / Materials'),
        ('Structural', 'Structural & Engineering'),
        ('General', 'General Compliance'),
    )

    STATUS_CHOICES = (
        ('Open', 'Open'),
        ('In Progress', 'In Progress'),
        ('Closed', 'Closed / Resolved'),
    )

    SOURCE_CHOICES = (
        ('INSPECTION', 'Field Inspection Finding'),
        ('SITE_MONITORING', 'Site Monitoring / Defect'),
        ('BIM_CLASH', 'BIM Model Clash Interference'),
        ('GPR_SCAN', 'Subsurface / GPR Anomaly'),
        ('PERMIT_REVIEW', 'Permit Review Non-Conformance'),
        ('MANUAL', 'Direct Regulatory Citation'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ncr_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_ncr_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='ncrs')
    
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(max_length=50, choices=SEVERITY_CHOICES, default='Major')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Safety')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Open')
    
    reported_by_name = models.CharField(max_length=255, default='J. Doe (Safety)')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reported_ncrs')
    assignee_name = models.CharField(max_length=255, default='Lead Contractor')
    
    source = models.CharField(max_length=50, choices=SOURCE_CHOICES, default='MANUAL')
    source_reference = models.CharField(max_length=100, blank=True, null=True)
    
    # Regulatory Escalation Matrix (Level 1: Reminder, Level 2: Warning, Level 3: Sr. Officer, Level 4: Director, Level 5: Legal Action)
    escalation_level = models.IntegerField(default=1)
    last_escalated_at = models.DateTimeField(null=True, blank=True)
    
    date_logged = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date_logged']

    def __str__(self):
        return f"{self.ncr_reference}: {self.title} ({self.severity} - {self.status})"

    @property
    def days_open(self):
        if self.status == 'Closed' and self.resolved_at:
            delta = self.resolved_at - self.date_logged
        else:
            delta = timezone.now() - self.date_logged
        return max(0, delta.days)


class CorrectiveActionPlan(models.Model):
    """
    Corrective & Preventive Actions (CAPA) tracking task workflows on Kanban board.
    """
    PRIORITY_CHOICES = (
        ('Critical', 'Critical'),
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    )

    STATUS_CHOICES = (
        ('todo', 'To Do'),
        ('in-progress', 'In Progress'),
        ('review', 'Under Review'),
        ('closed', 'Closed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capa_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_capa_ref)
    ncr = models.ForeignKey(NonConformanceReport, on_delete=models.SET_NULL, null=True, blank=True, related_name='capas')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='capas')
    
    title = models.CharField(max_length=255)
    action_plan = models.TextField(blank=True, null=True)
    priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='High')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='todo')
    
    assignee_name = models.CharField(max_length=255, default='HSE Officer')
    due_date = models.DateField(null=True, blank=True)
    
    comments_count = models.IntegerField(default=0)
    attachments_count = models.IntegerField(default=0)
    
    verification_notes = models.TextField(blank=True, null=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.capa_reference}: {self.title} ({self.status})"


class RegulatoryRequirement(models.Model):
    """
    Specific building codes, statutory clauses, and standards required for compliance.
    """
    CATEGORY_CHOICES = (
        ('Environmental', 'Environmental Standards'),
        ('Safety & Health', 'Safety & Occupational Health'),
        ('Building Codes', 'National Building Codes'),
        ('Legal & Planning', 'Planning & Urban Regulations'),
    )

    STATUS_CHOICES = (
        ('Compliant', 'Compliant'),
        ('At Risk', 'At Risk'),
        ('Non-Compliant', 'Non-Compliant'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requirement_reference = models.CharField(max_length=100, db_index=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Environmental')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    authority = models.CharField(max_length=255, default='EPA')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Compliant')
    last_checked = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['category', 'requirement_reference']

    def __str__(self):
        return f"{self.requirement_reference}: {self.title} ({self.status})"


class ComplianceReview(models.Model):
    """
    Lifecycle management for ongoing and scheduled compliance audits.
    """
    STAGE_CHOICES = (
        ('Initiation', 'Initiation'),
        ('Audit in Progress', 'Audit in Progress'),
        ('Reporting', 'Reporting'),
        ('Final Review', 'Final Review'),
        ('Completed', 'Completed'),
    )

    REVIEW_TYPES = (
        ('Safety', 'Safety Audit'),
        ('Building Code', 'Building Code Verification'),
        ('Environmental', 'Environmental Impact Check'),
        ('Quality', 'Quality Assurance Review'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_rev_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='compliance_reviews')
    
    title = models.CharField(max_length=255)
    review_type = models.CharField(max_length=50, choices=REVIEW_TYPES, default='Safety')
    auditor_name = models.CharField(max_length=255, default='J. Doe (Lead)')
    stage = models.CharField(max_length=50, choices=STAGE_CHOICES, default='Initiation')
    progress = models.IntegerField(default=10)
    
    start_date = models.DateField(default=timezone.now)
    due_date = models.DateField(null=True, blank=True)
    findings_summary = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.review_reference}: {self.title} ({self.stage} - {self.progress}%)"


class ComplianceCertificate(models.Model):
    """
    Official awarded regulatory compliance certificates with tamper-proof QR verification hash.
    """
    CATEGORY_CHOICES = (
        ('Environmental', 'Environmental Clearance'),
        ('Safety', 'Fire & Site Safety'),
        ('Quality', 'Quality Management'),
        ('Building Code', 'Structural Fitness Certificate'),
    )

    STATUS_CHOICES = (
        ('Active', 'Active / Valid'),
        ('Expiring Soon', 'Expiring Soon (<30 Days)'),
        ('Expired', 'Expired'),
        ('Revoked', 'Revoked / Suspended'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    certificate_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_cert_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='compliance_certificates')
    
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Environmental')
    authority = models.CharField(max_length=255, default='Environmental Protection Agency (EPA)')
    
    issue_date = models.DateField(default=timezone.now)
    expiry_date = models.DateField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Active')
    
    qr_verification_hash = models.CharField(max_length=255, blank=True, null=True)
    certificate_file_url = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-issue_date']

    def __str__(self):
        return f"{self.certificate_reference}: {self.title} ({self.status})"
