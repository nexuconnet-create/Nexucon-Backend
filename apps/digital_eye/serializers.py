"""
Digital Eye API serializers (implementation plan §5A, Weeks 1–3).
"""
from rest_framework import serializers

from common.permissions import scoped_projects
from .models import (
    BIMElementMapping, FieldDevice, GPRAnomaly, GPRSurvey, GnssBenchmark,
    GnssBoundaryPoint, GnssSurvey, LiveStream, PUNDITTest, SensorDataFile,
    TrimbleConnection, TrimbleProject,
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
            'sha256_checksum', 'description', 'uploaded_by', 'uploaded_by_name',
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
        # Rebar detections should carry a cover measurement when possible.
        anomaly_type = attrs.get('anomaly_type')
        if anomaly_type == 'rebar' and attrs.get('rebar_cover_mm') is None:
            # Allowed (cover may genuinely be unmeasured) but flagged in the
            # description if not mentioned.
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


class PUNDITTestSerializer(serializers.ModelSerializer):
    project = ScopedProjectField()
    file_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=SensorDataFile.objects.all(), source='files',
        required=False, write_only=True,
    )
    files = SensorDataFileSerializer(many=True, read_only=True)
    test_type_display = serializers.CharField(source='get_test_type_display', read_only=True)
    quality_grade_display = serializers.CharField(source='get_quality_grade_display', read_only=True)
    transducer_type_display = serializers.CharField(source='get_transducer_type_display', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    def get_estimated_compressive_strength_mpa(self, obj):
        """
        Estimated compressive strength (E.C.S) from pulse velocity, using the
        documented calibration curve that the official NDT report renders
        (apps.reports.ndt_reports) — single source of truth. Returns None when
        the velocity is missing or outside the curve's valid 2.0–5.0 km/s
        range (never extrapolated).
        """
        from apps.reports.ndt_reports import estimated_compressive_strength
        return estimated_compressive_strength(obj.velocity_km_s)

    estimated_compressive_strength_mpa = serializers.SerializerMethodField()

    class Meta:
        model = PUNDITTest
        fields = [
            'id', 'test_reference', 'project', 'project_name', 'device', 'scan_session',
            'test_type', 'test_type_display', 'structural_element',
            'transducer_frequency_khz', 'transducer_type', 'transducer_type_display',
            'test_location',
            'path_length_mm', 'pulse_time_us',
            'crack_path_length_mm', 'crack_pulse_time_us', 'uncracked_pulse_time_us',
            'surface_temperature_c', 'surface_condition', 'latitude', 'longitude',
            'velocity_km_s', 'quality_grade', 'quality_grade_display',
            'estimated_compressive_strength_mpa',
            'crack_depth_mm', 'operator', 'operator_name', 'tested_at', 'notes',
            'file_ids', 'files', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'test_reference', 'velocity_km_s', 'quality_grade',
                            'crack_depth_mm', 'operator', 'created_by',
                            'created_at', 'updated_at']

    def validate(self, attrs):
        test_type = attrs.get('test_type', getattr(self.instance, 'test_type', 'pulse_velocity'))
        if test_type == 'pulse_velocity':
            for field in ('path_length_mm', 'pulse_time_us'):
                value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
                if value is None:
                    raise serializers.ValidationError(
                        {field: 'Required for pulse-velocity testing (BS 1881-203).'})
                if value <= 0:
                    raise serializers.ValidationError({field: 'Must be a positive measurement.'})
        if test_type == 'crack_depth':
            for field in ('crack_path_length_mm', 'crack_pulse_time_us', 'uncracked_pulse_time_us'):
                value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
                if value is None:
                    raise serializers.ValidationError(
                        {field: 'Required for crack-depth (time-difference) testing.'})
        return attrs


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
        ]
        # authorization_code / pkce_verifier / tokens are never serialized out.


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
        # When a stream is anchored to a BIM element, adopt its coordinates.
        element = attrs.get('mapped_element') or (
            self.instance.mapped_element if self.instance else None)
        if element and not (attrs.get('mapped_coordinates') or
                            (self.instance and self.instance.mapped_coordinates)):
            attrs['mapped_coordinates'] = element.coordinates
        return attrs
