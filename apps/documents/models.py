from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
from apps.government.models import Profile
import uuid
import datetime

def generate_doc_ref():
    return f"DOC-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_app_ref():
    return f"APP-DOC-{datetime.datetime.now().year}-{uuid.uuid4().hex[:4].upper()}"


class DocumentFolder(models.Model):
    """
    Project document organization folders (e.g. 01_Architectural, 02_Structural).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='document_folders', null=True, blank=True)
    files_count = models.IntegerField(default=0)
    total_size = models.CharField(max_length=50, default='0 MB')
    is_shared = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Document(models.Model):
    """
    Metadata for project and regulatory documents (drawings, reports, permits, contracts).
    """
    DISCIPLINE_CHOICES = (
        ('Architecture', 'Architecture'),
        ('Structural', 'Structural'),
        ('MEP', 'MEP (Mechanical/Electrical/Plumbing)'),
        ('Planning', 'Planning & Urban Design'),
        ('Legal', 'Contracts & Legal'),
        ('Environmental', 'Environmental & Geotechnical'),
        ('General', 'General / Administrative'),
    )

    DOCUMENT_TYPES = (
        ('DRAWING', '2D Architectural / Structural Drawing'),
        ('CONTRACT', 'Contract & Legal Agreement'),
        ('INSPECTION_REPORT', 'Site Inspection Report'),
        ('COMPLIANCE_CERTIFICATE', 'Compliance Certificate'),
        ('SITE_PHOTO', 'Site Photograph'),
        ('REPORT', 'Technical Report'),
        ('PERMIT_ATTACHMENT', 'Permit Submission Document'),
    )

    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('PENDING_REVIEW', 'Pending Review'),
        ('UNDER_REVIEW', 'Under Review'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
        ('EXPIRING_SOON', 'Expiring Soon'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_doc_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='documents')
    folder = models.CharField(max_length=100, default='01_Architectural')
    
    title = models.CharField(max_length=255)
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPES, default='DRAWING')
    discipline = models.CharField(max_length=50, choices=DISCIPLINE_CHOICES, default='Architecture')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='APPROVED')
    
    current_version = models.CharField(max_length=50, default='v1.0')
    file_url = models.CharField(max_length=500, blank=True, null=True, default='https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf')
    file_size = models.CharField(max_length=50, default='12.4 MB')
    file_format = models.CharField(max_length=20, default='PDF')
    pages_count = models.IntegerField(default=12)
    
    is_starred = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=True)
    expiry_date = models.DateField(null=True, blank=True)
    
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_documents')
    uploader_name = models.CharField(max_length=255, default='S. Jenkins')
    
    # Official Government Digital Stamping & Verification Vault
    is_digitally_stamped = models.BooleanField(default=False)
    stamped_by_name = models.CharField(max_length=255, blank=True, null=True)
    stamped_at = models.DateTimeField(null=True, blank=True)
    stamp_reference = models.CharField(max_length=255, blank=True, null=True)
    signature_hash = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.document_reference}) - {self.status}"


class Version(models.Model):
    """
    Version control for documents with commit changes and author records.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField(default=1)
    version_label = models.CharField(max_length=50, default='v1.0')
    changes_summary = models.TextField(blank=True, null=True)
    
    author_name = models.CharField(max_length=255, default='Lead Architect')
    author_role = models.CharField(max_length=255, default='Review Team')
    
    file_url = models.CharField(max_length=500, blank=True, null=True)
    file_size = models.CharField(max_length=50, default='12.4 MB')
    status = models.CharField(max_length=50, default='Current')
    
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.document.title} - {self.version_label}"


class Approval(models.Model):
    """
    Official digital signatures & stamps archive for approved project documents.
    """
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_app_ref)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='approval_records', null=True, blank=True)
    version = models.ForeignKey(Version, on_delete=models.SET_NULL, null=True, blank=True, related_name='approvals')
    
    category = models.CharField(max_length=100, default='Project Planning')
    approved_by_name = models.CharField(max_length=255, default='Gov. Planning Comm.')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='APPROVED')
    
    comments = models.TextField(blank=True, null=True)
    signature_hash = models.CharField(max_length=255, blank=True, null=True)
    reviewed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-reviewed_at']

    def __str__(self):
        return f"{self.approval_reference} - {self.document.title} ({self.status})"


class DocumentTemplate(models.Model):
    """
    Standard regulatory document templates (checklists, violation notices, permits).
    """
    CATEGORY_CHOICES = (
        ('PERMIT', 'Permits & Approvals'),
        ('INSPECTION', 'Site Inspections'),
        ('ENFORCEMENT', 'Enforcement & Stop-Work'),
        ('COMPLIANCE', 'Environmental & Compliance'),
        ('LEGAL', 'Contracts & Legal'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='INSPECTION')
    description = models.TextField()
    
    file_format = models.CharField(max_length=20, default='PDF')
    file_url = models.CharField(max_length=500, blank=True, null=True, default='https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/template.pdf')
    file_size = models.CharField(max_length=50, default='450 KB')
    usage_count = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.category})"
