"""
Digital Eye — Unified Field Sensory Hub.

Houses the field-sensory and CDE integration data models:
  * FieldDevice          — device registry (Tersus GNSS MVP SI, GPR, PUNDIT)
  * SensorDataFile       — raw sensor artifacts in object storage with checksums
  * GPRSurvey / GPRAnomaly — subsurface radar surveys and detections
  * PUNDITTest / PunditTest — ultrasonic pulse-velocity NDT (BS 1881-203 / ASTM C597)
  * GnssSurvey / GnssBenchmark / GnssBoundaryPoint — geodetic positioning
  * TrimbleConnection / TrimbleProject / BIMElementMapping / LiveStream —
    Trimble Connect OAuth, CDE sync, BIM GUID mappings and live video streams
  * BIMStructuralElement — 3D structural BIM elements and clearance states
  * GPRScan              — high-frequency radargram and rebar scans
  * DigitalEyeFinding    — correlated non-destructive findings and NCR escalations
  * AIAnalysisRecord     — multi-modal fusion AI scan inferences
  * ProcessingQueueJob   — background telemetry processing pipeline queue
  * EvidenceSpatialPoint — geo-referenced RTK survey markers and spatial beacons
  * DeviceReportRecord   — stamped QA/QC engineering reports
"""
import uuid
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone


def _ref(prefix: str, length: int = 6) -> str:
    return f"{prefix}-{datetime.now().year}-{uuid.uuid4().hex[:length].upper()}"


def generate_survey_ref():
    return _ref('GPR')


def generate_test_ref():
    return _ref('PND')


def generate_gnss_ref():
    return _ref('GNS')


def generate_device_ref():
    return f"DE-{uuid.uuid4().hex[:8].upper()}"


# ======================================================================
# Device Registry
# ======================================================================

