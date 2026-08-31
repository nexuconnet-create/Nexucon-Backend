from django.db import models
import uuid
from django.utils import timezone

class ScanSession(models.Model):
    """
    Represents a single scanning session initiated by the edge scanner.
    Tracks status, project association, and sensory payloads.
    """
    STATUS_CHOICES = [
        ('initialized', 'Initialized'),
        ('uploading', 'Uploading'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='scans')
    scanner_id = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='initialized')
    expected_size_mb = models.FloatField(null=True, blank=True)
    timestamp = models.DateTimeField(null=True, blank=True)
    sensors_used = models.JSONField(default=list, blank=True)
    rgb_url = models.URLField(max_length=500, blank=True, null=True)
    thermal_url = models.URLField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class ScanMetadata(models.Model):
    """
    Stores additional metadata associated with a ScanSession,
    such as GPS location and operator notes.
    """
    session = models.OneToOneField(ScanSession, on_delete=models.CASCADE, related_name='metadata')
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    elevation = models.FloatField(null=True, blank=True)
    operator_id = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

class ProcessingTask(models.Model):
    """
    Tracks the status of AI or other background processing tasks for a scan.
    """
    TASK_TYPES = [
        ('ai_analysis', 'AI Analysis'),
        ('bim_alignment', 'BIM Alignment'),
        ('deviation_analysis', 'Deviation Analysis'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='tasks')
    task_type = models.CharField(max_length=50, choices=TASK_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Defect(models.Model):
    """
    Identified defects from AI processing.
    """
    DEFECT_TYPES = [
        ('crack', 'Crack'),
        ('spalling', 'Spalling'),
        ('corrosion', 'Corrosion'),
        ('thermal_anomaly', 'Thermal Anomaly'),
        ('deformation', 'Deformation'),
        ('delamination', 'Delamination'),
    ]
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]
    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In Progress'),
        ('RESOLVED', 'Resolved'),
        ('REJECTED', 'Rejected'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='defects')
    type = models.CharField(max_length=50, choices=DEFECT_TYPES)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    location_x = models.FloatField(null=True, blank=True)
    location_y = models.FloatField(null=True, blank=True)
    location_z = models.FloatField(null=True, blank=True)
    bbox_xmin = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) left edge of the defect in the source image")
    bbox_ymin = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) top edge of the defect in the source image")
    bbox_xmax = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) right edge of the defect in the source image")
    bbox_ymax = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) bottom edge of the defect in the source image")
    image_url = models.URLField(max_length=500, blank=True, null=True)
    thermal_image_url = models.URLField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True)
    confidence_score = models.FloatField(null=True, blank=True, help_text="AI confidence score")
    is_false_positive = models.BooleanField(default=False, help_text="Flagged as false positive by multimodal AI")
    evidence_link = models.URLField(max_length=500, blank=True, null=True, help_text="Link to the specific point cloud data or raw evidence")
    grid_zone = models.CharField(max_length=50, blank=True, null=True)
    room_level = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class ThermalAnomaly(models.Model):
    """
    Identified thermal anomalies.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='thermal_anomalies')
    temperature_variance = models.FloatField(help_text="Temperature variance from baseline")
    severity = models.CharField(max_length=20, choices=Defect.SEVERITY_CHOICES)
    status = models.CharField(max_length=20, choices=Defect.STATUS_CHOICES, default='OPEN')
    location_x = models.FloatField(null=True, blank=True)
    location_y = models.FloatField(null=True, blank=True)
    location_z = models.FloatField(null=True, blank=True)
    bbox_xmin = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) left edge of the anomaly in the thermal image")
    bbox_ymin = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) top edge of the anomaly in the thermal image")
    bbox_xmax = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) right edge of the anomaly in the thermal image")
    bbox_ymax = models.FloatField(null=True, blank=True, help_text="Normalised (0-1) bottom edge of the anomaly in the thermal image")
    image_url = models.URLField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True)
    confidence_score = models.FloatField(null=True, blank=True, help_text="AI confidence score")
    evidence_link = models.URLField(max_length=500, blank=True, null=True, help_text="Link to the specific point cloud data or raw evidence")
    grid_zone = models.CharField(max_length=50, blank=True, null=True)
    room_level = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class HardwareAlert(models.Model):
    """
    Telemetry and hardware alerts from physical scanning devices.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scanner = models.ForeignKey('scans.Scanner', on_delete=models.CASCADE, related_name='alerts')
    session = models.ForeignKey(ScanSession, on_delete=models.SET_NULL, null=True, blank=True, related_name='hardware_alerts')
    issue = models.CharField(max_length=255)
    severity = models.CharField(max_length=20, choices=[('low', 'Low'), ('high', 'High')])
    description = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

