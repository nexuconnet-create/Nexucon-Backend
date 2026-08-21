from django.db import models
from django.conf import settings
import uuid
import datetime

def generate_project_ref():
    return f"NXC-GOV-{datetime.datetime.now().year}-{uuid.uuid4().hex[:4].upper()}"

class Project(models.Model):
    """
    Core project model for construction sites.
    """
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('PLANNING', 'Planning / Proposed'),
        ('APPROVED', 'Approved'),
        ('ACTIVE', 'Under Construction'),
        ('SUSPENDED', 'Suspended / Stop-Work'),
        ('COMPLETED', 'Completed'),
        ('ABANDONED', 'Abandoned'),
    )

    PROJECT_TYPE_CHOICES = (
        ('Residential', 'Residential'),
        ('Commercial', 'Commercial'),
        ('Industrial', 'Industrial'),
        ('Infrastructure', 'Infrastructure'),
        ('Mixed-Use', 'Mixed-Use'),
        ('Institutional', 'Institutional'),
        ('Renovation', 'Renovation / Redevelopment'),
        ('Other', 'Other'),
    )

    PRIORITY_CHOICES = (
        ('Low', 'Low'),
        ('Normal', 'Normal'),
        ('High', 'High'),
        ('Critical', 'Critical'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # 1. Project Information
    name = models.CharField(max_length=255)
    reference_number = models.CharField(max_length=50, unique=True, default=generate_project_ref)
    project_type = models.CharField(max_length=50, choices=PROJECT_TYPE_CHOICES, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    development_category = models.CharField(max_length=100, blank=True, null=True)
    estimated_project_value = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    number_of_floors = models.IntegerField(blank=True, null=True)
    start_date = models.DateField(null=True, blank=True)
    estimated_completion = models.DateField(null=True, blank=True)

    # 2. Developer / Project Owner
    developer_name = models.CharField(max_length=255, blank=True, null=True)
    developer_organization = models.CharField(max_length=255, blank=True, null=True)
    developer_reg_number = models.CharField(max_length=100, blank=True, null=True)
    developer_email = models.EmailField(blank=True, null=True)
    developer_phone = models.CharField(max_length=50, blank=True, null=True)
    developer_address = models.TextField(blank=True, null=True)
    developer_contact_person = models.CharField(max_length=255, blank=True, null=True)

    # 3. Project Location
    site_address = models.TextField(blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    lga = models.CharField(max_length=100, help_text="Local Government Area", blank=True, null=True)
    ward_area = models.CharField(max_length=100, blank=True, null=True)
    plot_number = models.CharField(max_length=100, blank=True, null=True)
    block_number = models.CharField(max_length=100, blank=True, null=True)
    land_title_reference = models.CharField(max_length=255, blank=True, null=True)
    # GeoDjango fields
    try:
        from django.contrib.gis.db import models as gis_models
        location = gis_models.PointField(null=True, blank=True, help_text="GNSS Point location")
        boundary = gis_models.PolygonField(null=True, blank=True, help_text="Site boundaries")
    except Exception:
        location = models.JSONField(null=True, blank=True, help_text="GNSS Point location")
        boundary = models.JSONField(null=True, blank=True, help_text="Site boundaries")

    # 5. Regulatory Information
    permit_number = models.CharField(max_length=100, blank=True, null=True)
    permit_status = models.CharField(max_length=100, blank=True, null=True)
    planning_approval_reference = models.CharField(max_length=100, blank=True, null=True)
    building_control_reference = models.CharField(max_length=100, blank=True, null=True)
    environmental_approval_reference = models.CharField(max_length=100, blank=True, null=True)
    existing_applications = models.TextField(blank=True, null=True)
    applicable_regulations = models.TextField(blank=True, null=True)
    regulatory_authority = models.CharField(max_length=255, blank=True, null=True)
    approval_date = models.DateField(null=True, blank=True)
    permit_expiry_date = models.DateField(null=True, blank=True)

    # 6. Project Scope & Development Details
    primary_use = models.CharField(max_length=255, blank=True, null=True)
    proposed_use = models.CharField(max_length=255, blank=True, null=True)
    site_area = models.DecimalField(max_digits=10, decimal_places=2, help_text="in sqm", blank=True, null=True)
    gross_floor_area = models.DecimalField(max_digits=10, decimal_places=2, help_text="in sqm", blank=True, null=True)
    building_height = models.DecimalField(max_digits=10, decimal_places=2, help_text="in meters", blank=True, null=True)
    number_of_units = models.IntegerField(blank=True, null=True)
    construction_method = models.CharField(max_length=255, blank=True, null=True)
    structural_system = models.CharField(max_length=255, blank=True, null=True)
    special_requirements = models.TextField(blank=True, null=True)

    # 8. Government Assignment
    assigned_department = models.CharField(max_length=255, blank=True, null=True)
    assigned_officer = models.CharField(max_length=255, blank=True, null=True)
    assigned_inspector = models.CharField(max_length=255, blank=True, null=True)
    technical_reviewer = models.CharField(max_length=255, blank=True, null=True)
    compliance_officer = models.CharField(max_length=255, blank=True, null=True)
    project_priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Normal')
    monitoring_category = models.CharField(max_length=255, blank=True, null=True)
    inspection_frequency = models.CharField(max_length=100, blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)

    # 9. Monitoring Configuration
    enable_site_monitoring = models.BooleanField(default=False)
    enable_gnss = models.BooleanField(default=False)
    enable_bim = models.BooleanField(default=False)
    inspection_required = models.BooleanField(default=True)
    compliance_monitoring_required = models.BooleanField(default=True)
    progress_reporting_required = models.BooleanField(default=False)
    site_verification_required = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.reference_number})"


class ProjectProfessional(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='professionals')
    name = models.CharField(max_length=255)
    organization = models.CharField(max_length=255, blank=True, null=True)
    license_number = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    role = models.CharField(max_length=100) # e.g. Architect, Civil Engineer

    def __str__(self):
        return f"{self.name} - {self.role} ({self.project.name})"


class ProjectDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='project_documents')
    document_type = models.CharField(max_length=100) # e.g. Architectural Drawing, Title Deed
    file = models.FileField(upload_to='project_documents/', blank=True, null=True)
    name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class ProjectMilestone(models.Model):
    """
    Construction Milestones linked to the project schedule.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    title = models.CharField(max_length=200)
    target_date = models.DateField()
    is_completed = models.BooleanField(default=False)
    completion_date = models.DateField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.project.name} - {self.title}"