class FieldDevice(models.Model):
    """
    Registry of Digital Eye field devices: Tersus GNSS (MVP SI) rovers, GPR
    carts, PUNDIT ultrasonic instruments. Devices are registered by operators
    or the integration APIs; telemetry fields (battery, position) stay null
    until the device actually reports them.
    """
    DEVICE_TYPES = [
        ('tersus_gnss', 'Tersus GNSS (MVP SI)'),
        ('gpr', 'Ground Penetrating Radar'),
        ('pundit', 'PUNDIT Ultrasonic NDT'),
        ('scanner', '3D Laser Scanner'),
        ('other', 'Other Sensor'),
    ]
    STATUS_CHOICES = [
        ('registered', 'Registered'),
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('maintenance', 'Under Maintenance'),
        ('retired', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_reference = models.CharField(max_length=40, unique=True, default=generate_device_ref)
    device_id = models.CharField(max_length=100, unique=True, help_text="Serial / asset tag")
    name = models.CharField(max_length=150, blank=True, default='')
    device_type = models.CharField(max_length=30, choices=DEVICE_TYPES, db_index=True)
    model = models.CharField(max_length=100, blank=True, default='')
    manufacturer = models.CharField(max_length=100, blank=True, default='')
    firmware_version = models.CharField(max_length=50, blank=True, default='')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='registered', db_index=True)
    assigned_project = models.ForeignKey(
        'projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='field_devices',
    )
    battery_level = models.IntegerField(null=True, blank=True, help_text="Percent 0-100 as reported by the device")
    latitude = models.FloatField(null=True, blank=True, help_text="Last reported position")
    longitude = models.FloatField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    calibration_date = models.DateField(null=True, blank=True)
    calibration_certificate_url = models.URLField(max_length=500, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)

    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='registered_field_devices',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['device_type', 'device_id']

    def __str__(self):
        return f"{self.device_id} ({self.get_device_type_display()})"


class SensorDataFile(models.Model):
    """
    A raw sensor artifact (radargram, depth slice, PUNDIT raw export, RINEX
    file, photo) stored through Django's default storage (Cloudflare R2 when
    configured). Carries a SHA-256 checksum for storage & integrity QA.
    """
    FILE_TYPES = [
        ('gpr_radargram', 'GPR Radargram'),
        ('gpr_depth_slice', 'GPR Depth Slice'),
        ('gpr_raw', 'GPR Raw Dataset'),
        ('pundit_raw', 'PUNDIT Raw Export'),
        ('gnss_rinex', 'GNSS RINEX Log'),
        ('photo', 'Site Photo'),
        ('video', 'Video'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to='digital_eye/%Y/%m/', max_length=500)
    file_type = models.CharField(max_length=30, choices=FILE_TYPES, default='other')
    file_name = models.CharField(max_length=255, blank=True, default='')
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    sha256_checksum = models.CharField(max_length=64, blank=True, default='', db_index=True,
                                       help_text="SHA-256 of the file content at upload time")
    description = models.CharField(max_length=255, blank=True, default='')
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sensor_data_files',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.file_name or str(self.id)


# ======================================================================
# GPR Subsurface Radar
# ======================================================================

class GPRSurvey(models.Model):
    """
    A GPR survey over an area of a project: raw radargrams and depth slices
    are attached as SensorDataFile entries; detected subsurface features are
    stored as GPRAnomaly rows.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('in_progress', 'Survey In Progress'),
        ('processing', 'AI Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_reference = models.CharField(max_length=40, unique=True, default=generate_survey_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='gpr_surveys')
    device = models.ForeignKey(FieldDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name='gpr_surveys')

    title = models.CharField(max_length=255)
    survey_area = models.CharField(max_length=255, blank=True, default='', help_text="e.g. 'Foundation Zone B, Grid 4-7'")
    structural_element = models.CharField(max_length=100, blank=True, default='',
                                          help_text="Structural element under survey, e.g. COL-C24")
    antenna_frequency_mhz = models.PositiveIntegerField(null=True, blank=True)
    depth_range_m = models.FloatField(null=True, blank=True, help_text="Maximum penetration depth in metres")
    grid_spacing_m = models.FloatField(null=True, blank=True)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    coordinate_system = models.CharField(max_length=100, blank=True, default='WGS84 / UTM Zone 31N (Minna Datum)')

    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='gpr_surveys_operated')
    operator_name = models.CharField(max_length=150, blank=True, default='')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    files = models.ManyToManyField(SensorDataFile, blank=True, related_name='gpr_surveys')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='gpr_surveys_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.survey_reference} — {self.title}"


class GPRAnomaly(models.Model):
    """
    A subsurface feature detected in a GPR survey (AI adapter output or
    operator marking): voids, utilities, rebar, delamination, moisture.
    """
    ANOMALY_TYPES = [
        ('void', 'Void / Cavity'),
        ('utility', 'Buried Utility'),
        ('rebar', 'Reinforcement / Rebar'),
        ('delamination', 'Delamination'),
        ('moisture', 'Moisture / Saturation'),
        ('burial', 'Buried Object'),
        ('other', 'Other Feature'),
    ]
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]
    DETECTION_SOURCES = [
        ('ai', 'AI Adapter'),
        ('device', 'Device Software'),
        ('manual', 'Operator Marking'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(GPRSurvey, on_delete=models.CASCADE, related_name='anomalies')
    anomaly_type = models.CharField(max_length=30, choices=ANOMALY_TYPES, db_index=True)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='low')
    depth_m = models.FloatField(null=True, blank=True, help_text="Depth below surface in metres")
    estimated_size_m = models.FloatField(null=True, blank=True, help_text="Approximate lateral extent in metres")
    coordinates = models.JSONField(null=True, blank=True, help_text="{'x','y'} grid or {'latitude','longitude'}")
    depth_slice = models.JSONField(null=True, blank=True, help_text="Depth-slice reference {'slice_depth_m', 'index'}")
    rebar_cover_mm = models.FloatField(null=True, blank=True, help_text="Concrete cover over detected rebar")
    description = models.TextField(blank=True, default='')
    confidence = models.FloatField(null=True, blank=True, help_text="Detection confidence 0.0-1.0")
    detected_by = models.CharField(max_length=20, choices=DETECTION_SOURCES, default='manual')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['survey', 'depth_m']

    def __str__(self):
        return f"{self.get_anomaly_type_display()} @ {self.depth_m}m ({self.survey.survey_reference})"


# ======================================================================
# PUNDIT Ultrasonic NDT
# ======================================================================

class PUNDITTest(models.Model):
    """
    A PUNDIT (Portable Ultrasonic Non-destructive Digital Indicating Tester)
    ultrasonic test: pulse-velocity concrete QA per BS 1881-203 / ASTM C597,
    or crack-depth testing using the time-difference method.

    Accepts both string IDs (e.g. demo seeds) and UUIDs.
    """
    TRANSDUCER_TYPES = [
        ('direct', 'Direct Transmission'),
        ('semi_direct', 'Semi-Direct Transmission'),
        ('indirect', 'Indirect (Surface) Transmission'),
        ('DIRECT', 'Direct Transmission'),
        ('SEMI_DIRECT', 'Semi-Direct Transmission'),
        ('INDIRECT', 'Indirect (Surface) Transmission'),
    ]
    TEST_TYPES = [
        ('pulse_velocity', 'Pulse Velocity (Concrete QA)'),
        ('crack_depth', 'Crack Depth (Time-Difference Method)'),
        ('surface_quality', 'Surface Quality / Homogeneity'),
    ]
    QUALITY_GRADES = [
        ('excellent', 'Excellent (>= 4.5 km/s)'),
        ('good', 'Good (3.75 – 4.5 km/s)'),
        ('questionable', 'Questionable (3.0 – 3.75 km/s)'),
        ('poor', 'Poor (2.0 – 3.0 km/s)'),
        ('very_poor', 'Very Poor (< 2.0 km/s)'),
        ('pending', 'Pending Analysis'),
        ('EXCELLENT', 'Excellent'),
        ('GOOD', 'Good'),
        ('DOUBTFUL', 'Doubtful'),
        ('POOR', 'Poor'),
        ('VERY_POOR', 'Very Poor'),
    ]

    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    test_reference = models.CharField(max_length=100, unique=True, default=generate_test_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='pundit_tests', null=True, blank=True)
    device = models.ForeignKey(FieldDevice, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='pundit_tests')
    scan_session = models.ForeignKey('scans.ScanSession', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='pundit_tests')

    # Project / element string denormalizations
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element_id_str = models.CharField(max_length=100, blank=True, null=True)
    structural_element_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element_guid = models.CharField(max_length=100, blank=True, null=True)

    test_type = models.CharField(max_length=30, choices=TEST_TYPES, default='pulse_velocity')
    structural_element = models.CharField(max_length=100, blank=True, default='',
                                          help_text="Element tested, e.g. COL-C24")

    device_model = models.CharField(max_length=100, default='Proceq Pundit PL-200 UPV', blank=True)
    transducer_frequency_khz = models.PositiveIntegerField(null=True, blank=True, help_text="e.g. 54 kHz")
    transducer_type = models.CharField(
        max_length=50, choices=TRANSDUCER_TYPES, blank=True, default='DIRECT',
        help_text="Transducer coupling arrangement (BS 1881-203)",
    )
    test_location = models.CharField(
        max_length=255, blank=True, default='',
        help_text="On-site location of the test station, e.g. Grid D-7 Core Section",
    )
    # Measured inputs
    path_length_mm = models.FloatField(null=True, blank=True, help_text="Direct transducer path length")
    pulse_time_us = models.FloatField(null=True, blank=True, help_text="Measured transit time in microseconds")
    transit_time_us = models.FloatField(null=True, blank=True, help_text="Alias for pulse_time_us")
    
    # Crack depth method (BS 1881-203): uncracked and cracked transit times.
    crack_path_length_mm = models.FloatField(null=True, blank=True)
    crack_pulse_time_us = models.FloatField(null=True, blank=True)
    uncracked_pulse_time_us = models.FloatField(null=True, blank=True)

    surface_temperature_c = models.FloatField(null=True, blank=True)
    surface_condition = models.CharField(max_length=150, blank=True, default='')

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # Computed outputs & metrics
    velocity_km_s = models.FloatField(null=True, blank=True, help_text="Computed pulse velocity in km/s")
    pulse_velocity_ms = models.FloatField(null=True, blank=True, help_text="Computed pulse velocity in m/s")
    quality_grade = models.CharField(max_length=50, choices=QUALITY_GRADES, default='pending')
    concrete_quality_rating = models.CharField(max_length=50, default='EXCELLENT', blank=True)
    estimated_compressive_strength_mpa = models.FloatField(null=True, blank=True)
    crack_depth_mm = models.FloatField(null=True, blank=True, help_text="Computed crack depth")
    estimated_crack_depth_mm = models.FloatField(null=True, blank=True)
    waveform_samples = models.JSONField(default=list, blank=True)

    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='pundit_tests_operated')
    operator_name = models.CharField(max_length=255, blank=True, default='Engr. F. Balogun (NDE Specialist)')
    tested_at = models.DateTimeField(null=True, blank=True)
    test_date = models.DateField(default=timezone.localdate, null=True, blank=True)
    status = models.CharField(max_length=50, default='VERIFIED', blank=True)
    notes = models.TextField(blank=True, null=True, default='')

    files = models.ManyToManyField(SensorDataFile, blank=True, related_name='pundit_tests')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='pundit_tests_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.test_reference} — {self.structural_element or self.structural_element_name or self.get_test_type_display()}"

    def save(self, *args, **kwargs):
        # Ensure test_date is a date object, not a datetime
        if self.test_date is not None and isinstance(self.test_date, datetime):
            self.test_date = self.test_date.date()

        # Synchronize pulse_time_us and transit_time_us
        if self.transit_time_us is not None and self.pulse_time_us is None:
            self.pulse_time_us = self.transit_time_us
        elif self.pulse_time_us is not None and self.transit_time_us is None:
            self.transit_time_us = self.pulse_time_us

        # Synchronize pulse_velocity_ms and velocity_km_s
        if self.pulse_velocity_ms is not None and self.velocity_km_s is None:
            self.velocity_km_s = round(self.pulse_velocity_ms / 1000.0, 4)
        elif self.velocity_km_s is not None and self.pulse_velocity_ms is None:
            self.pulse_velocity_ms = round(self.velocity_km_s * 1000.0, 1)

        # Synchronize structural element labels
        if self.structural_element_name and not self.structural_element:
            self.structural_element = self.structural_element_name
        elif self.structural_element and not self.structural_element_name:
            self.structural_element_name = self.structural_element

        # Synchronize crack depth
        if self.estimated_crack_depth_mm is not None and self.crack_depth_mm is None:
            self.crack_depth_mm = self.estimated_crack_depth_mm
        elif self.crack_depth_mm is not None and self.estimated_crack_depth_mm is None:
            self.estimated_crack_depth_mm = self.crack_depth_mm

        super().save(*args, **kwargs)


# Alias PunditTest to PUNDITTest for backwards compatibility with origin/main
PunditTest = PUNDITTest


# ======================================================================
# Tersus GNSS Positioning
# ======================================================================

class GnssSurvey(models.Model):
    """
    A Tersus GNSS (MVP SI) positioning survey: benchmarks, boundary
    coordinates and GIS projection (Lagos Minna Datum / UTM 31N).
    """
    METHOD_CHOICES = [
        ('rtk', 'RTK (Real-Time Kinematic)'),
        ('static', 'Static Observation'),
        ('ppp', 'Precise Point Positioning'),
        ('dgps', 'DGPS'),
    ]
    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    FIX_QUALITY = [
        ('fixed', 'RTK Fixed'),
        ('float', 'RTK Float'),
        ('dgps', 'DGPS'),
        ('single', 'Single / Autonomous'),
        ('none', 'No Fix'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey_reference = models.CharField(max_length=40, unique=True, default=generate_gnss_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='gnss_surveys')
    device = models.ForeignKey(FieldDevice, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='gnss_surveys')

    title = models.CharField(max_length=255)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='rtk')
    projection = models.CharField(max_length=100, blank=True, default='UTM Zone 31N / Minna Datum (Lagos)')
    fix_quality = models.CharField(max_length=20, choices=FIX_QUALITY, blank=True, default='')
    variance_summary = models.JSONField(
        null=True, blank=True,
        help_text="Computed coordinate variance vs approved design, in metres",
    )

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planned')
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='gnss_surveys_operated')
    operator_name = models.CharField(max_length=150, blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    files = models.ManyToManyField(SensorDataFile, blank=True, related_name='gnss_surveys')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='gnss_surveys_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.survey_reference} — {self.title}"


class GnssBenchmark(models.Model):
    """A surveyed benchmark point with WGS84 geographic and UTM projected coordinates."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(GnssSurvey, on_delete=models.CASCADE, related_name='benchmarks')
    point_id = models.CharField(max_length=50, help_text="Benchmark designation, e.g. BM-001")
    latitude = models.FloatField()
    longitude = models.FloatField()
    ellipsoidal_height = models.FloatField(null=True, blank=True)
    easting = models.FloatField(null=True, blank=True, help_text="UTM easting (computed projection)")
    northing = models.FloatField(null=True, blank=True, help_text="UTM northing (computed projection)")
    utm_zone = models.CharField(max_length=10, blank=True, default='')
    accuracy_mm = models.FloatField(null=True, blank=True, help_text="Reported accuracy of the fix")
    description = models.CharField(max_length=255, blank=True, default='')
    captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['survey', 'point_id']
        constraints = [
            models.UniqueConstraint(fields=['survey', 'point_id'], name='unique_benchmark_per_survey'),
        ]

    def __str__(self):
        return f"{self.point_id} ({self.survey.survey_reference})"


class GnssBoundaryPoint(models.Model):
    """An ordered boundary/setting-out point of the site perimeter."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    survey = models.ForeignKey(GnssSurvey, on_delete=models.CASCADE, related_name='boundary_points')
    sequence = models.PositiveIntegerField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    easting = models.FloatField(null=True, blank=True)
    northing = models.FloatField(null=True, blank=True)
    label = models.CharField(max_length=50, blank=True, default='')
    description = models.CharField(max_length=255, blank=True, default='')
    captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['survey', 'sequence']

    def __str__(self):
        return f"{self.survey.survey_reference} boundary #{self.sequence}"


# ======================================================================
# Trimble Connect (BIM & Live Streams)
# ======================================================================

class TrimbleConnection(models.Model):
    """
    A Trimble Connect OAuth 2.0 (PKCE) connection and CDE model synchronization status.
    Tokens and synchronization state are persisted here.
    """
    STATUS_CHOICES = [
        ('disconnected', 'Disconnected'),
        ('pending_authorization', 'Pending Authorization'),
        ('connected', 'Connected'),
        ('error', 'Error'),
        ('CONNECTED', 'Connected'),
        ('DISCONNECTED', 'Disconnected'),
    ]

    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    name = models.CharField(max_length=150, blank=True, default='Trimble Connect')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='disconnected', db_index=True)

    # OAuth state
    authorization_code = models.CharField(max_length=500, blank=True, default='')
    pkce_verifier = models.CharField(max_length=128, blank=True, default='',
                                     help_text="PKCE code_verifier for the in-flight handshake")
    access_token = models.TextField(blank=True, default='')
    refresh_token = models.TextField(blank=True, default='')
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=255, blank=True, default='')

    trimble_user_id = models.CharField(max_length=100, blank=True, default='')
    trimble_user_name = models.CharField(max_length=150, blank=True, default='')

    # Health check & diagnostics
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    last_health_status = models.CharField(max_length=30, blank=True, default='')
    last_error = models.TextField(blank=True, default='')

    # CDE Sync attributes from origin/main
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='trimble_connections', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, default='Project', blank=True)
    trimble_project_id = models.CharField(max_length=100, default='TC-PRJ-99201', blank=True)
    trimble_project_name = models.CharField(max_length=255, default='CDE Model Sync', blank=True)
    region = models.CharField(max_length=50, default='EU-West', blank=True)
    last_sync_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    synced_models_count = models.IntegerField(default=12)
    synced_elements_count = models.IntegerField(default=1420)
    bcf_topics_count = models.IntegerField(default=4)
    webhook_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='trimble_connections')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} [{self.get_status_display()}]"

    @property
    def token_expired(self) -> bool:
        if not self.token_expires_at:
            return True
        return timezone.now() >= self.token_expires_at


class TrimbleProject(models.Model):
    """
    A Trimble Connect project discovered via the API, optionally linked to a
    Nexucon project for BIM model sync.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(TrimbleConnection, on_delete=models.CASCADE, related_name='trimble_projects')
    external_id = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=255, blank=True, default='')
    linked_project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='trimble_projects')
    raw_metadata = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_id'], name='unique_trimble_project_per_connection'),
        ]

    def __str__(self):
        return f"{self.name or self.external_id} (Trimble)"


