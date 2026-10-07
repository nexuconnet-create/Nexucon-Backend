from django.db import models
from django.utils import timezone
import uuid
import datetime

def generate_notice_ref():
    return f"NTC-2026-{uuid.uuid4().hex[:4].upper()}"


class ViolationReport(models.Model):
    """
    Citizen reports of suspected building code violations.
    """
    STATUS_CHOICES = (
        ('NEW', 'New / Unverified'),
        ('INVESTIGATING', 'Under Investigation'),
        ('VERIFIED', 'Verified / Action Taken'),
        ('DISMISSED', 'Dismissed / Invalid'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tracking_number = models.CharField(max_length=100, unique=True, blank=True, null=True, db_index=True)
    reporter_name = models.CharField(max_length=150, blank=True, null=True, help_text="Can be anonymous")
    reporter_contact = models.CharField(max_length=150, blank=True, null=True)
    
    address = models.CharField(max_length=255)
    description = models.TextField()
    evidence_url = models.URLField(blank=True, null=True, help_text="Link to uploaded photo/video")
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='NEW')
    reported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-reported_at']

    def save(self, *args, **kwargs):
        if not self.tracking_number:
            self.tracking_number = f"VIO-2026-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Violation Report {self.tracking_number or self.id} at {self.address} - {self.status}"


class PublicNotice(models.Model):
    """
    Official public regulatory advisories, stop-work enforcement notices,
    and stage clearance bulletins published to the Public Transparency Portal.
    """
    NOTICE_TYPES = (
        ('STOP_WORK', 'Stop-Work Order'),
        ('SAFETY_ADVISORY', 'Safety Advisory'),
        ('STAGE_CLEARANCE', 'Stage Clearance'),
        ('REGULATORY_UPDATE', 'Regulatory Update'),
    )
    SEVERITY_LEVELS = (
        ('CRITICAL', 'Critical'),
        ('WARNING', 'Warning'),
        ('INFO', 'Info'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_number = models.CharField(max_length=100, unique=True, default=generate_notice_ref, db_index=True)
    notice_type = models.CharField(max_length=50, choices=NOTICE_TYPES, default='SAFETY_ADVISORY')
    title = models.CharField(max_length=255)
    description = models.TextField()
    issuing_agency = models.CharField(max_length=255, default='Lagos State Building Control Agency (LASBCA)')
    target_lga = models.CharField(max_length=100, default='Statewide')
    target_project_name = models.CharField(max_length=255, blank=True, null=True)
    target_permit_number = models.CharField(max_length=100, blank=True, null=True)
    published_at = models.DateTimeField(default=timezone.now)
    effective_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    severity = models.CharField(max_length=50, choices=SEVERITY_LEVELS, default='INFO')
    download_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-published_at']

    def __str__(self):
        return f"{self.reference_number} - {self.title} ({self.notice_type})"


class PublicPublicationAudit(models.Model):
    """
    Immutable audit ledger for all public projections and publication decisions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity_type = models.CharField(max_length=50)  # 'PROJECT', 'DOCUMENT', 'NOTICE'
    entity_id = models.CharField(max_length=100)
    action = models.CharField(max_length=50, default='PUBLISH')  # PUBLISH, UNPUBLISH, REVISE
    published_by_name = models.CharField(max_length=255, blank=True, default='Directorate of Public Records')
    payload_hash = models.CharField(max_length=64, blank=True, default='')  # SHA-256
    governance_reason = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} {self.entity_type} {self.entity_id} at {self.timestamp}"

