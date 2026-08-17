from django.db import models
from django.conf import settings
from apps.projects.models import Project
from apps.government.models import Profile
import uuid

class Document(models.Model):
    """
    Metadata for uploaded documents (Architectural drawings, structural reports, etc).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    title = models.CharField(max_length=255)
    document_type = models.CharField(max_length=100)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.document_type})"


from .validators import SecureFileValidator

class Version(models.Model):
    """
    Version control for documents.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField(default=1)
    file = models.FileField(upload_to='documents/%Y/%m/', validators=[SecureFileValidator()], null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.document.title} - v{self.version_number}"


class Approval(models.Model):
    """
    Digital signatures/approvals for specific document versions.
    """
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(Version, on_delete=models.CASCADE, related_name='approvals')
    reviewer = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name='document_reviews')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    comments = models.TextField(blank=True, null=True)
    reviewed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Review by {self.reviewer.user.email} - {self.status}"
