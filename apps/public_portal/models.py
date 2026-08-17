from django.db import models
import uuid

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
    reporter_name = models.CharField(max_length=150, blank=True, null=True, help_text="Can be anonymous")
    reporter_contact = models.CharField(max_length=150, blank=True, null=True)
    
    address = models.CharField(max_length=255)
    description = models.TextField()
    evidence_url = models.URLField(blank=True, null=True, help_text="Link to uploaded photo/video")
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='NEW')
    reported_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Violation Report at {self.address} - {self.status}"
