"""
Digital Eye API serializers.
"""
from rest_framework import serializers

from common.permissions import scoped_projects
from .models import (
    AIAnalysisRecord, BIMElementMapping, BIMStructuralElement, CoreSample,
    DeviceReportRecord, DigitalEyeFinding, EvidenceSpatialPoint, FieldDevice,
    GPRAnomaly, GPRScan, GPRSurvey, GnssBenchmark, GnssBoundaryPoint,
    GnssSurvey, LiveStream, ProjectCurveSetting, PUNDITReading, PUNDITTest,
    ProcessingQueueJob, SensorDataFile, StrengthCurve, TrimbleConnection,
    TrimbleProject,
)


class ScopedProjectField(serializers.PrimaryKeyRelatedField):
    """FK field that only accepts projects inside the requesting user's scope."""

    def get_queryset(self):
        from apps.projects.models import Project
        return scoped_projects(self.context['request'].user)


class FieldDeviceSerializer(serializers.ModelSerializer):
    assigned_project = ScopedProjectField(required=False, allow_null=True)
    device_type_display = serializers.CharField(source='get_device_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = FieldDevice
        fields = [
            'id', 'device_reference', 'device_id', 'name', 'device_type',
            'device_type_display', 'model', 'manufacturer', 'firmware_version',
            'status', 'status_display', 'assigned_project', 'battery_level',
            'latitude', 'longitude', 'last_seen', 'calibration_date',
            'calibration_certificate_url', 'notes', 'is_active', 'registered_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'device_reference', 'status', 'battery_level',
                            'latitude', 'longitude', 'last_seen', 'registered_by',
                            'created_at', 'updated_at']


class SensorDataFileSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SensorDataFile
        fields = [
            'id', 'file', 'file_type', 'file_name', 'file_size_bytes',
            'sha256_checksum', 'description', 'project', 'uploaded_by', 'uploaded_by_name',
            'created_at',
        ]
        read_only_fields = ['id', 'file_name', 'file_size_bytes', 'sha256_checksum',
                            'uploaded_by', 'created_at']

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            return obj.uploaded_by.get_full_name() or obj.uploaded_by.email
        return None

    def validate_file(self, value):
        if value.size > 512 * 1024 * 1024:
            raise serializers.ValidationError('File exceeds the 512 MB sensor-data limit.')
        return value


class GPRAnomalySerializer(serializers.ModelSerializer):
    anomaly_type_display = serializers.CharField(source='get_anomaly_type_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    detected_by_display = serializers.CharField(source='get_detected_by_display', read_only=True)

    class Meta:
        model = GPRAnomaly
        fields = [
            'id', 'survey', 'anomaly_type', 'anomaly_type_display', 'severity',
            'severity_display', 'depth_m', 'estimated_size_m', 'coordinates',
            'depth_slice', 'rebar_cover_mm', 'description', 'confidence',
            'detected_by', 'detected_by_display', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        anomaly_type = attrs.get('anomaly_type')
        if anomaly_type == 'rebar' and attrs.get('rebar_cover_mm') is None:
            if not attrs.get('description'):
                attrs['description'] = 'Rebar detected; cover not measured.'
        return attrs


class GPRSurveySerializer(serializers.ModelSerializer):
    project = ScopedProjectField()
    project_name = serializers.CharField(source='project.name', read_only=True)
    anomalies = GPRAnomalySerializer(many=True, read_only=True)
    anomaly_count = serializers.IntegerField(source='anomalies.count', read_only=True)
    file_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=SensorDataFile.objects.all(), source='files',
        required=False, write_only=True,
    )
    files = SensorDataFileSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = GPRSurvey
        fields = [
            'id', 'survey_reference', 'project', 'project_name', 'device',
            'title', 'survey_area',
            'structural_element', 'antenna_frequency_mhz', 'depth_range_m',
            'grid_spacing_m', 'latitude', 'longitude', 'coordinate_system',
            'operator', 'operator_name', 'status', 'status_display', 'started_at',
            'completed_at', 'notes', 'file_ids', 'files', 'anomalies',
            'anomaly_count', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'survey_reference', 'operator', 'status',
                            'created_by', 'created_at', 'updated_at']


class PUNDITReadingSerializer(serializers.ModelSerializer):
    """
    One reading at one test point (A, B, C, ...). The operator supplies
    ONLY the field measurement — the transit time (pulse velocity), the
    cracked + uncracked transit times (crack depth) or the observed
    surface condition (surface quality). Velocity, E.C.S and crack depth
    are computed server-side on save and are read-only here. Which
    measurement is required depends on the parent test's test_type and is
    enforced in PUNDITTestSerializer.validate.
    """
    point_label = serializers.CharField(
        max_length=10, required=False, allow_blank=True, allow_null=True, default='',
    )

    class Meta:
        model = PUNDITReading
        fields = ['id', 'point_label', 'path_length_mm', 'transit_time_us',
                  'uncracked_transit_time_us', 'surface_condition',
                  'rebound_number', 'velocity_km_s', 'ecs_mpa',
                  'strength_curve_snapshot', 'crack_depth_mm',
                  'notes', 'created_at', 'updated_at']
        read_only_fields = ['id', 'velocity_km_s', 'ecs_mpa',
                            'strength_curve_snapshot', 'crack_depth_mm',
                            'created_at', 'updated_at']
        extra_kwargs = {
            # Which measurement a point carries depends on the parent test's
            # test_type — enforced there, not here.
            'transit_time_us': {'required': False, 'allow_null': True},
        }

    def validate_transit_time_us(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError('Transit time is the field measurement — it must be a positive value.')
        return value

    def validate_uncracked_transit_time_us(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError('Uncracked transit time is a field measurement — it must be a positive value.')
        return value

    def validate_rebound_number(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError('Rebound number must be positive when provided.')
        return value

    def validate_path_length_mm(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError('Path length must be positive when provided.')
        return value


class PUNDITTestSerializer(serializers.ModelSerializer):
    project = ScopedProjectField(required=False, allow_null=True)
    test_type_display = serializers.CharField(source='get_test_type_display', read_only=True)
    quality_grade_display = serializers.CharField(source='get_quality_grade_display', read_only=True)
    transducer_type_display = serializers.CharField(source='get_transducer_type_display', read_only=True)
    file_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=SensorDataFile.objects.all(), source='files',
        required=False, write_only=True,
    )
    files = SensorDataFileSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    # Multiple test points per element (review meeting A1): a test accepts a
    # list of readings instead of a single scalar measurement. Labels are
    # auto-assigned A, B, C... in submission order when omitted.
    readings = PUNDITReadingSerializer(many=True, required=False)

    def get_estimated_compressive_strength_mpa(self, obj):
        """
        Estimated compressive strength (E.C.S) from pulse velocity through
        the project's active Nexucon Link curve (strength_curves module —
        the single f_cu path). Multi-reading tests carry the element-mean
        E.C.S persisted at write time; legacy rows without one are computed
        on read through the same active-curve path.
        """
        if obj.estimated_compressive_strength_mpa is not None:
            return obj.estimated_compressive_strength_mpa
        from apps.digital_eye.strength_curves import apply_active_curve
        strength, _snapshot = apply_active_curve(
            obj.project, obj.velocity_km_s,
            rebound_number=obj.rebound_number,
            temperature_c=obj.surface_temperature_c)
        return strength

    estimated_compressive_strength_mpa = serializers.SerializerMethodField()

    class Meta:
        model = PUNDITTest
        fields = [
            'id', 'test_reference', 'project', 'project_name', 'device', 'scan_session',
            'project_id_str', 'structural_element_id_str', 'structural_element_name',
            'structural_element_guid', 'device_model',
            'test_type', 'test_type_display', 'structural_element', 'floor',
            'weather_condition', 'concrete_age_days', 'readings',
            'transducer_frequency_khz', 'transducer_type', 'transducer_type_display',
            'test_location',
            'path_length_mm', 'pulse_time_us', 'transit_time_us',
            'crack_path_length_mm', 'crack_pulse_time_us', 'uncracked_pulse_time_us',
            'surface_temperature_c', 'surface_condition', 'rebound_number',
            'latitude', 'longitude',
            'velocity_km_s', 'pulse_velocity_ms', 'quality_grade', 'quality_grade_display',
            'concrete_quality_rating', 'estimated_compressive_strength_mpa',
            'strength_curve_snapshot',
            'ai_ci_lower_mpa', 'ai_ci_upper_mpa', 'ai_pof_pct',
            'ai_data_quality', 'ai_reasoning_traces',
            'crack_depth_mm', 'estimated_crack_depth_mm', 'waveform_samples',
            'operator', 'operator_name', 'tested_at', 'test_date', 'status', 'notes',
            'file_ids', 'files', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'test_reference', 'velocity_km_s', 'quality_grade',
                            'strength_curve_snapshot',
                            'ai_ci_lower_mpa', 'ai_ci_upper_mpa', 'ai_pof_pct',
                            'ai_data_quality', 'ai_reasoning_traces',
                            'crack_depth_mm', 'operator',
                            'created_by', 'created_at', 'updated_at']

    # ------------------------------------------------------- multi-reading
    @staticmethod
    def _assign_labels(readings_data):
        """Auto-assign point labels A, B, C, ... to any reading that arrives
        without one. Duplicate labels are rejected — each point on an element
        must be distinct."""
        used_labels = set()
        for reading in readings_data:
            explicit = (reading.get('point_label') or '').strip().upper()
            if explicit:
                if len(explicit) > 10:
                    raise serializers.ValidationError(
                        {'readings': f'Point label {explicit!r} is too long (max 10 characters).'})
                if explicit in used_labels:
                    raise serializers.ValidationError(
                        {'readings': f'Duplicate test-point label {explicit!r} — each point on an element must be distinct.'})
                used_labels.add(explicit)

        alphabet_idx = 0
        for idx, reading in enumerate(readings_data):
            label = (reading.get('point_label') or '').strip().upper()
            if not label:
                while True:
                    candidate = chr(ord('A') + alphabet_idx) if alphabet_idx < 26 else f"P{alphabet_idx + 1}"
                    alphabet_idx += 1
                    if candidate not in used_labels:
                        label = candidate
                        used_labels.add(label)
                        break
            reading['point_label'] = label
        return [r['point_label'] for r in readings_data]

    def _sync_readings(self, test, readings_data):
        """Replace the test's reading rows, then persist the element-level
        verdict: for pulse velocity the mean velocity, grade from the mean
        and mean E.C.S; for crack depth the element-mean crack depth. The
        operator never enters any of these — they are computed."""
        from apps.digital_eye.adapters import PUNDITAdapter
        from apps.digital_eye.strength_curves import apply_active_curve
        test.readings.all().delete()
        PUNDITReading.objects.bulk_create(
            [PUNDITReading(test=test, **data) for data in readings_data]
        )
        # bulk_create skips save(); recompute the derived fields explicitly.
        for reading in test.readings.all():
            reading.compute()
            reading.save(update_fields=['velocity_km_s', 'ecs_mpa',
                                        'strength_curve_snapshot', 'crack_depth_mm',
                                        'updated_at'])
        mean_v = test.element_mean_velocity_km_s()
        test.velocity_km_s = mean_v
        test.pulse_velocity_ms = round(mean_v * 1000.0, 1) if mean_v is not None else None
        # The element verdict's E.C.S flows through the project's active
        # Nexucon Link curve, with the curve provenance snapshotted (8 Sep
        # meeting — f_cu is the reason the Neural Link exists).
        mean_ecs, curve_snap = apply_active_curve(
            test.project, mean_v,
            rebound_number=test.rebound_number,
            temperature_c=test.surface_temperature_c)
        test.estimated_compressive_strength_mpa = mean_ecs
        test.strength_curve_snapshot = curve_snap
        test.quality_grade = PUNDITAdapter.grade_quality(mean_v)
        if test.test_type == 'crack_depth':
            mean_d = test.element_mean_crack_depth_mm()
            test.crack_depth_mm = mean_d
            test.estimated_crack_depth_mm = mean_d
        if readings_data:
            # Keep the scalar columns in step with the first reading so
            # legacy consumers (registry, waveform viewer) still show a real
            # measurement rather than blanks — the columns written match the
            # test type.
            first = test.readings.first()
            if test.test_type == 'crack_depth':
                test.crack_path_length_mm = first.path_length_mm
                test.crack_pulse_time_us = first.transit_time_us
                test.uncracked_pulse_time_us = first.uncracked_transit_time_us
            elif test.test_type == 'surface_quality':
                test.surface_condition = first.surface_condition
            else:
                test.path_length_mm = first.path_length_mm
                test.pulse_time_us = first.transit_time_us
                test.transit_time_us = first.transit_time_us
        test.save()

    def create(self, validated_data):
        readings_data = validated_data.pop('readings', None) or []
        test = super().create(validated_data)
        if readings_data:
            self._sync_readings(test, readings_data)
        return test

    def update(self, instance, validated_data):
        readings_data = validated_data.pop('readings', None)
        test = super().update(instance, validated_data)
        if readings_data is not None:
            self._sync_readings(test, readings_data)
        return test

    def validate(self, attrs):
        test_type = attrs.get('test_type', getattr(self.instance, 'test_type', 'pulse_velocity'))
        readings = attrs.get('readings')
        if test_type == 'pulse_velocity':
            if readings is not None:
                # Multi-reading path: each point needs its own transit time;
                # path length may be given per reading or shared from the test.
                if not readings:
                    raise serializers.ValidationError(
                        {'readings': 'Provide at least one test point (A) for pulse-velocity testing.'})
                self._assign_labels(readings)
                shared_path = attrs.get('path_length_mm') or (getattr(self.instance, 'path_length_mm', None) if self.instance else None)
                for reading in readings:
                    if reading.get('path_length_mm') is None:
                        if not shared_path:
                            raise serializers.ValidationError(
                                {'readings': 'Path length is required (per reading or on the test) — it is a physical measurement.'})
                        reading['path_length_mm'] = shared_path
                # The scalar pair is not required when readings carry the
                # measurements; the create/update paths write the first
                # reading back onto the scalar columns for legacy consumers.
            else:
                for field in ('path_length_mm', 'pulse_time_us'):
                    value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
                    if value is None and field == 'pulse_time_us':
                        value = attrs.get('transit_time_us', getattr(self.instance, 'transit_time_us', None) if self.instance else None)
                    if value is None:
                        raise serializers.ValidationError(
                            {field: 'Required for pulse-velocity testing (BS 1881-203).'})
                    if value <= 0:
                        raise serializers.ValidationError({field: 'Must be a positive measurement.'})
        if test_type == 'crack_depth':
            if readings is not None:
                # Multi-point path (A1, crack depth): each point carries its
                # own t_cracked + t_0 pair; the transducer spacing L is shared
                # from the test (or given per reading).
                if not readings:
                    raise serializers.ValidationError(
                        {'readings': 'Provide at least one test point (A) for crack-depth testing.'})
                self._assign_labels(readings)
                shared_path = attrs.get('crack_path_length_mm') or (
                    getattr(self.instance, 'crack_path_length_mm', None) if self.instance else None)
                for reading in readings:
                    if not reading.get('transit_time_us') or not reading.get('uncracked_transit_time_us'):
                        raise serializers.ValidationError(
                            {'readings': 'Each crack-depth test point needs both the cracked '
                                         'transit time t_c and the uncracked transit time t_0 — '
                                         'they are field measurements.'})
                    if reading.get('path_length_mm') is None:
                        if not shared_path:
                            raise serializers.ValidationError(
                                {'readings': 'Transducer spacing L is required (per reading or '
                                             'on the test) — it is a physical measurement.'})
                        reading['path_length_mm'] = shared_path
                # The scalar triple is not required when readings carry the
                # measurements; _sync_readings writes the first reading back
                # onto the scalar columns for legacy consumers.
            else:
                for field in ('crack_path_length_mm', 'crack_pulse_time_us', 'uncracked_pulse_time_us'):
                    value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
                    if value is None:
                        raise serializers.ValidationError(
                            {field: 'Required for crack-depth (time-difference) testing.'})
        if test_type == 'surface_quality' and readings is not None:
            # Multi-point path (A1, surface quality): each point carries the
            # observed surface condition at that point of the element.
            if not readings:
                raise serializers.ValidationError(
                    {'readings': 'Provide at least one test point (A) for surface-quality testing.'})
            self._assign_labels(readings)
            for reading in readings:
                if not (reading.get('surface_condition') or '').strip():
                    raise serializers.ValidationError(
                        {'readings': 'Each surface-quality test point needs its observed '
                                     'surface condition — it is the field record.'})
        return attrs



class PunditTestSerializer(serializers.ModelSerializer):
    class Meta:
        model = PUNDITTest
        fields = '__all__'


class GnssBenchmarkSerializer(serializers.ModelSerializer):
    class Meta:
        model = GnssBenchmark
        fields = [
            'id', 'survey', 'point_id', 'latitude', 'longitude',
            'ellipsoidal_height', 'easting', 'northing', 'utm_zone',
            'accuracy_mm', 'description', 'captured_at', 'created_at',
        ]
        read_only_fields = ['id', 'easting', 'northing', 'utm_zone', 'created_at']


class GnssBoundaryPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = GnssBoundaryPoint
        fields = [
            'id', 'survey', 'sequence', 'latitude', 'longitude', 'easting',
            'northing', 'label', 'description', 'captured_at', 'created_at',
        ]
        read_only_fields = ['id', 'easting', 'northing', 'created_at']


class GnssSurveySerializer(serializers.ModelSerializer):
    project = ScopedProjectField()
    benchmarks = GnssBenchmarkSerializer(many=True, read_only=True)
    boundary_points = GnssBoundaryPointSerializer(many=True, read_only=True)
    file_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=SensorDataFile.objects.all(), source='files',
        required=False, write_only=True,
    )
    files = SensorDataFileSerializer(many=True, read_only=True)
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = GnssSurvey
        fields = [
            'id', 'survey_reference', 'project', 'device', 'title', 'method',
            'method_display', 'projection', 'fix_quality', 'variance_summary',
            'latitude', 'longitude', 'status', 'status_display', 'operator',
            'operator_name', 'started_at', 'completed_at', 'notes', 'file_ids',
            'files', 'benchmarks', 'boundary_points', 'created_by', 'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'survey_reference', 'variance_summary', 'operator',
                            'created_by', 'created_at', 'updated_at']


class BIMElementMappingSerializer(serializers.ModelSerializer):
    project = ScopedProjectField()
    source_display = serializers.CharField(source='get_source_display', read_only=True)

    class Meta:
        model = BIMElementMapping
        fields = [
            'id', 'project', 'trimble_project', 'bim_guid', 'element_id',
            'element_name', 'element_type', 'level', 'discipline', 'coordinates',
            'properties', 'source', 'source_display', 'created_by', 'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class TrimbleProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrimbleProject
        fields = [
            'id', 'connection', 'external_id', 'name', 'linked_project',
            'raw_metadata', 'last_synced_at', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'connection', 'external_id', 'name', 'raw_metadata',
                            'last_synced_at', 'created_at', 'updated_at']


class TrimbleConnectionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = TrimbleConnection
        fields = [
            'id', 'name', 'status', 'status_display', 'scope',
            'trimble_user_id', 'trimble_user_name', 'last_health_check_at',
            'last_health_status', 'last_error', 'created_by', 'created_at', 'updated_at',
            'project', 'project_id_str', 'project_name', 'trimble_project_id',
            'trimble_project_name', 'region', 'last_sync_at', 'synced_models_count',
            'synced_elements_count', 'bcf_topics_count', 'webhook_active',
        ]
        read_only_fields = ['created_at', 'updated_at']


class LiveStreamSerializer(serializers.ModelSerializer):
    project = ScopedProjectField()
    mapped_element_id = serializers.PrimaryKeyRelatedField(
        queryset=BIMElementMapping.objects.all(), source='mapped_element',
        required=False, allow_null=True,
    )
    mapped_element_label = serializers.SerializerMethodField()

    class Meta:
        model = LiveStream
        fields = [
            'id', 'project', 'name', 'stream_url', 'stream_provider',
            'stream_token', 'mapped_element_id', 'mapped_element_label',
            'mapped_coordinates', 'status', 'last_checked_at', 'last_error',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'mapped_coordinates', 'status', 'last_checked_at',
                            'last_error', 'created_by', 'created_at', 'updated_at']

    def get_mapped_element_label(self, obj):
        if obj.mapped_element:
            return (obj.mapped_element.element_id or obj.mapped_element.bim_guid
                    or obj.mapped_element.element_name)
        return None

    def validate(self, attrs):
        element = attrs.get('mapped_element') or (
            self.instance.mapped_element if self.instance else None)
        if element and not (attrs.get('mapped_coordinates') or
                            (self.instance and self.instance.mapped_coordinates)):
            attrs['mapped_coordinates'] = element.coordinates
        return attrs


class BIMStructuralElementSerializer(serializers.ModelSerializer):
    class Meta:
        model = BIMStructuralElement
        fields = '__all__'


class GPRScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = GPRScan
        fields = '__all__'


class DigitalEyeFindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = DigitalEyeFinding
        fields = '__all__'


class AIAnalysisRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIAnalysisRecord
        fields = '__all__'


class ProcessingQueueJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingQueueJob
        fields = '__all__'


class EvidenceSpatialPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvidenceSpatialPoint
        fields = '__all__'


class DeviceReportRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceReportRecord
        fields = '__all__'


class StrengthCurveSerializer(serializers.ModelSerializer):
    """
    Nexucon Link calibration curve (8 Sep meeting). Formula parameters are
    stored in the m/s velocity domain; the engine converts once on apply.
    """
    project = ScopedProjectField(required=False, allow_null=True)
    curve_type_display = serializers.CharField(
        source='get_curve_type_display', read_only=True)
    formula_display = serializers.SerializerMethodField()

    class Meta:
        model = StrengthCurve
        fields = [
            'id', 'name', 'curve_type', 'curve_type_display', 'standard',
            'project', 'velocity_unit', 'strength_unit', 'formula_params',
            'formula_display', 'data_points', 'valid_range_min_ms',
            'valid_range_max_ms', 'r2_score', 'standard_error', 'aic',
            'is_default', 'provenance', 'version', 'created_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'velocity_unit', 'strength_unit',
                            'r2_score', 'standard_error', 'aic', 'version',
                            'created_by', 'created_at', 'updated_at']

    def get_formula_display(self, obj):
        from .strength_curves import formula_display
        try:
            return formula_display(obj.curve_type, obj.formula_params or {})
        except Exception:
            return None

    def validate_curve_type(self, value):
        from .strength_curves import CURVE_TYPES
        if value not in CURVE_TYPES:
            raise serializers.ValidationError(
                f"Unknown curve type. Must be one of: {', '.join(CURVE_TYPES)}.")
        return value

    def validate(self, attrs):
        curve_type = attrs.get('curve_type') or (
            self.instance.curve_type if self.instance else None)
        params = attrs.get('formula_params') or (
            self.instance.formula_params if self.instance else None)
        if not params:
            raise serializers.ValidationError(
                {'formula_params': 'Curve parameters are required.'})
        # The engine is the single validator of every parameter set: it
        # rejects the wrong keys per type before anything is persisted.
        from .strength_curves import validate_curve_params
        try:
            validate_curve_params(curve_type, params)
        except ValueError as exc:
            raise serializers.ValidationError({'formula_params': str(exc)})

        # A curve may only be marked default by the platform (Directors
        # approve one fallback); anything else stays a project curve.
        if attrs.get('is_default'):
            attrs['is_default'] = False
        return attrs


class ProjectCurveSettingSerializer(serializers.ModelSerializer):
    active_curve = StrengthCurveSerializer(read_only=True)
    project = ScopedProjectField()

    class Meta:
        model = ProjectCurveSetting
        fields = ['id', 'project', 'active_curve', 'updated_by', 'updated_at']
        read_only_fields = ['id', 'updated_by', 'updated_at']


class CoreSampleSerializer(serializers.ModelSerializer):
    """
    A laboratory core result (ground-truth layer, path-to-95% Layer 3).
    ``calibration_pair`` is the real (v, f) pair the core contributes —
    present only when BOTH halves exist (lab result + a linked UPV test
    with a measured velocity). Never synthesized.
    """
    project = ScopedProjectField()
    pundit_test = serializers.SlugRelatedField(
        slug_field='id', queryset=PUNDITTest.objects.all(),
        required=False, allow_null=True)
    calibration_pair = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CoreSample
        fields = [
            'id', 'project', 'pundit_test', 'structural_element',
            'test_location', 'core_diameter_mm', 'core_length_mm',
            'lab_strength_mpa', 'lab_report_ref', 'sampled_at', 'notes',
            'calibration_pair', 'recorded_by', 'recorded_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'recorded_by', 'created_at', 'updated_at']

    def get_calibration_pair(self, obj):
        return obj.calibration_pair()

    def get_recorded_by_name(self, obj):
        if obj.recorded_by:
            return obj.recorded_by.get_full_name() or obj.recorded_by.email
        return None

    def validate(self, attrs):
        # The linked UPV test must belong to the same project — a pair
        # across projects would be fabricated ground truth.
        pundit_test = attrs.get('pundit_test') or (
            self.instance.pundit_test if self.instance else None)
        project = attrs.get('project') or (
            self.instance.project if self.instance else None)
        if pundit_test is not None and project is not None \
                and pundit_test.project_id != project.pk:
            raise serializers.ValidationError(
                {'pundit_test': 'The linked PUNDIT test must belong to the '
                                'same project as the core sample.'})
        for field in ('core_diameter_mm', 'core_length_mm'):
            value = attrs.get(field)
            if value is not None and value <= 0:
                raise serializers.ValidationError(
                    {field: 'Must be positive.'})
        strength = attrs.get('lab_strength_mpa')
        if strength is not None and strength <= 0:
            raise serializers.ValidationError(
                {'lab_strength_mpa': 'Must be positive.'})
        return attrs
