from django.db import models
from django.conf import settings
from django.utils import timezone
import uuid

def generate_notif_ref():
    return f"NOT-{uuid.uuid4().hex[:4].upper()}"

def generate_deliv_ref():
    return f"EML-{uuid.uuid4().hex[:6].upper()}"


class Notification(models.Model):
    """
    Centralized event-driven notification record across government agency operations.
    """
    CATEGORY_CHOICES = (
        ('CRITICAL', 'Critical Incident / Blocker'),
        ('APPLICATIONS', 'New Applications & Permits'),
        ('INSPECTIONS', 'Inspection Requests & Walkthroughs'),
        ('COMPLIANCE', 'Compliance & Infraction Alerts'),
        ('APPROVALS', 'Approval Queue & Sign-offs'),
        ('EMERGENCY', 'Emergency Dispatch & Response'),
        ('OVERDUE', 'Overdue SLA Actions'),
        ('BIM', 'BIM & 3D Model Review'),
        ('GPR', 'Ground Penetrating Radar & Subsurface'),
        ('DOCUMENTS', 'Document Vault & Revisions'),
        ('MILESTONES', 'Construction Milestone Gates'),
        ('GENERAL', 'General System Notification'),
    )

    PRIORITY_CHOICES = (
        ('Critical', 'Critical Priority'),
        ('High', 'High Priority'),
        ('Medium', 'Medium Priority'),
        ('Low', 'Low Priority'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_notif_ref)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='notifications'
    )
    recipient_role = models.CharField(max_length=100, default='All')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='GENERAL')
    event_type = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    
    title = models.CharField(max_length=255)
    message = models.TextField()
    snippet = models.TextField(blank=True, null=True)
    
    priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Medium')
    severity = models.CharField(max_length=50, default='Normal')
    location = models.CharField(max_length=255, blank=True, null=True)
    
    entity_type = models.CharField(max_length=50, blank=True, null=True)
    entity_id = models.CharField(max_length=100, blank=True, null=True)
    action_url = models.CharField(max_length=255, blank=True, null=True)
    action_required = models.CharField(max_length=255, blank=True, null=True)
    
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    
    is_acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='acknowledged_notifications'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    
    email_sent = models.BooleanField(default=False)
    email_id = models.CharField(max_length=100, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.notification_reference} [{self.category}]: {self.title}"


class EmailDelivery(models.Model):
    """
    Immutable audit ledger of outbound transactional and event emails.
    """
    STATUS_CHOICES = (
        ('QUEUED', 'Queued'),
        ('PROCESSING', 'Processing'),
        ('SENT', 'Sent'),
        ('DELIVERED', 'Delivered'),
        ('FAILED', 'Failed'),
        ('RETRYING', 'Retrying'),
        ('CANCELLED', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_deliv_ref)
    notification = models.ForeignKey(
        Notification, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='email_deliveries'
    )
    recipient_email = models.EmailField(db_index=True)
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='email_deliveries'
    )
    
    template_key = models.CharField(max_length=100, db_index=True)
    subject = models.CharField(max_length=255)
    provider = models.CharField(max_length=50, default='resend')
    provider_message_id = models.CharField(max_length=100, blank=True, null=True)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='QUEUED', db_index=True)
    attempt_count = models.IntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, null=True)
    
    idempotency_key = models.CharField(max_length=255, unique=True, null=True, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.delivery_reference} -> {self.recipient_email} [{self.status}]: {self.subject}"


class NotificationPreference(models.Model):
    """
    User-specific multi-channel notification and email dispatch preferences.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='notification_preferences'
    )
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    
    # Granular Category Toggles
    email_applications = models.BooleanField(default=True)
    email_inspections = models.BooleanField(default=True)
    email_approvals = models.BooleanField(default=True)
    email_compliance = models.BooleanField(default=True)
    email_emergency = models.BooleanField(default=True) # Enforced mandatory for emergency
    email_overdue = models.BooleanField(default=True)
    email_critical = models.BooleanField(default=True)
    email_bim = models.BooleanField(default=True)
    email_gpr = models.BooleanField(default=True)
    email_documents = models.BooleanField(default=True)
    email_milestones = models.BooleanField(default=True)
    
    # Legacy fields
    email_critical_alerts = models.BooleanField(default=True)
    email_daily_digest = models.BooleanField(default=True)
    email_approval_requests = models.BooleanField(default=True)
    email_inspection_updates = models.BooleanField(default=True)
    email_compliance_ncrs = models.BooleanField(default=True)
    sms_emergency_alerts = models.BooleanField(default=True)
    in_app_sound = models.BooleanField(default=True)

    def is_email_allowed_for_category(self, category: str) -> bool:
        if not self.email_enabled:
            # Emergency is mandatory and cannot be disabled
            if category == 'EMERGENCY' or category == 'CRITICAL':
                return True
            return False
            
        cat = category.upper()
        if cat == 'EMERGENCY': return True # Mandatory
        if cat == 'CRITICAL': return self.email_critical
        if cat == 'APPLICATIONS': return self.email_applications
        if cat == 'INSPECTIONS': return self.email_inspections
        if cat == 'APPROVALS': return self.email_approvals
        if cat == 'COMPLIANCE': return self.email_compliance
        if cat == 'OVERDUE': return self.email_overdue
        if cat == 'BIM': return self.email_bim
        if cat == 'GPR': return self.email_gpr
        if cat == 'DOCUMENTS': return self.email_documents
        if cat == 'MILESTONES': return self.email_milestones
        return True

    def __str__(self):
        return f"Preferences for {self.user}"

from apps.projects.models import Project

class WebhookEndpoint(models.Model):
    """
    Registered external webhook endpoints that listen for events (e.g. scan processing completed).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='webhooks', null=True, blank=True)
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=1000)
    secret_key = models.CharField(max_length=255, blank=True, default='', help_text="Used to sign payloads for security.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} - {self.url}"




from apps.projects.models import Project
class WebhookDelivery(models.Model):
    """
    Log of webhook delivery attempts to ensure traceability and aid debugging.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='deliveries')
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    status_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default='')
    success = models.BooleanField(default=False)
    attempted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Delivery to {self.endpoint.name} - {'Success' if self.success else 'Failed'}"


class InAppNotification(models.Model):
    """
    Real-Time QA: In-App notifications for project managers, e.g. 'Stop Work' alerts.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='notifications', null=True, blank=True)
    title = models.CharField(max_length=255)
    message = models.TextField()
    type = models.CharField(max_length=50, default='info', help_text="e.g. info, warning, critical, stop_work")
    is_read = models.BooleanField(default=False)
    related_entity_id = models.UUIDField(null=True, blank=True, help_text="ID of the related Defect, ScanSession, etc.")
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.type.upper()}] {self.title}"
