from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
import uuid
import datetime

def generate_mdl_ref():
    return f"MDL-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_cls_ref():
    return f"CLS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_ann_ref():
    return f"ANN-{uuid.uuid4().hex[:4].upper()}"


class BIMModel(models.Model):
    """
    Building Information Models across Architecture, Structural, MEP, and Civil disciplines.
    """
    DISCIPLINE_CHOICES = (
        ('Architecture', 'Architecture'),
        ('MEP', 'Mechanical / Electrical / Plumbing (MEP)'),
        ('Structural', 'Structural Engineering'),
        ('Multi-Disciplinary', 'Multi-Disciplinary / Federated'),
        ('Civil/Infrastructure', 'Civil / Infrastructure'),
        ('Landscape', 'Landscape & Site Planning'),
    )

    STATUS_CHOICES = (
        ('Active', 'Active'),
        ('Under Review', 'Under Review'),
        ('Approved', 'Approved & Certified'),
        ('Changes Requested', 'Changes Requested'),
        ('Archived', 'Archived'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_mdl_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bim_models')
    name = models.CharField(max_length=255)
    discipline = models.CharField(max_length=50, choices=DISCIPLINE_CHOICES, default='Architecture')
    format = models.CharField(max_length=50, default='IFC4')
    
    file_url = models.CharField(max_length=500, blank=True, null=True, default='https://assets.nexucon.com/bim/sample_model.ifc')
    file_size = models.CharField(max_length=50, default='345 MB')
    current_version = models.CharField(max_length=50, default='v1.0')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Active')
    
    # Digital Certification & Cryptographic Seal
    is_digitally_certified = models.BooleanField(default=False)
    certified_by_name = models.CharField(max_length=255, blank=True, null=True)
    certified_at = models.DateTimeField(null=True, blank=True)
    hash_signature = models.CharField(max_length=255, blank=True, null=True)
    
    lod = models.CharField(max_length=50, default='LOD 300')
    element_count = models.IntegerField(default=12450)
    coordinate_system = models.JSONField(default=dict, blank=True, help_text="Georeferencing CRS and origin coordinates")
    
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_bim_models')
    uploaded_by_name = models.CharField(max_length=255, default='BIM Coordinator')
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.discipline} {self.current_version}) - {self.project.name}"


class BIMModelVersion(models.Model):
    """
    Historical revision log tracking commits, changes, and element delta stats.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model = models.ForeignKey(BIMModel, on_delete=models.CASCADE, related_name='versions')
    version_label = models.CharField(max_length=50, default='v1.0')
    commit_hash = models.CharField(max_length=50, default='a8f93bc2')
    changes_summary = models.TextField(blank=True, null=True)
    
    author_name = models.CharField(max_length=255, default='Lead Architect')
    author_role = models.CharField(max_length=255, default='BIM Coordinator')
    
    stats_added = models.IntegerField(default=0)
    stats_modified = models.IntegerField(default=0)
    stats_removed = models.IntegerField(default=0)
    
    file_url = models.CharField(max_length=500, blank=True, null=True)
    file_size = models.CharField(max_length=50, default='345 MB')
    is_current = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.model.name} {self.version_label} ({self.commit_hash})"


class BIMClash(models.Model):
    """
    Multi-disciplinary interference detected between models (e.g. MEP Duct vs Structural Beam).
    """
    CLASH_TYPES = (
        ('HARD_CLASH', 'Hard Clash / Physical Intersection'),
        ('SOFT_CLASH', 'Soft Clash / Clearance Violation'),
        ('CLEARANCE', 'Egress & Accessibility Clearance'),
        ('DUPLICATE', 'Duplicate Element'),
    )

    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    )

    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('ASSIGNED', 'Assigned'),
        ('IN_REVIEW', 'In Review'),
        ('RESOLVED', 'Resolved'),
        ('CONVERTED_TO_ISSUE', 'Converted to Site Issue'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clash_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_cls_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bim_clashes')
    primary_model = models.ForeignKey(BIMModel, on_delete=models.CASCADE, related_name='primary_clashes')
    secondary_model = models.ForeignKey(BIMModel, on_delete=models.SET_NULL, null=True, blank=True, related_name='secondary_clashes')
    
    clash_type = models.CharField(max_length=50, choices=CLASH_TYPES, default='HARD_CLASH')
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='HIGH')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='OPEN')
    
    assigned_to_name = models.CharField(max_length=255, default='MEP Engineer')
    assigned_discipline = models.CharField(max_length=100, default='MEP')
    coordinates_3d = models.JSONField(default=dict, blank=True, help_text="3D spatial coordinates {x, y, z}")
    
    converted_site_issue = models.ForeignKey('monitoring.SiteIssue', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_bim_clashes')
    resolution_notes = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.clash_reference} - {self.title} ({self.project.name})"


class BIMAnnotation(models.Model):
    """
    BCF-compliant design review markups, viewpoints, and discussion threads.
    """
    STATUS_CHOICES = (
        ('Open', 'Open'),
        ('In Progress', 'In Progress'),
        ('Resolved', 'Resolved'),
        ('Closed', 'Closed'),
    )

    PRIORITY_CHOICES = (
        ('Low', 'Low'),
        ('Medium', 'Medium'),
        ('High', 'High'),
        ('Critical', 'Critical'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    annotation_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_ann_ref)
    model = models.ForeignKey(BIMModel, on_delete=models.CASCADE, related_name='annotations')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bim_annotations')
    
    author_name = models.CharField(max_length=255, default='Sarah Jenkins')
    author_role = models.CharField(max_length=255, default='Lead Architect')
    text = models.TextField()
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Open')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='Medium')
    
    viewpoint_camera = models.JSONField(default=dict, blank=True)
    element_ids = models.JSONField(default=list, blank=True)
    comments_count = models.IntegerField(default=0)
    attachments = models.JSONField(default=list, blank=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.annotation_reference} - {self.author_name}: {self.text[:30]}"


class BIMProgressValidation(models.Model):
    """
    4D BIM schedule validation comparing as-built LiDAR/drone surveys against as-planned models.
    """
    SCHEDULE_STATUS = (
        ('ON_TRACK', 'On Track'),
        ('DELAYED', 'Delayed'),
        ('AHEAD', 'Ahead of Schedule'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bim_progress_validations')
    model = models.ForeignKey(BIMModel, on_delete=models.SET_NULL, null=True, blank=True, related_name='progress_validations')
    
    schedule_status = models.CharField(max_length=50, choices=SCHEDULE_STATUS, default='ON_TRACK')
    days_variance = models.IntegerField(default=0, help_text="Negative for days behind, positive for days ahead")
    
    completed_elements_count = models.IntegerField(default=4205)
    total_elements_count = models.IntegerField(default=9500)
    earned_value_usd = models.CharField(max_length=50, default='$2.4M')
    
    planned_vs_actual = models.JSONField(default=list, blank=True)
    simulation_date = models.DateField(default=timezone.now)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"4D Validation: {self.project.name} ({self.schedule_status})"
