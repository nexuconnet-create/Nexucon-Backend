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
        ('bim_model', 'BIM Model File (.ifc / .rvt, kept for re-import)'),
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
    # Optional project link for project-level artifacts (e.g. a screenshot of
    # the BIM model 3D preview captured for the report) that are not attached
    # to any single test/survey.
    project = models.ForeignKey(
        'projects.Project', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sensor_files',
    )
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
    floor = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Floor/level of the test station, e.g. 'Ground Floor', 'First Floor'",
    )
    weather_condition = models.CharField(
        max_length=150, blank=True, default='',
        help_text="Weather condition at test time, recorded by the operator on site",
    )
    concrete_age_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Concrete age at test time in days (optional; strength gain "
                  "past 28 days is minimal, so ages beyond 28 are informative "
                  "rather than corrective)",
    )

    # No assumed instrument: the device is whatever was actually used
    # (blank until recorded / linked via the FieldDevice relation).
    device_model = models.CharField(max_length=100, default='', blank=True)
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
    rebound_number = models.FloatField(
        null=True, blank=True,
        help_text="Rebound hammer reading for this element (shared by its points "
                  "unless a reading carries its own; required only for SonReb curves)")

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # Computed outputs & metrics
    velocity_km_s = models.FloatField(null=True, blank=True, help_text="Computed pulse velocity in km/s")
    pulse_velocity_ms = models.FloatField(null=True, blank=True, help_text="Computed pulse velocity in m/s")
    quality_grade = models.CharField(max_length=50, choices=QUALITY_GRADES, default='pending')
    # Honest default: no quality is claimed until the engine has graded the
    # element from real measurements (never a pre-filled 'EXCELLENT').
    concrete_quality_rating = models.CharField(max_length=50, default='', blank=True)
    estimated_compressive_strength_mpa = models.FloatField(null=True, blank=True)
    strength_curve_snapshot = models.JSONField(
        null=True, blank=True,
        help_text="Provenance of the curve that produced the element-mean E.C.S: "
                  "curve id/name/type/formula/valid range (Nexucon Link)")
    crack_depth_mm = models.FloatField(null=True, blank=True, help_text="Computed crack depth")
    estimated_crack_depth_mm = models.FloatField(null=True, blank=True)
    waveform_samples = models.JSONField(default=list, blank=True)

    # Multi-Model AI Ensemble / Uncertainty Quantification
    ai_ci_lower_mpa = models.FloatField(null=True, blank=True)
    ai_ci_upper_mpa = models.FloatField(null=True, blank=True)
    ai_pof_pct = models.FloatField(null=True, blank=True, help_text="Probability of failure against design strength")
    ai_data_quality = models.CharField(max_length=50, blank=True, default='')
    ai_reasoning_traces = models.JSONField(default=list, blank=True)

    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='pundit_tests_operated')
    # No fabricated operator attribution: blank until the real operator's
    # name is recorded with the station (was a hard-coded engineer's name).
    operator_name = models.CharField(max_length=255, blank=True, default='')
    tested_at = models.DateTimeField(null=True, blank=True)
    test_date = models.DateField(default=timezone.localdate, null=True, blank=True)
    # A station is not 'VERIFIED' until someone actually verified it.
    status = models.CharField(max_length=50, default='RECORDED', blank=True)
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

    def reading_rows(self):
        """
        Ordered per-point rows for this test — the single data path the
        report, charts and AI consume. Returns dicts:
            {label, path_mm, transit_us, velocity_km_s, ecs_mpa,
             uncracked_us, crack_depth_mm, surface_condition}
        Tests recorded through the multi-reading model return their real
        A/B/C… rows; legacy single-reading tests return their scalar
        measurement as one 'A' row (values never invented — missing
        measurements stay None).
        """
        readings = list(self.readings.all())
        if readings:
            return [
                {'label': r.point_label,
                 'path_mm': r.path_length_mm,
                 'transit_us': r.transit_time_us,
                 'velocity_km_s': r.velocity_km_s,
                 'ecs_mpa': r.ecs_mpa,
                 'uncracked_us': r.uncracked_transit_time_us,
                 'crack_depth_mm': r.crack_depth_mm,
                 'surface_condition': r.surface_condition}
                for r in readings
            ]
        from apps.digital_eye.adapters import PUNDITAdapter
        from apps.digital_eye.strength_curves import apply_active_curve
        row = {'label': 'A', 'path_mm': None, 'transit_us': None,
               'velocity_km_s': None, 'ecs_mpa': None, 'uncracked_us': None,
               'crack_depth_mm': None, 'surface_condition': None}
        if self.test_type == 'crack_depth':
            row.update({
                'path_mm': self.crack_path_length_mm,
                'transit_us': self.crack_pulse_time_us,
                'uncracked_us': self.uncracked_pulse_time_us,
                'crack_depth_mm': PUNDITAdapter.compute_crack_depth_mm(
                    self.crack_path_length_mm, self.crack_pulse_time_us,
                    self.uncracked_pulse_time_us),
            })
        elif self.test_type == 'surface_quality':
            row['surface_condition'] = self.surface_condition or None
        else:
            velocity = self.velocity_km_s
            if velocity is None:
                velocity = PUNDITAdapter.compute_velocity_km_s(
                    self.path_length_mm, self.pulse_time_us)
            ecs, _snapshot = apply_active_curve(
                self.project, velocity,
                rebound_number=self.rebound_number,
                temperature_c=self.surface_temperature_c)
            row.update({
                'path_mm': self.path_length_mm,
                'transit_us': self.pulse_time_us,
                'velocity_km_s': velocity,
                'ecs_mpa': ecs if velocity is not None else None,
            })
        return [row]

    def element_mean_velocity_km_s(self):
        """Mean pulse velocity over this test's readings (the element verdict
        basis per A1); None when no reading is computable."""
        velocities = [row['velocity_km_s'] for row in self.reading_rows()
                      if row['velocity_km_s'] is not None]
        return (sum(velocities) / len(velocities)) if velocities else None

    def element_point_count(self):
        """
        How many test points contributed a computable velocity to this
        element's mean — the n in the curve's s/sqrt(n) confidence-margin
        divisor (15 Sep 2026: "three test points averaged", with the
        standard error factored into the post-velocity calculation).

        The element verdict is the mean of these points, so its confidence
        margin narrows as sqrt(n) of them — BS EN 13791 logic, where more
        readings buy a tighter confidence on the in-situ estimate. A legacy
        single-reading test, or one whose points yielded no velocity,
        honestly counts as 1: the figure is then a single reading and earns
        no averaging benefit.
        """
        return max(1, len([row for row in self.reading_rows()
                           if row['velocity_km_s'] is not None]))

    def element_mean_crack_depth_mm(self):
        """Mean crack depth over this test's readings (the element verdict
        for multi-point crack tests); None when no point yields a depth."""
        depths = [row['crack_depth_mm'] for row in self.reading_rows()
                  if row['crack_depth_mm'] is not None]
        return (sum(depths) / len(depths)) if depths else None

    def refresh_confidence_metrics(self):
        """
        Recompute the ai_* fields from the recorded data using the single
        honest engine the report also uses (apps.digital_eye.confidence_metrics
        — no fabricated uncertainty, no Monte Carlo with invented errors):

        * ai_ci_lower/upper_mpa — 95% CI = estimate +/- 1.96 x the curve
          regression standard error; null when the curve has no regression
          (laboratory default, lookup table, manual parameters).
        * ai_pof_pct           — P(true strength < 25 N/mm2), normal
          approximation; null when no CI exists.
        * ai_data_quality      — BS EN 12504-4 bucket from point count and
          within-element velocity spread.
        * ai_reasoning_traces  — the numbered, data-cited derivation chain.

        Cross-element outlier checks are a project-level analysis and stay
        in the report path (adapters.PUNDITAdapter._confidence_metrics).
        Fields are reset to their honest empty values before recomputation
        so a weaker re-save never leaves stale figures behind.
        """
        from . import confidence_metrics as cm

        self.ai_ci_lower_mpa = None
        self.ai_ci_upper_mpa = None
        self.ai_pof_pct = None
        self.ai_data_quality = ''
        self.ai_reasoning_traces = []

        if self.test_type != 'pulse_velocity':
            return      # strength confidence applies to strength tests only

        rows = self.reading_rows()
        point_velocities = [row['velocity_km_s'] for row in rows
                            if row['velocity_km_s'] is not None]
        mean_v = (sum(point_velocities) / len(point_velocities)) \
            if point_velocities else None
        spread_pct = None
        if len(point_velocities) > 1 and mean_v:
            spread_pct = round(
                (max(point_velocities) - min(point_velocities))
                / mean_v * 100, 1)

        # Legacy single-reading tests persist their element verdict here
        # (the multi-reading serializer path already did): the same
        # active-curve computation the report and serializer use, with the
        # curve provenance snapshotted. Velocities outside the calibrated
        # range keep a None strength — never extrapolated.
        if self.estimated_compressive_strength_mpa is None and mean_v is not None:
            from .strength_curves import apply_active_curve
            self.estimated_compressive_strength_mpa, snapshot = apply_active_curve(
                self.project, mean_v,
                rebound_number=self.rebound_number,
                temperature_c=self.surface_temperature_c,
                # mean_v is the mean of these points, so the curve's
                # confidence margin narrows as sqrt(n) of them.
                n_points=len(point_velocities))
            if not isinstance(self.strength_curve_snapshot, dict):
                self.strength_curve_snapshot = snapshot

        # Standard error: prefer the provenance snapshot of the curve that
        # produced this value; fall back to the project's active curve
        # (None honestly for curves without a regression).
        se = None
        snapshot = self.strength_curve_snapshot
        if isinstance(snapshot, dict):
            se = snapshot.get('standard_error')
        if se is None:
            se = cm.curve_standard_error_mpa(self.project)

        ecs = self.estimated_compressive_strength_mpa
        ci = cm.strength_confidence_interval(ecs, se)
        p_below = (cm.probability_below_design(ecs, se)
                   if ci is not None else None)
        quality = cm.data_quality_score(len(rows), spread_pct)

        element_summary = {
            'element': self.structural_element or self.structural_element_name or None,
            'point_velocities_m_s': [round(v * 1000, 2)
                                     for v in point_velocities],
            'mean_velocity_m_s': None if mean_v is None
            else round(mean_v * 1000, 2),
            'mean_ecs_n_mm2': None if ecs is None else round(ecs, 1),
            # Provenance of the figure above, straight from the snapshot
            # stored alongside it: the standard-error policy that moved it.
            'se_adjustment': (snapshot or {}).get('se_adjustment')
            if isinstance(snapshot, dict) else None,
        }
        self.ai_reasoning_traces = cm.reasoning_trace(
            element_summary, se_mpa=se, ci=ci, p_below=p_below,
            quality=quality)
        if ci is not None:
            self.ai_ci_lower_mpa = round(ci[0], 2)
            self.ai_ci_upper_mpa = round(ci[1], 2)
        if p_below is not None:
            self.ai_pof_pct = round(p_below * 100.0, 2)
        if quality and quality[0]:
            self.ai_data_quality = quality[0]

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

        # Honest confidence metrics (REFINED EXECUTIVE SUMMARY §2.2 "Path to
        # 95% Confidence"): persist the same figures the report computes —
        # from the curve's regression standard error and the recorded
        # readings. Nothing is fabricated; when the curve carries no
        # regression (laboratory default / lookup / manual entry) the
        # numeric fields stay null and the trace says so.
        self.refresh_confidence_metrics()

        # Synchronize crack depth
        if self.estimated_crack_depth_mm is not None and self.crack_depth_mm is None:
            self.crack_depth_mm = self.estimated_crack_depth_mm
        elif self.crack_depth_mm is not None and self.estimated_crack_depth_mm is None:
            self.estimated_crack_depth_mm = self.crack_depth_mm

        super().save(*args, **kwargs)


