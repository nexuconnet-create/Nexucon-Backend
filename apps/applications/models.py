from django.db import models
from django.conf import settings
from apps.projects.models import Project
import uuid

class Application(models.Model):
    """
    Permit applications submitted by developers/contractors.
    """
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('SUBMITTED', 'Submitted'),
        ('UNDER_REVIEW', 'Under Review'),
        ('REVIEW_COMPLETED', 'Review Completed'),
        ('APPROVAL_REQUESTED', 'Approval Requested'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='applications')
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='applications')
    application_type = models.CharField(max_length=100)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    priority = models.CharField(max_length=50, default='Normal')
    
    # Assignment & Routing
    created_by_name = models.CharField(max_length=255, null=True, blank=True, help_text="Inspector/Director who routed this")
    assigned_reviewer_name = models.CharField(max_length=255, null=True, blank=True)
    
    submission_date = models.DateTimeField(null=True, blank=True)
    review_deadline = models.DateField(null=True, blank=True)
    required_action = models.TextField(null=True, blank=True)
    
    # JSON arrays for simple mocking
    review_items = models.JSONField(default=list, blank=True)
    attached_documents = models.JSONField(default=list, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.application_type} for {self.project.name} - {self.status}"
