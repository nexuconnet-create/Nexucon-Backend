from rest_framework import serializers
from .models import (
    ScanSession, ScanMetadata, Defect, ThermalAnomaly,
    BIMAlignmentResult, ProcessingTask, ScanFile, ProgressValidationResult,
    ScanPlan, ComplianceCheck, Scanner, GnssTelemetry, ComplianceCertificate,
    StopWorkFlag
)
from apps.storage.services import StorageService
from apps.storage.cloudinary_service import CloudinaryService

class StartSessionSerializer(serializers.ModelSerializer):
    """Serializer for creating a scan session. Includes optional project_id to link the session to a Project."""
    project_id = serializers.UUIDField(required=False, allow_null=True, help_text="UUID of the project to associate with the scan session.")

    class Meta:
        model = ScanSession
        fields = ["project_id", "scanner_id", "timestamp", "sensors_used", "expected_size_mb"]

class LocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    elevation = serializers.FloatField(required=False, allow_null=True)

class ScanMetadataSerializer(serializers.ModelSerializer):
    """Validate scan metadata and flatten nested location fields into model columns."""
    location = LocationSerializer(required=False)

    class Meta:
        model = ScanMetadata
        fields = ["location", "latitude", "longitude", "operator_id", "notes"]

    def create(self, validated_data):
        location_data = validated_data.pop("location", {})
        if location_data:
            validated_data["latitude"] = location_data.get("latitude")
            validated_data["longitude"] = location_data.get("longitude")
            validated_data["elevation"] = location_data.get("elevation", 0.0)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        location_data = validated_data.pop("location", {})
        if location_data:
            instance.latitude = location_data.get("latitude", instance.latitude)
            instance.longitude = location_data.get("longitude", instance.longitude)
            instance.elevation = location_data.get("elevation", instance.elevation or 0.0)

        if "latitude" in validated_data:
            instance.latitude = validated_data["latitude"]
        if "longitude" in validated_data:
            instance.longitude = validated_data["longitude"]

        instance.operator_id = validated_data.get("operator_id", instance.operator_id)
        instance.notes = validated_data.get("notes", instance.notes)
        instance.save()
        return instance

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        lat = instance.latitude
        lon = instance.longitude
        ret["latitude"] = lat
        ret["longitude"] = lon
        if lat is not None and lon is not None:
            ret["location"] = {"latitude": lat, "longitude": lon}
        else:
            ret["location"] = None
        return ret

class SessionResponseSerializer(serializers.ModelSerializer):
    """Response serializer for a newly created scan session, providing a pre‑signed upload URL."""
    upload_url = serializers.SerializerMethodField()
    name = serializers.CharField(source='scanner_id', read_only=True)  # Frontend expects name or uses Unnamed
    metadata = serializers.SerializerMethodField()
    overall_ai_confidence = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()
    operator = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    class Meta:
        model = ScanSession
        fields = ["id", "project", "project_name", "name", "scanner_id", "sensors_used", "status", "expected_size_mb", "timestamp", "created_at", "updated_at", "upload_url", "metadata", "overall_ai_confidence", "duration", "operator"]

    def get_project_name(self, obj):
        """Project display name so the frontend never has to show a UUID."""
        return obj.project.name if getattr(obj, 'project', None) else None

    def get_duration(self, obj) -> str:
        """
        Elapsed survey time, derived from the session window. Sessions that are
        still open have no meaningful end time yet.
        """
        if obj.status in ('initialized', 'uploading'):
            return 'In progress'
        if not obj.created_at or not obj.updated_at:
            return None
        seconds = (obj.updated_at - obj.created_at).total_seconds()
        if seconds <= 0:
            return None
        minutes = int(round(seconds / 60))
        if minutes < 1:
            return '<1 min'
        if minutes < 60:
            return f'{minutes} min'
        return f'{minutes // 60}h {minutes % 60}m'

    def get_operator(self, obj):
        """Operator recorded on the session metadata, if one was submitted."""
        metadata = getattr(obj, 'metadata', None)
        if metadata and (metadata.operator_id or '').strip():
            return metadata.operator_id
        return None

    def get_overall_ai_confidence(self, obj):
        report = obj.reports.order_by('-generated_at').first()
        if report and report.overall_ai_confidence is not None:
            return report.overall_ai_confidence
        return None

    def get_metadata(self, obj):
        if hasattr(obj, 'metadata') and obj.metadata:
            return ScanMetadataSerializer(obj.metadata).data
        return None

    def get_upload_url(self, obj) -> str:
        from apps.storage.services import StorageService
        try:
            return StorageService().generate_presigned_upload_url(str(obj.id), "raw_scan")
        except ValueError:
            return None

class LocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    elevation = serializers.FloatField(required=False, allow_null=True)

class ScanUploadRequestSerializer(serializers.Serializer):
    """Serializer for uploading scan files in chunks."""
    FILE_TYPE_CHOICES = ["lidar", "rgb", "thermal", "gps", "gaussian_splat"]
    session_id = serializers.UUIDField()
    file_type = serializers.ChoiceField(choices=FILE_TYPE_CHOICES)
    file = serializers.FileField()
    chunk_number = serializers.IntegerField(required=False, min_value=1)
    total_chunks = serializers.IntegerField(required=False, min_value=1)

class ProcessingTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingTask
        fields = "__all__"