class BIMElementMapping(models.Model):
    """
    BIM entity/element with GUID mapping: extracted from Trimble Connect models
    or from uploaded IFC files. Structural element IDs (e.g. COL-C24) map to
    IFC GlobalIds so NDT evidence can be correlated to design elements.
    """
    SOURCE_CHOICES = [
        ('trimble', 'Trimble Connect'),
        ('ifc_upload', 'Uploaded IFC'),
        ('manual', 'Manual Entry'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='bim_elements')
    trimble_project = models.ForeignKey(TrimbleProject, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='elements')
    bim_guid = models.CharField(max_length=64, blank=True, default='', db_index=True,
                                help_text="IFC GlobalId / Trimble GUID")
    element_id = models.CharField(max_length=100, blank=True, default='', db_index=True,
                                  help_text="Structural element designation, e.g. COL-C24")
    element_name = models.CharField(max_length=255, blank=True, default='')
    element_type = models.CharField(max_length=100, blank=True, default='', help_text="e.g. IfcColumn")
    level = models.CharField(max_length=100, blank=True, default='')
    discipline = models.CharField(max_length=100, blank=True, default='')
    coordinates = models.JSONField(null=True, blank=True, help_text="{'x','y','z'} element coordinates")
    properties = models.JSONField(default=dict, blank=True, help_text="IFC property set values")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='bim_elements_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['element_id']
        indexes = [models.Index(fields=['project', 'element_id'])]

    def __str__(self):
        return f"{self.element_id or self.bim_guid} ({self.project_id})"


class LiveStream(models.Model):
    """
    A live video stream mapped to BIM element coordinates for visual overlay.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('live', 'Live'),
        ('offline', 'Offline'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='live_streams')
    name = models.CharField(max_length=255)
    stream_url = models.URLField(max_length=1000, blank=True, default='')
    stream_provider = models.CharField(max_length=50, blank=True, default='trimble_connect')
    stream_token = models.TextField(blank=True, default='', help_text="Provider access token for playback, if issued")
    mapped_element = models.ForeignKey(BIMElementMapping, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='live_streams')
    mapped_coordinates = models.JSONField(null=True, blank=True,
                                          help_text="BIM element coordinates the feed is anchored to")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='live_streams_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} [{self.get_status_display()}] ({self.project_id})"


# ======================================================================
# Digital Eye Scan-to-BIM & AI Analytics Models (origin/main)
# ======================================================================

class BIMStructuralElement(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    element_guid = models.CharField(max_length=100, default=uuid.uuid4)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default='COLUMN')
    discipline = models.CharField(max_length=50, default='Structural')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='structural_elements', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    model_id = models.CharField(max_length=100, blank=True, null=True)
    model_name = models.CharField(max_length=255, blank=True, null=True)
    grid_location = models.CharField(max_length=100, blank=True, default='')
    level = models.CharField(max_length=100, blank=True, default='')
    elevation_level_m = models.FloatField(null=True, blank=True)
    coordinates_3d = models.JSONField(default=dict, blank=True)
    bounding_box = models.JSONField(default=dict, blank=True)
    designed_concrete_grade = models.CharField(max_length=50, default='C35/45')
    concrete_grade_specified = models.CharField(max_length=50, blank=True, null=True)
    designed_rebar_spacing_mm = models.IntegerField(default=150)
    designed_cover_depth_mm = models.IntegerField(default=40)
    gpr_clearance_status = models.CharField(max_length=50, default='VERIFIED')
    pundit_clearance_status = models.CharField(max_length=50, default='VERIFIED')
    ai_anomaly_count = models.IntegerField(default=0)
    open_findings_count = models.IntegerField(default=0)
    last_inspected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.grid_location})"


class GPRScan(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    scan_reference = models.CharField(max_length=100, unique=True)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='gpr_scans', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element = models.ForeignKey(BIMStructuralElement, on_delete=models.SET_NULL, null=True, blank=True, related_name='gpr_scans')
    structural_element_id_str = models.CharField(max_length=100, blank=True, null=True)
    structural_element_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element_guid = models.CharField(max_length=100, blank=True, null=True)
    grid_axis = models.CharField(max_length=100, default='Grid 4-C to 4-D')
    antenna_frequency = models.CharField(max_length=50, default='2.0_GHZ')
    device_name = models.CharField(max_length=255, default='Proceq GS8000 Subsurface GPR')
    operator_name = models.CharField(max_length=255, default='Engr. K. Adeyemi (Lead Geophysicist)')
    survey_date = models.DateField(default=timezone.localdate)
    transect_length_m = models.FloatField(default=12.5)
    max_penetration_depth_m = models.FloatField(default=0.8)
    measured_rebar_spacing_mm = models.IntegerField(default=150)
    specified_rebar_spacing_mm = models.IntegerField(default=150)
    measured_cover_depth_mm = models.IntegerField(default=45)
    rebar_deficiency_detected = models.BooleanField(default=False)
    void_detected = models.BooleanField(default=False)
    delamination_detected = models.BooleanField(default=False)
    utility_strike_hazard = models.BooleanField(default=False)
    dielectric_constant = models.FloatField(default=6.2)
    dielectric_permittivity = models.FloatField(default=6.2)
    radargram_image_url = models.TextField(default='https://res.cloudinary.com/depeqzb6z/image/upload/v1779868806/Make_it_look_like_an_202605192308_1_rdayse.png')
    c_scan_heatmap_url = models.TextField(blank=True, null=True)
    raw_data_file_url = models.TextField(blank=True, null=True)
    file_size = models.CharField(max_length=50, default='14.2 MB')
    status = models.CharField(max_length=50, default='VERIFIED')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.scan_reference} - {self.grid_axis}"


class DigitalEyeFinding(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    finding_reference = models.CharField(max_length=100, unique=True)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='digital_eye_findings', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element_id_str = models.CharField(max_length=100, blank=True, null=True)
    structural_element_name = models.CharField(max_length=255, blank=True, null=True)
    structural_element_guid = models.CharField(max_length=100, blank=True, null=True)
    gpr_scan_id = models.CharField(max_length=100, blank=True, null=True)
    pundit_test_id = models.CharField(max_length=100, blank=True, null=True)
    taxonomy = models.CharField(max_length=100, default='REBAR_SPACING_DEFICIENCY')
    title = models.CharField(max_length=255)
    description = models.TextField()
    severity = models.CharField(max_length=50, default='HIGH')
    confidence_score = models.FloatField(default=92.0)
    depth_mm = models.FloatField(null=True, blank=True)
    deviation_mm = models.FloatField(null=True, blank=True)
    gps_coordinates = models.JSONField(default=dict, blank=True)
    evidence_photos = models.JSONField(default=list, blank=True)
    radargram_snippet_url = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, default='OPEN')
    ncr_reference = models.CharField(max_length=100, blank=True, null=True)
    bcf_topic_guid = models.CharField(max_length=100, blank=True, null=True)
    assigned_inspector = models.CharField(max_length=255, blank=True, null=True)
    resolution_deadline = models.DateField(null=True, blank=True)
    corrective_action = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.finding_reference}: {self.title}"


class AIAnalysisRecord(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='digital_eye_ai_analyses', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    scan_reference = models.CharField(max_length=100, default='SCAN-AI-01')
    model_version = models.CharField(max_length=100, default='Nexucon Structural-Vision v3.2 + GPR-Inversion')
    analysis_type = models.CharField(max_length=50, default='MULTI_MODAL_FUSION')
    analyzed_at = models.DateTimeField(default=timezone.now)
    confidence_score = models.FloatField(default=96.4)
    overall_health_score = models.FloatField(default=94.0)
    total_elements_scanned = models.IntegerField(default=48)
    anomalies_detected = models.IntegerField(default=2)
    critical_defects_count = models.IntegerField(default=0)
    compliance_check_passed = models.BooleanField(default=True)
    findings = models.JSONField(default=list, blank=True)
    thermal_metrics = models.JSONField(default=dict, blank=True)
    deviation_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.scan_reference} - {self.analysis_type}"


class ProcessingQueueJob(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    job_reference = models.CharField(max_length=100, unique=True)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    device_id = models.CharField(max_length=100, default='TER-S1-008')
    source_type = models.CharField(max_length=50, default='GPR_RADAR_GS8000')
    stage = models.CharField(max_length=50, default='COMPLETED')
    progress_percentage = models.IntegerField(default=100)
    node_type = models.CharField(max_length=50, default='CLOUD_GPU_CLUSTER')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    file_count = models.IntegerField(default=12)
    total_bytes = models.CharField(max_length=50, default='1.4 GB')
    error_message = models.TextField(blank=True, null=True)
    logs = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.job_reference} [{self.stage}]"


class EvidenceSpatialPoint(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='spatial_points', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, null=True)
    beacon_code = models.CharField(max_length=100, blank=True, null=True)
    name = models.CharField(max_length=255)
    title = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    layer_type = models.CharField(max_length=50, default='GNSS_RTK_BEACON')
    lat = models.FloatField(default=6.4281)
    lng = models.FloatField(default=3.4219)
    elevation_m = models.FloatField(default=4.2)
    accuracy_mm = models.FloatField(default=1.8)
    deviation_mm = models.FloatField(default=0.0)
    severity = models.CharField(max_length=50, default='NORMAL')
    structural_element_name = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.beacon_code or self.name} ({self.layer_type})"


class DeviceReportRecord(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    report_reference = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=255)
    device_type = models.CharField(max_length=50, default='PUNDIT')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='device_reports', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, default='Project')
    element_id = models.CharField(max_length=100, blank=True, null=True)
    element_name = models.CharField(max_length=255, blank=True, null=True)
    report_type = models.CharField(max_length=100, default='Ultrasonic Pulse Velocity (UPV) QA/QC Report')
    standards_cited = models.JSONField(default=list, blank=True)
    compliance_status = models.CharField(max_length=50, default='COMPLIANT')
    executive_summary = models.TextField(blank=True, default='')
    metrics = models.JSONField(default=dict, blank=True)
    generated_by = models.CharField(max_length=255, default='Nexucon AI Automated Compliance Engine')
    certified_engineer = models.CharField(max_length=255, default='Engr. T. Oladipo, FNSE, COREN Reg.')
    stamped_at = models.DateTimeField(default=timezone.now)
    file_size = models.CharField(max_length=50, default='2.4 MB')
    download_url = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.report_reference}: {self.title}"
