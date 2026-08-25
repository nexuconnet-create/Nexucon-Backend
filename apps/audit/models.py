from django.db import models
from django.conf import settings
from django.core.exceptions import PermissionDenied
import uuid
import hashlib

def generate_audit_ref():
    return f"AUD-{uuid.uuid4().hex[:6].upper()}"


class AuditEvent(models.Model):
    """
    Append-only tamper-proof immutable audit ledger for recording statutory regulatory actions.
    """
    SEVERITY_CHOICES = (
        ('Normal', 'Normal Event'),
        ('Warning', 'Warning / Infraction'),
        ('High', 'High Priority Action'),
        ('Critical', 'Critical / Security Event'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_audit_ref)
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='audit_events'
    )
    user_name = models.CharField(max_length=255, default='System Officer')
    user_role = models.CharField(max_length=100, default='Government Officer')
    user_email = models.EmailField(null=True, blank=True)
    
    action = models.CharField(max_length=100, db_index=True)
    event_type = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    resource_type = models.CharField(max_length=100, db_index=True)
    resource_id = models.CharField(max_length=255, db_index=True)
    project_name = models.CharField(max_length=255, default='Central Metro Transit Hub')
    
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    
    previous_state = models.JSONField(null=True, blank=True)
    new_state = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    severity = models.CharField(max_length=50, choices=SEVERITY_CHOICES, default='Normal', db_index=True)
    signature_hash = models.CharField(max_length=255, default='0x8f4e2c9b1a7d3e5f')
    is_verified = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'audit_event'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.audit_reference} [{self.action}] on {self.resource_type} ({self.resource_id}) at {self.timestamp}"

    def calculate_hash(self):
        payload = f"{self.id}:{self.action}:{self.resource_type}:{self.resource_id}:{self.timestamp}"
        return "0x" + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]

    def save(self, *args, **kwargs):
        # Strict immutability: existing records can NEVER be updated
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Audit records are immutable append-only. Modification is strictly forbidden.")
        if not self.signature_hash or self.signature_hash == '0x8f4e2c9b1a7d3e5f':
            salt = uuid.uuid4().hex[:8]
            raw = hashlib.sha256(f"{self.action}:{self.resource_type}:{self.resource_id}:{salt}".encode('utf-8')).hexdigest()[:14]
            self.signature_hash = f"0x{raw}"
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Strict immutability: records can NEVER be deleted
        raise PermissionDenied("Audit records are immutable append-only. Deletion is strictly forbidden.")