# Alias PunditTest to PUNDITTest for backwards compatibility with origin/main
PunditTest = PUNDITTest


class PUNDITReading(models.Model):
    """
    One reading at one test point of a PUNDIT test (A1 of the 4 Sep 2026
    review meeting: a structural element is tested at a minimum of three
    points — upper/middle/lower, open-ended — with the path length and
    transducer held constant and only the measurement varying).

    The operator records ONLY the field measurement at each point:
      - pulse-velocity testing: the transit time t (v = L / t);
      - crack-depth testing (time-difference method): the cracked transit
        time t_c and the uncracked transit time t_0 (d = L/2·√((t_c/t_0)²−1));
      - surface-quality testing: the observed surface condition.
    Computed outputs (velocity, E.C.S, crack depth) are derived on save and
    read-only through the API — they cannot be typed in or doctored.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test = models.ForeignKey(PUNDITTest, on_delete=models.CASCADE,
                             related_name='readings')
    point_label = models.CharField(
        max_length=10,
        help_text="Test point on the element: 'A', 'B', 'C', ... (A is the first point)",
    )
    path_length_mm = models.FloatField(null=True, blank=True,
                                       help_text="Transducer path length shared by the element's points")
    transit_time_us = models.FloatField(
        null=True, blank=True,
        help_text="The measured transit time at this point (t for pulse velocity, "
                  "t_cracked for crack depth; not measured for surface quality)")
    uncracked_transit_time_us = models.FloatField(
        null=True, blank=True,
        help_text="Crack-depth method only: the uncracked-path transit time t_0 "
                  "measured at this point")
    velocity_km_s = models.FloatField(null=True, blank=True,
                                      help_text="Computed v = L/t (read-only; never operator-entered)")
    ecs_mpa = models.FloatField(null=True, blank=True,
                                help_text="Computed E.C.S from the project's active calibration curve (read-only)")
    strength_curve_snapshot = models.JSONField(
        null=True, blank=True,
        help_text="Provenance of the curve that produced this point's E.C.S (Nexucon Link)")
    crack_depth_mm = models.FloatField(
        null=True, blank=True,
        help_text="Computed time-difference crack depth at this point (read-only)")
    surface_condition = models.CharField(
        max_length=150, blank=True, default='',
        help_text="Surface-quality method only: the condition observed at this point")
    rebound_number = models.FloatField(
        null=True, blank=True,
        help_text="Rebound hammer reading at this point (required only when the "
                  "project's active curve is a SonReb combination curve)")
    notes = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['point_label']
        constraints = [
            models.UniqueConstraint(fields=['test', 'point_label'],
                                    name='uniq_reading_point_per_test'),
        ]

    def __str__(self):
        return f"{self.test.test_reference} point {self.point_label}"

    def compute(self):
        """Deterministic per-point derivations — never a guessed number.
        v = L / t (mm/us == km/s) for pulse-velocity points;
        d = L/2·√((t_c/t_0)²−1) for crack-depth points. Each output is only
        set when its inputs exist and are valid. The E.C.S flows through the
        project's active Nexucon Link curve (8 Sep meeting: project-specific
        calibration), and the curve provenance is snapshotted onto the
        reading so every stored strength names the formula that made it."""
        from apps.digital_eye.adapters import PUNDITAdapter
        from apps.digital_eye.strength_curves import apply_active_curve
        is_crack_point = (self.transit_time_us is not None
                          and self.uncracked_transit_time_us is not None)
        if not is_crack_point \
                and self.path_length_mm and self.transit_time_us \
                and self.path_length_mm > 0 and self.transit_time_us > 0:
            # Pulse-velocity point: v = L / t. (Crack points carry the
            # CRACKED-path time — L/t_c is not a valid velocity, so none is
            # computed for them.)
            self.velocity_km_s = self.path_length_mm / self.transit_time_us
        else:
            self.velocity_km_s = None
        test = self.test
        # SonReb combination curves need a rebound number: this point's own
        # reading, else the element-level reading on the test.
        rebound = self.rebound_number
        if rebound is None and test is not None:
            rebound = test.rebound_number
        self.ecs_mpa, self.strength_curve_snapshot = apply_active_curve(
            test.project if test is not None and test.project_id else None,
            self.velocity_km_s,
            rebound_number=rebound,
            temperature_c=test.surface_temperature_c if test is not None else None)
        self.crack_depth_mm = PUNDITAdapter.compute_crack_depth_mm(
            self.path_length_mm, self.transit_time_us,
            self.uncracked_transit_time_us)

    def save(self, *args, **kwargs):
        self.compute()
        super().save(*args, **kwargs)


# ======================================================================
# Nexucon Link — compressive-strength (f_cu) conversion curves
# (8 Sep 2026 review meeting "Neural Link" + client Nexucon Link spec)
# ======================================================================

class StrengthCurve(models.Model):
    """
    A UPV-to-compressive-strength conversion curve. Every f_cu the platform
    reports is produced by exactly one of these (or the documented built-in
    fallback) — the active curve for the project, resolved by
    apps.digital_eye.strength_curves.resolve_active_curve.

    Formula parameters are in the m/s velocity domain (the client's
    specification and calibration CSVs are m/s); the apply() entry point
    converts the platform's canonical km/s once.

    Nothing here is ever fabricated: curves come from real calibration data
    (regression over UPV / rebound / cube-test pairs) or from the
    laboratory's documented fixed curve, and strengths are only produced
    inside each curve's calibrated range.
    """
    CURVE_TYPE_CHOICES = [
        ('linear', 'Linear: f = m*V + c'),
        ('polynomial', 'Polynomial (deg 2): f = c0 + c1*V + c2*V^2'),
        ('exponential', 'Exponential: f = a*exp(b*V) + c'),
        ('sonreb', 'SonReb (UPV + rebound): f = a*V^b*R^c'),
        ('lookup', 'Lookup table (piecewise-linear interpolation)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    curve_type = models.CharField(max_length=20, choices=CURVE_TYPE_CHOICES)
    standard = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Reference standard, e.g. 'BS 1881-203:1999', 'ACI 228.2R-2018'")
    # Null project = platform-wide curve; set = project-specific calibration.
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE,
                                null=True, blank=True,
                                related_name='strength_curves')
    velocity_unit = models.CharField(max_length=20, default='m/s')
    strength_unit = models.CharField(max_length=20, default='MPa')
    formula_params = models.JSONField(
        default=dict,
        help_text="Curve parameters in the m/s velocity domain, e.g. "
                  "{'m': 0.008961, 'c': -7.97} / {'a':..., 'b':..., 'c':...} / "
                  "{'coeffs': [c0, c1, c2]} / {'points': [{'v', 'f'}, ...]}")
    data_points = models.JSONField(
        default=list, blank=True,
        help_text="The real calibration pairs the curve was fitted from: "
                  "[{'v' (m/s), 'f' (MPa), 'r' (rebound, optional)}, ...]")
    valid_range_min_ms = models.FloatField(
        null=True, blank=True, help_text="Calibrated range lower bound (m/s)")
    valid_range_max_ms = models.FloatField(
        null=True, blank=True, help_text="Calibrated range upper bound (m/s)")
    r2_score = models.FloatField(
        null=True, blank=True, help_text="Coefficient of determination (regression-derived curves)")
    standard_error = models.FloatField(null=True, blank=True)
    aic = models.FloatField(null=True, blank=True)

    # --- Standard-error policy (15 Sep 2026 client direction) -------------
    # "factor standard errors into post-velocity calculations before
    # converting them into FCU reports". Default 'none' so every existing
    # curve keeps reporting exactly what it reported before; a Director
    # opts in per curve. See apps/digital_eye/se_adjustment.py for the
    # statistics and the honesty rules.
    SE_ADJUSTMENT_CHOICES = [
        ('none', 'No adjustment — report the curve estimate as fitted'),
        ('bias_correction',
         'Bias correction — add the measured mean residual (the "+2" '
         'gap-closing convention, computed from real pairs)'),
        ('confidence_margin',
         'Confidence margin — subtract k x standard error (conservative '
         'characteristic strength, BS EN 13791 logic)'),
    ]
    se_adjustment_method = models.CharField(
        max_length=30, choices=SE_ADJUSTMENT_CHOICES, default='none',
        help_text="How the curve's standard error is factored into the "
                  "reported f_cu. 'none' until deliberately enabled.")
    se_adjustment_factor = models.FloatField(
        default=1.0,
        help_text="k — the confidence factor for the confidence-margin "
                  "method (1.0 ~ 84% one-sided, 1.645 ~ 95% one-sided). "
                  "Unused by the other methods.")

    is_default = models.BooleanField(
        default=False,
        help_text="Exactly one platform-wide fallback curve should carry this")
    provenance = models.JSONField(
        default=dict, blank=True,
        help_text="How the curve was established: source, reference, operator")
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='strength_curves_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'name']

    def __str__(self):
        scope = f" — {self.project.name}" if self.project_id else ' (platform)'
        return f"{self.name}{scope} [{self.curve_type}]"

    def apply(self, velocity_km_s, rebound_number=None):
        """f_cu (MPa) for a velocity (km/s) — None outside the calibrated
        range or when the curve's inputs are missing. Never extrapolates."""
        from . import strength_curves
        return strength_curves.apply_curve_params(
            self.curve_type, self.formula_params, velocity_km_s,
            rebound_number=rebound_number,
            valid_range_ms=(None if self.valid_range_min_ms is None
                            or self.valid_range_max_ms is None
                            else [self.valid_range_min_ms, self.valid_range_max_ms]))

    def snapshot(self, temperature_c=None):
        """Formula provenance stored on every reading/test it produces."""
        from . import strength_curves
        return strength_curves.curve_snapshot(self, temperature_c=temperature_c)

    def se_analysis(self):
        """This curve's standard-error picture, computed from its own real
        calibration pairs: n, standard error s, mean residual, R², AIC, and
        whether an adjustment is supported by the data. Everything is
        derived — nothing here is a stored constant."""
        from .se_adjustment import analysis_payload
        return analysis_payload(self)

    def save(self, *args, **kwargs):
        # Fit statistics are DERIVED, never client-supplied: recompute them
        # from the curve's real stored pairs on every save. Curves without
        # pairs (manual / documented laboratory curves) keep null — a
        # statistic the data cannot support is never invented.
        from .strength_curves import curve_fit_stats
        (self.r2_score, self.standard_error, self.aic) = curve_fit_stats(
            self.curve_type, self.formula_params or {}, self.data_points or [])
        super().save(*args, **kwargs)


