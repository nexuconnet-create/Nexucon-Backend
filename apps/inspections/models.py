from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
from apps.permits.models import Permit
import uuid
import datetime

def generate_inspection_ref():
    return f"INS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_finding_ref():
    return f"FND-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_swo_number():
    return f"SWO-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"


class Checklist(models.Model):
    """
    Standard templates for different inspection disciplines.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    inspection_type = models.CharField(max_length=100, default='General Inspection')
    items = models.JSONField(default=list, help_text="List of standard verification items")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.name} ({self.inspection_type})"


class Inspection(models.Model):
    """
    Field inspections requested, scheduled, and executed across construction projects.
    """
    STATUS_CHOICES = (
        ('REQUESTED', 'Requested'),
        ('SCHEDULED', 'Scheduled'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('RE_INSPECTION_REQUIRED', 'Re-Inspection Required'),
        ('FAILED', 'Failed / Stop-Work Issued'),
        ('CANCELLED', 'Cancelled'),
    )

    PRIORITY_CHOICES = (
        ('Low', 'Low'),
        ('Normal', 'Normal'),
        ('Medium', 'Medium'),
        ('High', 'High'),
        ('Critical', 'Critical'),
    )

    OUTCOME_CHOICES = (
        ('PENDING', 'Pending Assessment'),
        ('PASSED', 'Passed'),
        ('CONDITIONAL_PASS', 'Conditional Pass'),
        ('FAILED', 'Failed / Violations Found'),
    )

    INSPECTION_TYPES = (
        ('Foundation Inspection', 'Foundation Inspection'),
        ('Structural Review', 'Structural Review'),
        ('Site Verification', 'Site Verification'),
        ('Safety Audit', 'Safety Audit'),
        ('MEP Inspection', 'MEP Inspection'),
        ('Drainage & Environmental', 'Drainage & Environmental'),
        ('Final Clearance', 'Final Clearance'),
        ('Re-Inspection', 'Re-Inspection'),
        ('Emergency Inspection', 'Emergency Inspection'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspection_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_inspection_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='inspections')
    permit = models.ForeignKey(Permit, on_delete=models.SET_NULL, null=True, blank=True, related_name='inspections')
    
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_inspections')
    inspector_name = models.CharField(max_length=255, blank=True, null=True, default='Unassigned')
    
    inspection_type = models.CharField(max_length=100, choices=INSPECTION_TYPES, default='Foundation Inspection')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='REQUESTED')
    priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Normal')
    
    requested_by_name = models.CharField(max_length=255, blank=True, null=True)
    requested_at = models.DateTimeField(default=timezone.now)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    completed_date = models.DateTimeField(null=True, blank=True)
    
    # Telemetry & GPS Check-in
    checkin_time = models.DateTimeField(null=True, blank=True)
    gps_latitude = models.FloatField(null=True, blank=True)
    gps_longitude = models.FloatField(null=True, blank=True)
    gps_verified = models.BooleanField(default=False)
    
    # Outcome & Findings
    outcome = models.CharField(max_length=50, choices=OUTCOME_CHOICES, default='PENDING')
    summary_notes = models.TextField(blank=True, null=True)
    checklist_results = models.JSONField(default=list, blank=True, help_text="Item by item checklist results")
    photos_and_evidence = models.JSONField(default=list, blank=True, help_text="Uploaded photos and evidence urls")
    
    # Re-inspection link
    parent_inspection = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='re_inspections')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_date', '-created_at']

    def __str__(self):
        return f"{self.inspection_reference} - {self.inspection_type} at {self.project.name} ({self.status})"


class Finding(models.Model):
    """
    Specific defect, non-conformance, or safety violation found during an inspection.
    """
    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical / Stop-Work'),
    )

    CATEGORY_CHOICES = (
        ('STRUCTURAL', 'Structural Integrity'),
        ('SAFETY', 'Site Safety & Scaffolding'),
        ('ENVIRONMENTAL', 'Environmental & Drainage'),
        ('MEP', 'Mechanical / Electrical / Plumbing'),
        ('PERMIT_DEVIATION', 'Permit & Drawing Deviation'),
        ('QUALITY', 'Material Quality & Workmanship'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_finding_ref)
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name='findings')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='findings')
    
    title = models.CharField(max_length=255, default='Site Finding')
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='LOW')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='STRUCTURAL')
    
    photos = models.JSONField(default=list, blank=True, help_text="Photo and evidence URLs")
    corrective_action_required = models.TextField(blank=True, null=True)
    resolution_deadline = models.DateField(null=True, blank=True)
    
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True, null=True)
    requires_reinspection = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.finding_reference} ({self.severity}) on {self.project.name}"


class StopWorkOrder(models.Model):
    """
    Critical regulatory enforcement order halting site construction.
    """
    STATUS_CHOICES = (
        ('ACTIVE', 'Active / Enforced'),
        ('APPEALED', 'Pending Appeal'),
        ('LIFTED', 'Lifted / Reinstated'),
        ('ENFORCED', 'Tribunal Escalation'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_swo_number)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='stop_work_orders')
    inspection = models.ForeignKey(Inspection, on_delete=models.SET_NULL, null=True, blank=True, related_name='stop_work_orders')
    finding = models.ForeignKey(Finding, on_delete=models.SET_NULL, null=True, blank=True, related_name='stop_work_orders')
    
    reason = models.TextField()
    severity = models.CharField(max_length=50, default='CRITICAL')
    issued_by_name = models.CharField(max_length=255, default='Building Control Directorate')
    issued_at = models.DateTimeField(default=timezone.now)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='ACTIVE')
    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by_name = models.CharField(max_length=255, blank=True, null=True)
    lift_justification = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        return f"{self.order_number} - {self.project.name} ({self.status})"