class BIMAlignmentResult(models.Model):
    """
    Result of BIM alignment and deviation analysis.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ScanSession, on_delete=models.CASCADE, related_name='bim_alignment')
    alignment_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    transformation_matrix = models.JSONField(help_text="4x4 Transformation matrix", null=True, blank=True)
    mean_deviation = models.FloatField(null=True, blank=True)
    max_deviation = models.FloatField(null=True, blank=True)
    min_deviation = models.FloatField(null=True, blank=True)
    top_deviations = models.JSONField(null=True, blank=True, help_text="List of top deviations")
    clashes = models.JSONField(null=True, blank=True, help_text="Full clash-detection results persisted when the alignment ran")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class ScanFile(models.Model):
    """
    Tracks every individual file uploaded for a scan session.
    Provides a structured registry of all sensor artifacts.
    """
    FILE_TYPES = [
        ('lidar', 'LiDAR Point Cloud'),
        ('rgb', 'RGB Image'),
        ('thermal', 'Thermal Image'),
        ('gps', 'GPS Telemetry'),
        ('gaussian_splat', '3D Gaussian Splat'),
        ('bim', 'BIM Model'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='files')
    file_type = models.CharField(max_length=50, choices=FILE_TYPES)
    file_url = models.URLField(max_length=500)
    file_name = models.CharField(max_length=255, blank=True, default='')
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.file_type} file for session {self.session_id}"

    def fresh_url(self):
        """
        Re-sign the object on demand. The URL stored at upload time carries a
        short-lived signature (1 hour), so anything reading this file later
        must ask for a fresh URL instead of using the stale one.
        """
        from .utils import refresh_storage_url
        return refresh_storage_url(self.file_url)

class ProgressValidationResult(models.Model):
    """
    Stores the calculated progress score and volume metrics based on BIM alignment and point cloud coverage.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(ScanSession, on_delete=models.CASCADE, related_name='progress_validation')
    progress_score = models.FloatField(help_text="Calculated progress score (0.0 to 1.0)")
    covered_area_sqm = models.FloatField(null=True, blank=True, help_text="Total scanned area mapped to BIM.")
    volume_metrics = models.JSONField(null=True, blank=True, help_text="Volumetric data if applicable.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Progress {self.progress_score} for Session {self.session_id}"

class ScanPlan(models.Model):
    """
    Guided scan plan for operators. Defines areas to be scanned.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='scan_plans')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    target_area = models.CharField(max_length=255, help_text="e.g., 'Floor 3, Zone A'")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    sensors_used = models.JSONField(default=list, blank=True, help_text="List of sensors required for the scan")
    operator = models.CharField(max_length=100, blank=True, default='', help_text="Assigned operator name or ID")
    scanner = models.ForeignKey(
        'scans.Scanner', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='scan_plans', help_text="Device assigned to carry out this plan",
    )
    scheduled_for = models.DateTimeField(null=True, blank=True, help_text="Planned start of the survey")
    latitude = models.FloatField(null=True, blank=True, help_text="Target area latitude, for the plan map view")
    longitude = models.FloatField(null=True, blank=True, help_text="Target area longitude, for the plan map view")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} for {self.project.name}"

class Scanner(models.Model):
    """
    Registry of physical scanning devices (e.g. the Tersus MVP S1 handheld).

    Devices report in through the heartbeat endpoint; fields that only a device
    can know (battery, firmware, position) stay null until it does, rather than
    being guessed server-side.
    """
    STATUS_CHOICES = [
        ('online', 'Online'),
        ('idle', 'Idle'),
        ('offline', 'Offline'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_id = models.CharField(max_length=100, unique=True, help_text="Serial/asset tag, e.g. NAVIS-V3-001")
    model = models.CharField(max_length=100, blank=True, default='', help_text="e.g. Tersus MVP S1")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline')
    battery_level = models.IntegerField(null=True, blank=True, help_text="Percent 0-100, reported by the device")
    firmware_version = models.CharField(max_length=50, blank=True, default='')
    latitude = models.FloatField(null=True, blank=True, help_text="Last reported position")
    longitude = models.FloatField(null=True, blank=True, help_text="Last reported position")
    last_seen = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['device_id']

    def __str__(self):
        return self.device_id


class GnssTelemetry(models.Model):
    """
    GNSS/RTK quality samples reported by a device during a survey. Drives the
    QA/QC fix-rate metric and its trend.
    """
    FIX_TYPES = [
        ('rtk_fixed', 'RTK Fixed'),
        ('rtk_float', 'RTK Float'),
        ('dgps', 'DGPS'),
        ('single', 'Single'),
        ('none', 'No Fix'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ScanSession, on_delete=models.CASCADE, related_name='gnss_telemetry',
        null=True, blank=True,
    )
    scanner = models.ForeignKey(
        Scanner, on_delete=models.SET_NULL, related_name='gnss_telemetry',
        null=True, blank=True,
    )
    fix_rate = models.FloatField(help_text="Percentage of epochs with an RTK fix (0-100)")
    fix_type = models.CharField(max_length=20, choices=FIX_TYPES, default='rtk_fixed')
    satellites = models.IntegerField(null=True, blank=True)
    horizontal_accuracy_m = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']

    def __str__(self):
        return f"GNSS {self.fix_rate}% at {self.recorded_at}"


class ComplianceCertificate(models.Model):
    """
    Certificate issued once a session's compliance checks have been reviewed.
    """
    STATUS_CHOICES = [
        ('issued', 'Issued'),
        ('revoked', 'Revoked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ScanSession, on_delete=models.CASCADE, related_name='compliance_certificates',
    )
    certificate_number = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='issued')
    total_checks = models.IntegerField(default=0)
    passed_checks = models.IntegerField(default=0)
    failed_checks = models.IntegerField(default=0)
    issued_by = models.CharField(max_length=150, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        return self.certificate_number


class ComplianceCheck(models.Model):
    id = models.CharField(max_length=50, primary_key=True) # e.g., CHK-001
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='compliance_checks', null=True, blank=True)
    element = models.CharField(max_length=255)
    rule = models.CharField(max_length=255)
    measured = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=[('pass', 'Pass'), ('fail', 'Fail')])
    confidence = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.id} - {self.element}"

class StopWorkFlag(models.Model):
    """
    Formal stop-work order raised from the compliance dashboard against a
    scan session (optionally tied to a specific failed compliance check).
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('lifted', 'Lifted'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='stop_work_flags')
    compliance_check = models.ForeignKey(ComplianceCheck, on_delete=models.SET_NULL, null=True, blank=True, related_name='stop_work_flags')
    reason = models.TextField()
    flagged_by = models.EmailField(blank=True, default='', help_text="Email of the user who raised the flag")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    lifted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Stop-work {self.status} on session {self.session_id}"
