from django.db import models
from django.conf import settings
from apps.projects.models import Project
import uuid
import datetime

def generate_application_ref():
    return f"APP-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

class Application(models.Model):
    """
    Permit applications submitted by developers/contractors for regulatory approval.
    """
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('SUBMITTED', 'Submitted'),
        ('UNDER_REVIEW', 'Under Review'),
        ('REVIEW_COMPLETED', 'Review Completed'),
        ('APPROVAL_REQUESTED', 'Approval Requested'),
        ('CONDITIONAL_APPROVAL', 'Conditional Approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
        ('RENEWED', 'Renewed'),
    )

    PRIORITY_CHOICES = (
        ('Low', 'Low'),
        ('Normal', 'Normal'),
        ('Medium', 'Medium'),
        ('High', 'High'),
        ('Critical', 'Critical'),
    )

    FEE_STATUS_CHOICES = (
        ('UNPAID', 'Unpaid'),
        ('PAID', 'Paid'),
        ('WAIVED', 'Waived'),
        ('REFUNDED', 'Refunded'),
    )

    APPLICATION_TYPES = (
        ('Building Permit', 'Building Permit'),
        ('Renovation Permit', 'Renovation Permit'),
        ('Planning Approval', 'Planning Approval'),
        ('Demolition Permit', 'Demolition Permit'),
        ('Structural Approval', 'Structural Approval'),
        ('Environmental Clearance', 'Environmental Clearance'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application_reference = models.CharField(max_length=100, unique=True, default=generate_application_ref)
    title = models.CharField(max_length=255, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='applications')
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='applications')
    application_type = models.CharField(max_length=100, default='Building Permit')
    jurisdiction = models.CharField(max_length=150, blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='SUBMITTED')
    priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Normal')
    
    # Financials
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    fee_status = models.CharField(max_length=50, choices=FEE_STATUS_CHOICES, default='UNPAID')
    
    # Assignment & Routing
    created_by_name = models.CharField(max_length=255, null=True, blank=True, help_text="Officer/Director who logged this")
    assigned_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_applications')
    assigned_reviewer_name = models.CharField(max_length=255, null=True, blank=True)
    
    # Dates
    submission_date = models.DateTimeField(null=True, blank=True, default=datetime.datetime.now)
    review_deadline = models.DateField(null=True, blank=True)
    decision_date = models.DateTimeField(null=True, blank=True)
    
    # Decision details
    decision_reason = models.TextField(null=True, blank=True)
    conditions = models.TextField(null=True, blank=True, help_text="Requirements for Conditional Approval")
    required_action = models.TextField(null=True, blank=True)
    
    # JSON metadata stores
    review_items = models.JSONField(default=list, blank=True, help_text="Review criteria checklist with status and notes")
    attached_documents = models.JSONField(default=list, blank=True, help_text="Uploaded architectural, structural, and EIA documents")
    document_requests = models.JSONField(default=list, blank=True, help_text="List of formal document requests sent to applicant")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.application_reference} - {self.application_type} for {self.project.name} ({self.status})"
