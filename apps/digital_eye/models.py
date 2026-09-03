"""
Digital Eye — Unified Field Sensory Hub (implementation plan §5A, Weeks 1–3).

Houses the field-sensory data models:
  * FieldDevice      — device registry (Tersus GNSS MVP SI, GPR, PUNDIT)
  * SensorDataFile   — raw sensor artifacts in object storage with checksums
  * GPRSurvey / GPRAnomaly — subsurface radar surveys and detections
  * PUNDITTest       — ultrasonic pulse-velocity NDT (BS 1881-203 / ASTM C597)
  * GnssSurvey / GnssBenchmark / GnssBoundaryPoint — geodetic positioning
  * TrimbleConnection / TrimbleProject / BIMElementMapping / LiveStream —
    Trimble Connect OAuth, BIM GUID mappings and live video streams

All measurement fields are populated from real device uploads or real
operator input — defaults are blank/null, never invented values.
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
    configured). Carries a SHA-256 checksum for storage & integrity QA
    (plan §5 Week 3 QA: "Storage & checksum integrity testing").
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

    The measured values (path length, transit time) are always operator /
    device input. Velocity and quality grade are computed deterministically
    from those measurements — see apps.digital_eye.adapters.PUNDITAdapter.
    """
    TRANSDUCER_TYPES = [
        ('direct', 'Direct Transmission'),
        ('semi_direct', 'Semi-Direct Transmission'),
        ('indirect', 'Indirect (Surface) Transmission'),
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
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_reference = models.CharField(max_length=40, unique=True, default=generate_test_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='pundit_tests')
    device = models.ForeignKey(FieldDevice, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='pundit_tests')
    scan_session = models.ForeignKey('scans.ScanSession', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='pundit_tests')

    test_type = models.CharField(max_length=30, choices=TEST_TYPES, default='pulse_velocity')
    structural_element = models.CharField(max_length=100, blank=True, default='',
                                          help_text="Element tested, e.g. COL-C24")

    transducer_frequency_khz = models.PositiveIntegerField(null=True, blank=True, help_text="e.g. 54 kHz")
    transducer_type = models.CharField(
        max_length=20, choices=TRANSDUCER_TYPES, blank=True, default='',
        help_text="Transducer coupling arrangement (BS 1881-203)",
    )
    test_location = models.CharField(
        max_length=255, blank=True, default='',
        help_text="On-site location of the test station, e.g. Grid D-7 Core Section",
    )
    # Measured inputs
    path_length_mm = models.FloatField(null=True, blank=True, help_text="Direct transducer path length")
    pulse_time_us = models.FloatField(null=True, blank=True, help_text="Measured transit time in microseconds")
    # Crack depth method (BS 1881-203): uncracked and cracked transit times.
    crack_path_length_mm = models.FloatField(null=True, blank=True)
    crack_pulse_time_us = models.FloatField(null=True, blank=True)
    uncracked_pulse_time_us = models.FloatField(null=True, blank=True)

    surface_temperature_c = models.FloatField(null=True, blank=True)
    surface_condition = models.CharField(max_length=150, blank=True, default='')

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # Computed outputs (deterministic adapter)
    velocity_km_s = models.FloatField(null=True, blank=True, help_text="Computed pulse velocity")
    quality_grade = models.CharField(max_length=20, choices=QUALITY_GRADES, default='pending')
    crack_depth_mm = models.FloatField(null=True, blank=True, help_text="Computed crack depth")

    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='pundit_tests_operated')
    operator_name = models.CharField(max_length=150, blank=True, default='')
    tested_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    files = models.ManyToManyField(SensorDataFile, blank=True, related_name='pundit_tests')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='pundit_tests_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.test_reference} — {self.structural_element or self.get_test_type_display()}"


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
    A Trimble Connect OAuth 2.0 (PKCE) connection. Tokens are persisted here
    (not in memory) so the refresh lifecycle survives restarts and can be
    health-checked (plan §5 Week 1).
    """
    STATUS_CHOICES = [
        ('disconnected', 'Disconnected'),
        ('pending_authorization', 'Pending Authorization'),
        ('connected', 'Connected'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, blank=True, default='Trimble Connect')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='disconnected', db_index=True)

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

    # Health check & diagnostics (plan §5 Week 1)
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    last_health_status = models.CharField(max_length=30, blank=True, default='')
    last_error = models.TextField(blank=True, default='')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='trimble_connections')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
    BIM entity/element with GUID mapping (plan §5 Week 2): extracted from
    Trimble Connect models or from uploaded IFC files. Structural element IDs
    (e.g. COL-C24) map to IFC GlobalIds so NDT evidence can be correlated to
    design elements.
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
    A live video stream (e.g. Trimble Connect / camera feed) mapped to BIM
    element coordinates for visual-support overlay (plan §5 Week 2).
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
