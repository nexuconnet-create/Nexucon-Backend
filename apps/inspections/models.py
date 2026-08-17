from django.db import models
from apps.projects.models import Project
from apps.stakeholders.models import Inspector
import uuid

class Inspection(models.Model):
    """
    Field inspections requested or scheduled.
    """
    STATUS_CHOICES = (
        ('REQUESTED', 'Requested'),
        ('SCHEDULED', 'Scheduled'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed / Stop-Work Issued'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='inspections')
    inspector = models.ForeignKey(Inspector, on_delete=models.SET_NULL, null=True, related_name='inspections_assigned')
    
    inspection_type = models.CharField(max_length=100)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='REQUESTED')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.inspection_type} at {self.project.name} - {self.status}"


class Checklist(models.Model):
    """
    Templates for different inspection types.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    items = models.JSONField(default=list, help_text="List of items to check")
    
    def __str__(self):
        return self.name


class Finding(models.Model):
    """
    Specific issues found during an inspection.
    """
    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical / Stop-Work'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name='findings')
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='LOW')
    requires_reinspection = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.severity} Finding on {self.inspection.project.name}"
