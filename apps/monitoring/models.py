from django.db import models
from django.conf import settings
from django.utils import timezone
from apps.projects.models import Project
import uuid
import datetime

def generate_upd_ref():
    return f"UPD-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_obs_ref():
    return f"OBS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_iss_ref():
    return f"ISS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"

def generate_vrf_ref():
    return f"VRF-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"


class DailySiteUpdate(models.Model):
    """
    Frequent asynchronous updates, daily photos, drone surveys, and progress reports.
    """
    TYPE_CHOICES = (
        ('DAILY_PHOTO', 'Daily Photo Update'),
        ('DRONE_SURVEY', 'Comprehensive Drone Survey'),
        ('PROGRESS_REPORT', 'Progress Update'),
        ('SITE_LOG', 'General Site Log'),
    )

    STATUS_CHOICES = (
        ('Active', 'Active'),
        ('Pending Verification', 'Pending Verification'),
        ('Approved', 'Approved'),
        ('Flagged', 'Flagged'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    update_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_upd_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='site_updates')
    update_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default='DAILY_PHOTO')
    
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reported_site_updates')
    reported_by_name = models.CharField(max_length=255, default='Site Supervisor')
    
    progress_percentage = models.IntegerField(default=0, help_text="Estimated progress 0-100%")
    work_summary = models.TextField()
    
    photos = models.JSONField(default=list, blank=True, help_text="List of photo URLs and metadata")
    drone_survey_data = models.JSONField(default=dict, blank=True, help_text="Flight stats, point cloud / orthomosaic links")
    
    weather_condition = models.CharField(max_length=100, default='Clear / Sunny')
    workforce_count = models.IntegerField(default=0)
    gps_coordinates = models.JSONField(default=dict, blank=True)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Active')
    priority = models.CharField(max_length=20, default='Medium')
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.update_reference} - {self.update_type} ({self.project.name})"


class FieldObservation(models.Model):
    """
    Centralized record of observations captured during site visits and audits.
    """
    CATEGORY_CHOICES = (
        ('QUALITY', 'Quality & Workmanship'),
        ('SAFETY', 'Safety & Scaffolding'),
        ('PROGRESS', 'Progress & Pacing'),
        ('ENVIRONMENTAL', 'Environmental & Drainage'),
        ('GENERAL', 'General Site Condition'),
    )

    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    )

    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('UNDER_REVIEW', 'Under Review'),
        ('ACTION_REQUIRED', 'Action Required'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_obs_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='field_observations')
    
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='QUALITY')
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='LOW')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='OPEN')
    
    assigned_officer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_observations')
    assigned_officer_name = models.CharField(max_length=255, blank=True, null=True)
    observed_by_name = models.CharField(max_length=255, default='Government Field Inspector')
    
    gps_coordinates = models.JSONField(default=dict, blank=True)
    evidence_photos = models.JSONField(default=list, blank=True)
    
    corrective_action = models.TextField(blank=True, null=True)
    resolution_notes = models.TextField(blank=True, null=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.observation_reference} - {self.title} ({self.project.name})"


class SiteIssue(models.Model):
    """
    Site defects, regulatory concerns, and deviations requiring resolution.
    """
    SEVERITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    )

    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In Progress'),
        ('UNDER_REVIEW', 'Under Review'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_iss_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='site_issues')
    
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='MEDIUM')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='OPEN')
    
    assigned_to_name = models.CharField(max_length=255, default='Site Engineer')
    reported_by_name = models.CharField(max_length=255, default='Monitoring Officer')
    due_date = models.DateField(null=True, blank=True)
    
    resolution_evidence = models.JSONField(default=list, blank=True)
    resolution_notes = models.TextField(blank=True, null=True)
    is_escalated = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.issue_reference} - {self.title} ({self.project.name})"


class ConstructionMilestone(models.Model):
    """
    Key construction programme milestones tracked against approved schedules.
    """
    STATUS_CHOICES = (
        ('UPCOMING', 'Upcoming'),
        ('DUE_THIS_WEEK', 'Due This Week'),
        ('VERIFIED', 'Verified'),
        ('DELAYED', 'Delayed'),
        ('COMPLETED', 'Completed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='construction_milestones')
    name = models.CharField(max_length=255)
    
    target_date = models.DateField()
    actual_completion_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='UPCOMING')
    progress_percentage = models.IntegerField(default=0)
    
    verified_by_name = models.CharField(max_length=255, blank=True, null=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    evidence_documents = models.JSONField(default=list, blank=True)
    
    is_delayed = models.BooleanField(default=False)
    delay_reason = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['target_date']

    def __str__(self):
        return f"{self.name} - {self.project.name} ({self.status})"


class SiteVerification(models.Model):
    """
    Physical site verification, coordinate boundary checking, and GNSS variance tracking.
    """
    METHOD_CHOICES = (
        ('GNSS_RTK_SURVEY', 'Tersus Oscar GNSS RTK Rover'),
        ('TERSU_ROVER', 'Tersus GNSS Rover Calibration'),
        ('GPR_SCAN', 'Ground Penetrating Radar (GPR) Scan'),
        ('DRONE_PHOTOGRAMMETRY', 'Aerial Drone Photogrammetry'),
        ('TOTAL_STATION', 'Total Station Coordinate Survey'),
    )

    STATUS_CHOICES = (
        ('PENDING_VERIFICATION', 'Pending Verification'),
        ('VERIFIED', 'Verified'),
        ('VARIANCE_DETECTED', 'Variance Detected'),
        ('FLAGGED', 'Flagged for Review'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_vrf_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='verifications')
    
    method = models.CharField(max_length=50, choices=METHOD_CHOICES, default='GNSS_RTK_SURVEY')
    device_identifier = models.CharField(max_length=255, default='Tersus Oscar GNSS RTK #042')
    
    boundary_coordinates = models.JSONField(default=list, blank=True, help_text="Approved site perimeter boundary points")
    captured_coordinates = models.JSONField(default=dict, blank=True, help_text="Field measured coordinates {lat, lng, elevation}")
    approved_coordinates = models.JSONField(default=dict, blank=True, help_text="Approved CAD/GIS coordinates {lat, lng, elevation}")
    
    variance_meters = models.FloatField(default=0.0, help_text="Calculated deviation in meters")
    variance_detected = models.BooleanField(default=False)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING_VERIFICATION')
    
    verified_by_name = models.CharField(max_length=255, blank=True, null=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.verification_reference} - {self.method} ({self.project.name})"