class ProjectCurveSetting(models.Model):
    """
    The Neural Link calibration setting for a project: which conversion
    curve its strength computations use. Set BEFORE data injection (8 Sep
    meeting: calibration is the first step of the PUNDIT workflow); when
    unset, the project falls back to the platform default curve.
    """
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE,
                                   related_name='curve_setting')
    active_curve = models.ForeignKey(StrengthCurve, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name='active_for_projects')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='curve_settings_updated')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        curve = self.active_curve.name if self.active_curve_id else '(platform default)'
        return f"{self.project.name}: {curve}"


class NexuconLinkSettings(models.Model):
    """
    Platform-wide Nexucon Link system settings (the client wireframe's
    "System Settings" panel, and the 15 Sep 2026 direction that the
    exponential curve be "the default system setting for system
    calculations").

    Read carefully, that instruction is about WHICH MODEL the platform
    offers by default — concrete behaviour is non-linear, so a linear fit
    should not be the first thing an operator reaches for. It is NOT a
    licence to seed an exponential curve with invented parameters: the
    exponential curve in the client's specification PDF evaluates to about
    1.5 N/mm2 at 4000 m/s, which is not a concrete strength, and shipping
    it as an active default would make every uncalibrated project report
    nonsense. So this model stores a PREFERENCE — the curve type the
    calibration workflow pre-selects and recommends — while the ACTIVE
    curve for any project remains whatever was really calibrated for it.

    Singleton rows (exactly one, pk=1) edited only by Directors and
    audit-logged, mirroring ReportCMSPassword-style platform settings.
    """
    id = models.PositiveSmallIntegerField(primary_key=True, default=1,
                                          editable=False)
    preferred_curve_type = models.CharField(
        max_length=20, choices=StrengthCurve.CURVE_TYPE_CHOICES,
        default='exponential',
        help_text="The curve type the calibration workflow pre-selects and "
                  "recommends. Concrete behaviour is non-linear, so the "
                  "default is exponential per the 15 Sep 2026 direction. "
                  "This does not activate any curve by itself.")
    default_standard = models.CharField(
        max_length=100, blank=True, default='BS 1881-203:1986',
        help_text="Standard the new curves are recorded against by default.")
    velocity_unit = models.CharField(max_length=20, default='m/s')
    strength_unit = models.CharField(max_length=20, default='MPa')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='nexucon_link_settings_updated')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Nexucon Link system settings'
        verbose_name_plural = 'Nexucon Link system settings'

    def __str__(self):
        return f"Nexucon Link settings (preferred curve: {self.preferred_curve_type})"

    def save(self, *args, **kwargs):
        self.id = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The singleton, created on first access with its documented
        defaults (no fabricated curve parameters anywhere in it)."""
        obj, _ = cls.objects.get_or_create(id=1)
        return obj


class CoreSample(models.Model):
    """
    A concrete core extracted on site and crushed in the laboratory — the
    ground-truth layer of the client's "path to 95%" roadmap (REFINED
    EXECUTIVE SUMMARY, Layer 3: cross-validation against real core results).

    The lab-measured compressive strength, paired with the in-situ UPV test
    performed at the same location (``pundit_test``), forms a REAL
    calibration pair — measured velocity vs laboratory strength — that feeds
    the regression engine exactly like a manually typed pair. Nothing is
    derived or invented here: every number is typed from the laboratory's
    test certificate, and a core without a lab result or without a linked
    test simply does not form a pair.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE,
                                related_name='core_samples')
    pundit_test = models.ForeignKey(
        PUNDITTest, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='core_samples',
        help_text='The in-situ UPV test at/near the core location — its '
                  'measured velocity pairs with the laboratory strength')
    structural_element = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Element the core was taken from, e.g. COL-C24")
    test_location = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Where on site the core was extracted")
    core_diameter_mm = models.FloatField(
        null=True, blank=True, help_text="Core diameter in mm (e.g. 100)")
    core_length_mm = models.FloatField(
        null=True, blank=True, help_text="Core length after trimming, in mm")
    lab_strength_mpa = models.FloatField(
        null=True, blank=True,
        help_text="Compressive strength from the laboratory crushing test "
                  "(MPa) — typed from the test certificate, never estimated")
    lab_report_ref = models.CharField(
        max_length=150, blank=True, default='',
        help_text="Laboratory test-certificate reference")
    sampled_at = models.DateTimeField(
        null=True, blank=True, help_text="When the core was extracted on site")
    notes = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True,
                                    related_name='core_samples_recorded')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        where = ' / '.join(filter(None, (self.structural_element,
                                         self.test_location)))
        return (f"Core sample — {where or 'location not recorded'} "
                f"({self.lab_strength_mpa if self.lab_strength_mpa is not None else 'no lab result yet'} MPa)")

    def calibration_pair(self):
        """The real (v m/s, f MPa) pair this core contributes — or None when
        either half is missing (no laboratory result yet, or no linked UPV
        test / the test has no velocity). The rebound number rides along
        when the linked test carries one, so SonReb can use the pair too."""
        if self.lab_strength_mpa is None or self.pundit_test_id is None:
            return None
        velocity_ms = self.pundit_test.pulse_velocity_ms
        if velocity_ms is None:
            return None
        pair = {'v': float(velocity_ms), 'f': float(self.lab_strength_mpa)}
        rebound = self.pundit_test.rebound_number
        if rebound is not None:
            pair['r'] = float(rebound)
        return pair


