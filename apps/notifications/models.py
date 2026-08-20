from django.db import models
from django.conf import settings
from django.utils import timezone
import uuid

def generate_notif_ref():
    return f"NOT-{uuid.uuid4().hex[:4].upper()}"


class Notification(models.Model):
    """
    Real-time notification record across government agency operations.
    """
    CATEGORY_CHOICES = (
        ('CRITICAL', 'Critical Incident / Blocker'),
        ('APPLICATIONS', 'New Applications & Permits'),
        ('INSPECTIONS', 'Inspection Requests & Walkthroughs'),
        ('COMPLIANCE', 'Compliance & Infraction Alerts'),
        ('APPROVALS', 'Approval Queue & Sign-offs'),
        ('OVERDUE', 'Overdue SLA Actions'),
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
    
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.notification_reference} [{self.category}]: {self.title}"


class NotificationPreference(models.Model):
    """
    User-specific notification dispatch preferences.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='notification_preferences'
    )
    email_critical_alerts = models.BooleanField(default=True)
    email_daily_digest = models.BooleanField(default=True)
    email_approval_requests = models.BooleanField(default=True)
    email_inspection_updates = models.BooleanField(default=True)
    email_compliance_ncrs = models.BooleanField(default=True)
    sms_emergency_alerts = models.BooleanField(default=True)
    in_app_sound = models.BooleanField(default=True)

    def __str__(self):
        return f"Preferences for {self.user}"
