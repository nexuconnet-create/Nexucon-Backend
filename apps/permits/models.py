from django.db import models
from django.conf import settings
from apps.projects.models import Project
from apps.applications.models import Application
from apps.government.models import Profile
import uuid

class Permit(models.Model):
    """
    Approved building permits linked to projects.
    """
    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('REVOKED', 'Revoked'),
        ('EXPIRED', 'Expired'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    permit_number = models.CharField(max_length=100, unique=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='permits')
    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name='permit')
    
    issued_by = models.ForeignKey(Profile, on_delete=models.SET_NULL, null=True, related_name='issued_permits')
    issue_date = models.DateField()
    expiry_date = models.DateField()
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='ACTIVE')
    conditions = models.TextField(blank=True, null=True, help_text="Conditions for 'Approved Subject To...'")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.permit_number} - {self.project.name}"