class PunditAnalysisReview(models.Model):
    """
    The engineer's corroboration of a PUNDIT AI analysis record (client
    principle 5: every AI analysis must be reviewed and given final input
    by a qualified engineer before it feeds a report — the AI is
    decision-support, never the signatory).

    The analysis itself lives in the Evidence Registry
    (apps.evidence.AIAnalysisRecord) and is immutable; this row is the
    separate human decision ON it. No row (or requires_human_review on the
    analysis) means the analysis still awaits engineer review — an honest
    pending state, never auto-corroborated.
    """
    DECISIONS = [
        ('corroborated', 'Corroborated by reviewing engineer'),
        ('returned', 'Returned — revisions / further testing required'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis = models.OneToOneField(
        'evidence.AIAnalysisRecord', on_delete=models.CASCADE,
        related_name='pundit_review')
    decision = models.CharField(max_length=20, choices=DECISIONS)
    notes = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True,
                                    related_name='pundit_analysis_reviews')
    reviewed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-reviewed_at']

    def __str__(self):
        return f"{self.analysis_id} — {self.get_decision_display()}"


# ======================================================================
# Rebar Scanning (Profoscope / Electromagnetic Locator)
# ======================================================================

class RebarTest(models.Model):
    """
    A concrete rebar scan test, typically captured via electromagnetic rebar locators
    (e.g., Profoscope) to determine cover depth, spacing, and estimate bar sizes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_reference = models.CharField(max_length=40, unique=True, default=generate_test_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='rebar_tests')
    device = models.ForeignKey('FieldDevice', on_delete=models.SET_NULL, null=True, blank=True, related_name='rebar_tests')
    
    # No assumed element or location: both are field records — blank until
    # the operator enters what was actually scanned.
    structural_element = models.CharField(max_length=200, blank=True, default='')
    test_location = models.CharField(max_length=200, blank=True, default='')
    
    main_bar_mm = models.FloatField(null=True, blank=True, help_text="Estimated main bar diameter in mm")
    links_mm = models.FloatField(null=True, blank=True, help_text="Estimated link/stirrup diameter in mm")
    spacing_mm = models.CharField(max_length=50, blank=True, default='', help_text="Spacing of bars/links (can be numeric or '-' if unknown)")
    cover_depth_mm = models.FloatField(null=True, blank=True, help_text="Measured concrete cover depth over rebar in mm")
    
    notes = models.TextField(blank=True, default='')
    recorded_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        where = ' / '.join(filter(None, (self.structural_element, self.test_location)))
        return f"{self.test_reference} - {where or 'element not recorded'}"


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

    # CDE Sync attributes. All of these describe REAL synchronisation state —
    # they start blank/zero/unset until an actual sync reports them; nothing
    # is fabricated for a connection that has never synced.
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='trimble_connections', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, default='', blank=True)
    trimble_project_id = models.CharField(max_length=100, default='', blank=True)
    trimble_project_name = models.CharField(max_length=255, default='', blank=True)
    region = models.CharField(max_length=50, default='', blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    synced_models_count = models.IntegerField(default=0)
    synced_elements_count = models.IntegerField(default=0)
    bcf_topics_count = models.IntegerField(default=0)
    webhook_active = models.BooleanField(default=False)

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


class BIMModelGeometry(models.Model):
    """
    Tessellated 3D preview meshes for a project's imported BIM model (IFC
    upload or RVT translated to IFC via Autodesk APS). Built once at import
    time from the same file the element mappings were extracted from, so the
    data-collection page can render a live model preview and highlight the
    target structural element the operator picks. One row per project — a
    re-import replaces the preview alongside the mapping upserts.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE,
                                   related_name='bim_model_geometry')
    source_file = models.CharField(max_length=255, blank=True, default='',
                                   help_text="Original uploaded file name")
    translated_from_rvt = models.BooleanField(default=False)
    element_count = models.IntegerField(default=0)
    # [{"guid", "name", "type", "verts": [x, y, z, ...], "faces": [i, j, k, ...]}]
    elements = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='bim_model_geometry_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'BIM model geometries'

    def __str__(self):
        return f"{self.source_file or 'BIM model'} ({self.element_count} elements, {self.project_id})"


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
    designed_concrete_grade = models.CharField(max_length=50, blank=True, default='')
    concrete_grade_specified = models.CharField(max_length=50, blank=True, null=True)
    designed_rebar_spacing_mm = models.IntegerField(null=True, blank=True)
    designed_cover_depth_mm = models.IntegerField(null=True, blank=True)
    # Clearances are not pre-granted: an element starts 'PENDING' until a
    # real scan clears it (previously defaulted to 'VERIFIED').
    gpr_clearance_status = models.CharField(max_length=50, blank=True, default='PENDING')
    pundit_clearance_status = models.CharField(max_length=50, blank=True, default='PENDING')
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
    grid_axis = models.CharField(max_length=100, blank=True, default='')
    antenna_frequency = models.CharField(max_length=50, blank=True, default='')
    device_name = models.CharField(max_length=255, blank=True, default='')
    # No fabricated operator attribution — blank until recorded.
    operator_name = models.CharField(max_length=255, blank=True, default='')
    survey_date = models.DateField(default=timezone.localdate)
    transect_length_m = models.FloatField(null=True, blank=True)
    max_penetration_depth_m = models.FloatField(null=True, blank=True)
    measured_rebar_spacing_mm = models.IntegerField(null=True, blank=True)
    specified_rebar_spacing_mm = models.IntegerField(null=True, blank=True)
    measured_cover_depth_mm = models.IntegerField(null=True, blank=True)
    rebar_deficiency_detected = models.BooleanField(default=False)
    void_detected = models.BooleanField(default=False)
    delamination_detected = models.BooleanField(default=False)
    utility_strike_hazard = models.BooleanField(default=False)
    dielectric_constant = models.FloatField(null=True, blank=True)
    dielectric_permittivity = models.FloatField(null=True, blank=True)
    # Radargram imagery is uploaded evidence — never a hard-coded stock image.
    radargram_image_url = models.TextField(blank=True, null=True)
    c_scan_heatmap_url = models.TextField(blank=True, null=True)
    raw_data_file_url = models.TextField(blank=True, null=True)
    file_size = models.CharField(max_length=50, blank=True, default='')
    # A scan is not 'VERIFIED' until someone actually verified it.
    status = models.CharField(max_length=50, blank=True, default='RECORDED')
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
    taxonomy = models.CharField(max_length=100, blank=True, default='')
    title = models.CharField(max_length=255)
    description = models.TextField()
    # Honest defaults: severity must be chosen by whoever raises the finding,
    # and a confidence score is only present when a real analysis produced it.
    severity = models.CharField(max_length=50, blank=True, default='MEDIUM')
    confidence_score = models.FloatField(null=True, blank=True)
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
    scan_reference = models.CharField(max_length=100, blank=True, default='')
    model_version = models.CharField(max_length=100, blank=True, default='')
    analysis_type = models.CharField(max_length=50, blank=True, default='')
    analyzed_at = models.DateTimeField(default=timezone.now)
    # No pre-filled AI verdicts: every metric below is null until a real
    # analysis computes it (previously defaulted to 96.4% confidence, a
    # 94.0 health score, 48 elements scanned and 2 anomalies detected).
    confidence_score = models.FloatField(null=True, blank=True)
    overall_health_score = models.FloatField(null=True, blank=True)
    total_elements_scanned = models.IntegerField(null=True, blank=True)
    anomalies_detected = models.IntegerField(null=True, blank=True)
    critical_defects_count = models.IntegerField(null=True, blank=True)
    compliance_check_passed = models.BooleanField(null=True, blank=True)
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
    device_id = models.CharField(max_length=100, blank=True, default='')
    source_type = models.CharField(max_length=50, blank=True, default='')
    stage = models.CharField(max_length=50, blank=True, default='QUEUED')
    progress_percentage = models.IntegerField(default=0)
    node_type = models.CharField(max_length=50, blank=True, default='')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    file_count = models.IntegerField(null=True, blank=True)
    total_bytes = models.CharField(max_length=50, blank=True, default='')
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
    layer_type = models.CharField(max_length=50, blank=True, default='')
    # No pre-seeded Lagos coordinates: a spatial point with no recorded
    # position is not a real survey point. lat/lng stay null until measured.
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    elevation_m = models.FloatField(null=True, blank=True)
    accuracy_mm = models.FloatField(null=True, blank=True)
    deviation_mm = models.FloatField(null=True, blank=True)
    severity = models.CharField(max_length=50, blank=True, default='')
    structural_element_name = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.beacon_code or self.name} ({self.layer_type})"


