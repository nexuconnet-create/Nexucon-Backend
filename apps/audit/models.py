from django.db import models
from django.conf import settings
from django.core.exceptions import PermissionDenied
import uuid

class AuditEvent(models.Model):
    """
    Append-only audit trail model for recording sensitive actions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    previous_state = models.JSONField(null=True, blank=True)
    new_state = models.JSONField(null=True, blank=True)
    
    class Meta:
        db_table = 'audit_event'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} on {self.resource_type} ({self.resource_id}) at {self.timestamp}"

    def save(self, *args, **kwargs):
        if self.pk:
            # Check if this object already exists in the database
            if AuditEvent.objects.filter(pk=self.pk).exists():
                raise PermissionDenied("Audit records are append-only. Modification is strictly forbidden.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit records are append-only. Deletion is strictly forbidden.")