class DefectSerializer(serializers.ModelSerializer):
    image = serializers.FileField(write_only=True, required=False, help_text="Upload an image file to automatically generate image_url.")
    thermal_image = serializers.FileField(write_only=True, required=False, help_text="Upload a thermal image file to automatically generate thermal_image_url.")

    class Meta:
        model = Defect
        fields = "__all__"
        read_only_fields = ["session", "image_url", "thermal_image_url"]

    def create(self, validated_data):
        image = validated_data.pop("image", None)
        thermal_image = validated_data.pop("thermal_image", None)
        if image:
            from apps.storage.cloudinary_service import CloudinaryService
            validated_data["image_url"] = CloudinaryService.upload_file(image, folder="defects")
        if thermal_image:
            from apps.storage.cloudinary_service import CloudinaryService
            validated_data["thermal_image_url"] = CloudinaryService.upload_file(thermal_image, folder="defects/thermal")
        return super().create(validated_data)

class ThermalAnomalySerializer(serializers.ModelSerializer):
    image = serializers.FileField(write_only=True, required=False, help_text="Upload an image file to automatically generate image_url.")

    class Meta:
        model = ThermalAnomaly
        fields = "__all__"
        read_only_fields = ["session", "image_url"]

    def create(self, validated_data):
        image = validated_data.pop("image", None)
        if image:
            from apps.storage.cloudinary_service import CloudinaryService
            validated_data["image_url"] = CloudinaryService.upload_file(image, folder="thermal_anomalies")
        return super().create(validated_data)

class BIMAlignmentResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = BIMAlignmentResult
        fields = "__all__"

class ScanFileSerializer(serializers.ModelSerializer):
    content_url = serializers.SerializerMethodField()

    class Meta:
        model = ScanFile
        fields = ['id', 'session', 'file_type', 'file_url', 'content_url', 'file_name', 'file_size_bytes', 'created_at']
        read_only_fields = ['session']

    def get_content_url(self, obj):
        return f"/api/v1/scans/{obj.session_id}/files/{obj.id}/content/"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The stored URL is signed at upload time and expires after ~1 hour;
        # serve a freshly signed URL so links keep working.
        data['file_url'] = instance.fresh_url()
        return data

class ProgressValidationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProgressValidationResult
        fields = ['id', 'session', 'progress_score', 'covered_area_sqm', 'volume_metrics', 'created_at', 'updated_at']
        read_only_fields = ['session']

class ComplianceCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComplianceCheck
        fields = "__all__"

class ScanPlanSerializer(serializers.ModelSerializer):
    scanner_device_id = serializers.CharField(source='scanner.device_id', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = ScanPlan
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def update(self, instance, validated_data):
        lat = validated_data.get('latitude', instance.latitude)
        lng = validated_data.get('longitude', instance.longitude)
        project = validated_data.get('project', instance.project)

        if (lat is None or lng is None) and project:
            validated_data['latitude'] = project.latitude
            validated_data['longitude'] = project.longitude

        return super().update(instance, validated_data)

    def create(self, validated_data):
        lat = validated_data.get('latitude')
        lng = validated_data.get('longitude')
        project = validated_data.get('project')

        if (lat is None or lng is None) and project:
            validated_data['latitude'] = project.latitude
            validated_data['longitude'] = project.longitude

        return super().create(validated_data)
class ScannerSerializer(serializers.ModelSerializer):
    """Device registry entry, plus live-derived survey activity."""
    session_count = serializers.SerializerMethodField()
    latest_session_status = serializers.SerializerMethodField()

    class Meta:
        model = Scanner
        fields = [
            'id', 'device_id', 'model', 'status', 'battery_level',
            'firmware_version', 'latitude', 'longitude', 'last_seen',
            'is_active', 'session_count', 'latest_session_status',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_session_count(self, obj) -> int:
        return ScanSession.objects.filter(scanner_id=obj.device_id).count()

    def get_latest_session_status(self, obj):
        latest = (
            ScanSession.objects.filter(scanner_id=obj.device_id)
            .order_by('-created_at')
            .values_list('status', flat=True)
            .first()
        )
        return latest


class ScannerHeartbeatSerializer(serializers.Serializer):
    """Payload a device sends to report that it is alive and its own telemetry."""
    battery_level = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    status = serializers.ChoiceField(choices=Scanner.STATUS_CHOICES, required=False)
    firmware_version = serializers.CharField(required=False, allow_blank=True, max_length=50)
    latitude = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)


class GnssTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = GnssTelemetry
        fields = [
            'id', 'session', 'scanner', 'fix_rate', 'fix_type', 'satellites',
            'horizontal_accuracy_m', 'recorded_at', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class ComplianceCertificateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComplianceCertificate
        fields = [
            'id', 'session', 'certificate_number', 'status', 'total_checks',
            'passed_checks', 'failed_checks', 'issued_by', 'notes', 'issued_at',
        ]
        read_only_fields = [
            'id', 'certificate_number', 'total_checks', 'passed_checks',
            'failed_checks', 'issued_at',
        ]


class StopWorkFlagSerializer(serializers.ModelSerializer):
    session_id = serializers.UUIDField(write_only=True)
    check_id = serializers.CharField(write_only=True, required=False, allow_null=True, allow_blank=True)
    flagged_by_name = serializers.SerializerMethodField()

    class Meta:
        model = StopWorkFlag
        fields = [
            'id', 'session', 'session_id', 'compliance_check', 'check_id', 'reason',
            'flagged_by', 'flagged_by_name', 'status', 'created_at', 'lifted_at',
        ]
        read_only_fields = ['id', 'session', 'compliance_check', 'flagged_by', 'status', 'created_at', 'lifted_at']

    def get_flagged_by_name(self, obj):
        return obj.flagged_by or 'Compliance dashboard'

    def validate(self, attrs):
        check_id = attrs.pop('check_id', None)
        if check_id:
            check = ComplianceCheck.objects.filter(id=check_id).first()
            if check is None:
                raise serializers.ValidationError({'check_id': 'Unknown compliance check.'})
            attrs['compliance_check'] = check
        return attrs