class DeviceReportRecord(models.Model):
    id = models.CharField(max_length=100, primary_key=True, default=uuid.uuid4)
    report_reference = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=255)
    device_type = models.CharField(max_length=50, blank=True, default='')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='device_reports', null=True, blank=True)
    project_id_str = models.CharField(max_length=100, blank=True, null=True)
    project_name = models.CharField(max_length=255, blank=True, default='')
    element_id = models.CharField(max_length=100, blank=True, null=True)
    element_name = models.CharField(max_length=255, blank=True, null=True)
    report_type = models.CharField(max_length=100, blank=True, default='')
    standards_cited = models.JSONField(default=list, blank=True)
    # Honest defaults: a report is not compliant until something assessed it,
    # and no engineer has certified it until a real name is recorded. These
    # previously defaulted to 'COMPLIANT' and a fabricated
    # 'Engr. T. Oladipo, FNSE, COREN Reg.', which attributed a statutory
    # certification to a named person who never gave it.
    compliance_status = models.CharField(max_length=50, default='NOT_ASSESSED')
    executive_summary = models.TextField(blank=True, default='')
    metrics = models.JSONField(default=dict, blank=True)
    generated_by = models.CharField(max_length=255, blank=True, default='')
    certified_engineer = models.CharField(max_length=255, blank=True, default='')
    stamped_at = models.DateTimeField(default=timezone.now)
    file_size = models.CharField(max_length=50, blank=True, default='')
    download_url = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.report_reference}: {self.title}"
