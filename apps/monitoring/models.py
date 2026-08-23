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

def generate_ms_code():
    return f"MS-{uuid.uuid4().hex[:4].upper()}"


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
    Key construction programme milestones tracked against approved schedules,
    statutory inspection gates, BIM/GNSS tolerance checks, and audit trails.
    """
    STATUS_CHOICES = (
        ('PLANNED', 'Planned'),
        ('IN_PROGRESS', 'In Progress'),
        ('DUE_THIS_WEEK', 'Due This Week'),
        ('PENDING_VERIFICATION', 'Pending Verification'),
        ('VERIFIED', 'Verified & Certified'),
        ('COMPLETED', 'Completed'),
        ('DELAYED', 'Delayed'),
        ('BLOCKED', 'Blocked / Non-Compliant'),
        ('ON_HOLD', 'On Hold'),
        ('UPCOMING', 'Upcoming'),
    )

    PHASE_CHOICES = (
        ('SUBSTRUCTURE', 'Substructure & Foundation Piling'),
        ('STRUCTURAL_FRAME', 'Reinforced Concrete Superstructure Frame'),
        ('SUPERSTRUCTURE', 'Superstructure & Floor Slabs'),
        ('MEP_ROUGHIN', 'MEP Services & Conduit Rough-ins'),
        ('FACADE_ENVELOPE', 'Facade Glazing, Cladding & Building Envelope'),
        ('FINISHES', 'Internal Partitions, Screed & Architectural Finishes'),
        ('COMMISSIONING', 'Testing, Statutory Commissioning & Handover'),
    )

    RISK_CHOICES = (
        ('LOW', 'Low Risk (On Schedule)'),
        ('MEDIUM', 'Medium Risk (Approaching Gate)'),
        ('HIGH', 'High Risk (Slippage / Defect Present)'),
        ('CRITICAL', 'Critical Risk (Stop-Work / Gate Failed)'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    milestone_code = models.CharField(max_length=50, default=generate_ms_code, db_index=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='construction_milestones')
    name = models.CharField(max_length=255)
    phase = models.CharField(max_length=50, choices=PHASE_CHOICES, default='SUPERSTRUCTURE')
    description = models.TextField(blank=True, null=True)
    sequence_order = models.IntegerField(default=1)
    critical_path = models.BooleanField(default=False, help_text="Designates if milestone is on the project critical path")

    # Schedule & Dates
    planned_start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(help_text="Planned target completion date")
    actual_start_date = models.DateField(null=True, blank=True)
    actual_completion_date = models.DateField(null=True, blank=True)
    baseline_start_date = models.DateField(null=True, blank=True)
    baseline_end_date = models.DateField(null=True, blank=True)
    duration_days = models.IntegerField(default=30)
    variance_days = models.IntegerField(default=0, help_text="Calculated schedule variance in days (+ is delayed, - is ahead)")

    # Status & Progress
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PLANNED')
    progress_percentage = models.IntegerField(default=0, help_text="Physical progress 0-100%")
    physical_progress_notes = models.TextField(blank=True, null=True)
    
    # Risk Classification
    risk_level = models.CharField(max_length=20, choices=RISK_CHOICES, default='LOW')
    risk_factors = models.JSONField(default=list, blank=True, help_text="Dynamic risk justification points")

    # Dependencies & Critical Path
    dependencies = models.JSONField(default=list, blank=True, help_text="Predecessor milestone objects [{id, code, name, is_blocking}]")

    # Integrated Linkages
    linked_inspection_ids = models.JSONField(default=list, blank=True, help_text="Linked field inspection records [{id, ref, type, status, outcome}]")
    linked_issue_ids = models.JSONField(default=list, blank=True, help_text="Linked site issues/defects [{id, ref, title, severity, status}]")
    linked_bim_model_id = models.CharField(max_length=255, blank=True, null=True)
    bim_deviation_mm = models.FloatField(default=0.0, help_text="LiDAR/point cloud deviation in mm")
    bim_tolerance_max_mm = models.FloatField(default=15.0, help_text="Max allowable BIM tolerance in mm")
    survey_variance_meters = models.FloatField(default=0.0, help_text="GNSS RTK rover coordinate variance in meters")
    digital_eye_verified = models.BooleanField(default=False)

    # Evidence Vault
    evidence_documents = models.JSONField(default=list, blank=True, help_text="Uploaded test certs, lab reports, structural signoffs [{name, url, file_type, size, category}]")
    evidence_photos = models.JSONField(default=list, blank=True, help_text="Progress site photos [{url, caption, timestamp}]")

    # Verification Gates & Digital Sign-off
    verification_requirements = models.JSONField(default=dict, blank=True, help_text="Configured gate checks required for signoff")
    verification_signoff = models.JSONField(default=dict, blank=True, help_text="Digital signoff, certificate ref, signature hash")
    verified_by_name = models.CharField(max_length=255, blank=True, null=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    # Delay Management
    is_delayed = models.BooleanField(default=False)
    delay_reason = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sequence_order', 'target_date']

    def __str__(self):
        return f"[{self.milestone_code}] {self.name} - {self.project.name} ({self.status})"


class SiteVerification(models.Model):
    """
    Physical site verification, coordinate boundary checking, GNSS RTK rover telemetry,
    setback encroachment audit, and statutory digital certification.
    """
    METHOD_CHOICES = (
        ('GNSS_RTK_SURVEY', 'Tersus Oscar GNSS RTK Rover'),
        ('TERSU_ROVER', 'Tersus GNSS Rover Calibration'),
        ('DRONE_PHOTOGRAMMETRY', 'Aerial Drone Photogrammetry & LiDAR'),
        ('GPR_SCAN', 'Ground Penetrating Radar (GPR) Scan'),
        ('TOTAL_STATION', 'Total Station Cadastral Survey'),
        ('SETBACK_AUDIT', 'Statutory Building Setback Audit'),
        ('LEVEL_ELEVATION', 'Foundation Datum Elevation Check'),
    )

    STATUS_CHOICES = (
        ('PENDING_VERIFICATION', 'Pending Verification'),
        ('IN_PROGRESS', 'Survey In Progress'),
        ('VERIFIED', 'Verified & Certified'),
        ('VARIANCE_DETECTED', 'Variance Detected'),
        ('FLAGGED', 'Flagged for Review / SWO'),
        ('RESOLVED', 'Resolved & Remediated'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification_reference = models.CharField(max_length=100, db_index=True, null=True, blank=True, default=generate_vrf_ref)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='verifications')
    
    method = models.CharField(max_length=50, choices=METHOD_CHOICES, default='GNSS_RTK_SURVEY')
    device_identifier = models.CharField(max_length=255, default='Tersus Oscar GNSS RTK #042')
    
    # Cadastral Beacons & Coordinates
    cadastral_beacon_numbers = models.JSONField(default=list, blank=True, help_text="Cadastral beacon pillar IDs e.g. ['BC-LA-2026/089', 'BC-LA-2026/090']")
    boundary_coordinates = models.JSONField(default=list, blank=True, help_text="Approved site perimeter boundary polygon points")
    captured_coordinates = models.JSONField(default=dict, blank=True, help_text="Field measured coordinates {lat, lng, elevation, accuracy_horizontal_mm}")
    approved_coordinates = models.JSONField(default=dict, blank=True, help_text="Approved Masterplan/Planning coordinates {lat, lng, elevation}")
    
    # Spatial Tolerances & Displacements
    variance_meters = models.FloatField(default=0.0, help_text="Calculated horizontal spatial deviation in meters")
    elevation_variance_meters = models.FloatField(default=0.0, help_text="Calculated vertical elevation offset in meters")
    tolerance_limit_meters = models.FloatField(default=0.05, help_text="Allowable regulatory threshold in meters (default 50mm)")
    variance_detected = models.BooleanField(default=False, help_text="True if variance exceeds tolerance limit")
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING_VERIFICATION')
    
    # Encroachment & Setback Audit
    encroachment_detected = models.BooleanField(default=False, help_text="True if structure encroaches beyond approved building boundary line")
    encroachment_details = models.TextField(blank=True, null=True, help_text="Encroachment specifics and affected boundary offsets")

    # RTK Rover Telemetry
    telemetry_data = models.JSONField(default=dict, blank=True, help_text="Live RTK rover metrics {satellites, hdop, vdop, rtk_fix, latency_sec}")

    # Evidence Vault
    evidence_documents = models.JSONField(default=list, blank=True, help_text="Certified survey plans, RINEX raw logs, calibration certs")
    evidence_photos = models.JSONField(default=list, blank=True, help_text="Benchmark site photos, beacon tripod setups")
    
    # Statutory Digital Sign-off & Certification
    digital_cert_ref = models.CharField(max_length=100, blank=True, null=True, help_text="Statutory certificate reference e.g. CERT-VRF-2026-0042")
    signature_hash = models.CharField(max_length=255, blank=True, null=True, help_text="SHA-256 cryptographic verification signature")
    verified_by_name = models.CharField(max_length=255, blank=True, null=True)
    verified_by_role = models.CharField(max_length=255, blank=True, null=True, default='Directorate of Cadastral & Structural Survey')
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.verification_reference} - {self.method} ({self.project.name})"
