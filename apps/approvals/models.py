from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
import uuid

def generate_request_ref():
    return f"REQ-{uuid.uuid4().hex[:4].upper()}"

def generate_decision_ref():
    return f"LOG-{uuid.uuid4().hex[:4].upper()}"


class ApprovalRequest(models.Model):
    """
    Core centralized approval instance for Permits, Documents, Technical Reviews,
    BIM Versions, GPR findings, Inspections, and Milestones.
    """
    TYPE_CHOICES = (
        ('Document', 'Document Signature'),
        ('Technical', 'Technical & Engineering Review'),
        ('Permit', 'Permit & License Authorization'),
        ('Escalated', 'Escalated Blocker / High Value (>₦50M)'),
        ('General', 'General Administrative Request'),
    )

    DISCIPLINE_CHOICES = (
        ('MEP', 'Mechanical, Electrical & Plumbing (MEP)'),
        ('Structural', 'Structural Engineering'),
        ('Architecture', 'Architectural Design'),
        ('Legal', 'Legal & Regulatory'),
        ('Finance', 'Finance & Budget'),
        ('Safety', 'Safety & HSE'),
        ('Procurement', 'Procurement & Contracts'),
        ('General', 'General Project Management'),
    )

    PRIORITY_CHOICES = (
        ('Critical', 'Critical Priority'),
        ('High', 'High Priority'),
        ('Medium', 'Medium Priority'),
        ('Low', 'Low Priority'),
    )

    STATUS_CHOICES = (
        ('Pending', 'Pending Decision'),
        ('Approved', 'Fully Approved / Executed'),
        ('Rejected', 'Rejected'),
        ('Conditional', 'Subject To Conditions'),
        ('In Review', 'In Technical Review'),
        ('Awaiting Fix', 'Awaiting Fix / Remediation'),
        ('Escalated', 'Escalated to Director / DG'),
    )

    ENTITY_TYPE_CHOICES = (
        ('PermitApplication', 'Permit Application'),
        ('Document', 'Project Document / Version'),
        ('BIMModel', 'BIM Coordination Model'),
        ('Inspection', 'Site Inspection Report'),
        ('Milestone', 'Construction Milestone'),
        ('ComplianceReview', 'Compliance Review / Audit'),
        ('GPRFinding', 'GPR Subsurface Finding'),
        ('General', 'General Entity'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_request_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='approval_requests')
    
    title = models.CharField(max_length=255)
    request_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default='General')
    discipline = models.CharField(max_length=50, choices=DISCIPLINE_CHOICES, default='General')
    priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Medium')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    
    # Financial delegation of authority (DoA): >₦50M requires PS/Director-General
    value_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.0)
    doa_level_required = models.CharField(max_length=100, default='Director')
    
    submitted_by_name = models.CharField(max_length=255, default='Apex Engineering')
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='submitted_approvals')
    
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_approvals')
    assigned_to_name = models.CharField(max_length=255, blank=True, null=True, default='Lead Government Reviewer')
    
    due_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True, null=True)
    
    # Source entity linking and cryptographic version lineage
    source_entity_type = models.CharField(max_length=50, choices=ENTITY_TYPE_CHOICES, default='General')
    source_entity_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    source_version_hash = models.CharField(max_length=255, blank=True, null=True)
    
    # Compliance gating & condition tracking
    compliance_gate_status = models.CharField(max_length=50, default='Passed')
    conditions_met = models.BooleanField(default=False)
    
    # Escalation / Blocker properties
    bottleneck = models.CharField(max_length=255, blank=True, null=True)
    days_overdue = models.IntegerField(default=0)
    
    # Multi-signature properties
    signatories_required = models.IntegerField(default=1)
    signatories_completed = models.IntegerField(default=0)
    attached_file_url = models.CharField(max_length=500, blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.request_reference}: {self.title} ({self.status})"

    def calculate_doa(self):
        """Evaluate Delegation of Authority threshold based on financial value."""
        if self.value_amount > 50000000:
            return 'Permanent Secretary / Director General'
        return 'Director'

    def save(self, *args, **kwargs):
        self.doa_level_required = self.calculate_doa()
        super().save(*args, **kwargs)


class ApprovalDecision(models.Model):
    """
    Immutable audit record of an executive decision with cryptographic seal and role verification.
    """
    OUTCOME_CHOICES = (
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected / Denied'),
        ('Conditional', 'Conditional Approval'),
        ('Returned For Info', 'Returned For Information'),
        ('Escalated', 'Escalated to Executive Authority'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_decision_ref)
    approval_request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name='decisions')
    
    decider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approval_decisions')
    decider_name = models.CharField(max_length=255, default='Director')
    decider_role = models.CharField(max_length=255, default='Director')
    
    outcome = models.CharField(max_length=50, choices=OUTCOME_CHOICES)
    decision_notes = models.TextField(blank=True, null=True)
    conditions = models.TextField(blank=True, null=True)
    
    digital_pin_verified = models.BooleanField(default=False)
    signature_hash = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.decision_reference}: {self.outcome} by {self.decider_name}"


class TechnicalReviewCriteria(models.Model):
    """
    Individual evaluation criterion for engineering rubrics (MEP, Structural, Architecture).
    """
    STATUS_CHOICES = (
        ('pass', 'Passed'),
        ('fail', 'Failed'),
        ('pending', 'Pending Evaluation'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name='criteria')
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True, null=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.approval_request.request_reference} - {self.name}: {self.status}"


class ApprovalComment(models.Model):
    """
    Review comments, revision requests, condition verifications, and technical findings.
    """
    COMMENT_TYPE_CHOICES = (
        ('General', 'General Review Comment'),
        ('RevisionRequest', 'Formal Revision Request'),
        ('ConditionVerification', 'Condition Verification'),
        ('TechnicalFinding', 'Technical / Site Finding'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approval_comments')
    author_name = models.CharField(max_length=255, default='Reviewing Official')
    comment_type = models.CharField(max_length=50, choices=COMMENT_TYPE_CHOICES, default='General')
    content = models.TextField()
    attachment_url = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.approval_request.request_reference} - {self.comment_type} by {self.author_name}"
