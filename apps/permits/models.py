from django.db import models
from django.conf import settings
from apps.projects.models import Project
from apps.applications.models import Application
from apps.government.models import Profile
import uuid
import datetime

def generate_permit_number():
    return f"PRM-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

class Permit(models.Model):
    """
    Approved building permits linked to projects and regulatory applications.
    """
    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('REVOKED', 'Revoked'),
        ('EXPIRED', 'Expired'),
        ('RENEWED', 'Renewed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permit_number = models.CharField(max_length=100, unique=True, default=generate_permit_number)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='permits')
    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name='permit')
    
    issued_by = models.ForeignKey(Profile, on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_permits')
    issue_date = models.DateField(default=datetime.date.today)
    expiry_date = models.DateField()
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='ACTIVE')
    conditions = models.TextField(blank=True, null=True, help_text="Conditions for 'Approved Subject To...'")
    qr_verification_code = models.CharField(max_length=255, blank=True, null=True)
    
    # Renewal tracking
    renewal_count = models.PositiveIntegerField(default=0)
    last_renewal_date = models.DateField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.permit_number} - {self.project.name} ({self.status})"
