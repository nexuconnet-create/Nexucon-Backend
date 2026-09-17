"""
Telemetry API serializers.

Read shapes for sessions and packets, plus the two request bodies that are not
a model: the packet append and the session start.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import TelemetryPacket, TelemetrySession


class FieldDeviceProjectionSerializer(serializers.Serializer):
    """A read-only projection over digital_eye.FieldDevice.

    Deliberately not a ModelSerializer and deliberately not a second device
    registry. The spec's "device registry" screen needs a device list; the
    platform already has one, with real registration, calibration and
    assignment behind it. This projects that record into the fields a
    telemetry client needs and adds nothing that could drift from it.
    """

    id = serializers.UUIDField(read_only=True)
    device_reference = serializers.CharField(read_only=True)
    device_id = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    device_type = serializers.CharField(read_only=True)
    device_type_display = serializers.CharField(
        source='get_device_type_display', read_only=True)
    model = serializers.CharField(read_only=True)
    manufacturer = serializers.CharField(read_only=True)
    firmware_version = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(
        source='get_status_display', read_only=True)
    assigned_project = serializers.UUIDField(read_only=True, allow_null=True)
    battery_level = serializers.IntegerField(read_only=True, allow_null=True)
    latitude = serializers.FloatField(read_only=True, allow_null=True)
    longitude = serializers.FloatField(read_only=True, allow_null=True)
    last_seen = serializers.DateTimeField(read_only=True, allow_null=True)
    calibration_date = serializers.DateField(read_only=True, allow_null=True)
    calibration_certificate_url = serializers.CharField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    # Telemetry's own view of the device: is it streaming right now?
    open_session_reference = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_open_session_reference(self, obj):
        """The reference of this device's open session, or null.

        Null means the device is not streaming — it does not mean the platform
        could not find out. A device may legitimately be idle for days.
        """
        session = obj.telemetry_sessions.filter(
            status=TelemetrySession.STATUS_OPEN).first()
        return session.session_reference if session else None


class TelemetryPacketSerializer(serializers.ModelSerializer):
    class Meta:
        model = TelemetryPacket
        fields = ['id', 'sequence', 'payload', 'previous_hash', 'chain_hash',
                  'recorded_at', 'received_at']
        read_only_fields = fields


class TelemetrySessionSerializer(serializers.ModelSerializer):
    device_id = serializers.CharField(source='device.device_id', read_only=True)
    device_reference = serializers.CharField(
        source='device.device_reference', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    data_type_display = serializers.CharField(
        source='get_data_type_display', read_only=True)

    class Meta:
        model = TelemetrySession
        fields = [
            'id', 'session_reference', 'device', 'device_id', 'device_reference',
            'project', 'project_name', 'operator', 'operator_name',
            'data_type', 'data_type_display', 'status', 'sync_status',
            'session_start', 'session_end', 'packet_count', 'session_config',
            'sha256_hash', 'sync_error', 'promoted_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields


class SessionStatusSerializer(serializers.ModelSerializer):
    """`GET session/<id>/status/` — the polling shape the PWA uses.

    ``session_end`` is null while the session is open: nothing ended it, so
    nothing may claim it did. ``data_payload`` is absent until promotion
    because it is the normalised envelope built at /end, not a placeholder
    assembled at start.
    """

    device_id = serializers.CharField(source='device.device_id', read_only=True)
    chain_valid = serializers.SerializerMethodField()
    promoted = serializers.SerializerMethodField()

    class Meta:
        model = TelemetrySession
        fields = [
            'id', 'session_reference', 'device_id', 'project', 'data_type',
            'status', 'sync_status', 'packet_count', 'session_start',
            'session_end', 'sync_error', 'sha256_hash', 'promoted_at',
            'chain_valid', 'promoted', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_chain_valid(self, obj):
        """Never True for a session with no packets — an empty chain proves
        nothing, and reporting it as intact would be a claim about content
        that does not exist."""
        if not obj.packet_count:
            return None
        return obj.verify_chain()

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_promoted(self, obj):
        payload = obj.data_payload or {}
        if not payload:
            return None
        return {
            'data_type': payload.get('data_type'),
            'packets': len(payload.get('packets') or []),
            'sha256_hash': obj.sha256_hash,
        }


class StartSessionRequestSerializer(serializers.Serializer):
    """Body of `POST telemetry/session/start/`."""

    device = serializers.UUIDField()
    data_type = serializers.ChoiceField(
        choices=[c[0] for c in TelemetrySession.DATA_TYPE_CHOICES])
    project = serializers.UUIDField(required=False, allow_null=True)
    session_config = serializers.JSONField(required=False, default=dict)
    session_start = serializers.DateTimeField(required=False, allow_null=True)

    def validate_session_config(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'session_config must be an object of session-wide inputs.')
        return value


class AppendPacketRequestSerializer(serializers.Serializer):
    """Body of `POST telemetry/session/<id>/data/`.

    ``payload`` is the measurement as the device sent it and is stored
    unmodified — it is not validated here, because the registry serializer
    that owns that shape is the one that decides whether it is valid, and it
    runs at /end. Validating loosely here as well would create a second,
    weaker opinion about the same row.
    """

    payload = serializers.JSONField()
    sequence = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    recorded_at = serializers.DateTimeField(required=False, allow_null=True)


class PacketAckSerializer(serializers.Serializer):
    """Response to an appended packet — the receipt the device chains on.

    `chain_hash` is what the next packet must name as its predecessor, so a
    client that loses this response must re-read `session/<id>/status/` rather
    than guess a sequence number.
    """

    session_reference = serializers.CharField()
    sequence = serializers.IntegerField()
    chain_hash = serializers.CharField()
    packet_count = serializers.IntegerField()


class EndSessionResponseSerializer(serializers.Serializer):
    """Response to `POST session/<id>/end/`.

    `promoted` is null when nothing reached the statutory registry. It is never
    an empty object: an empty envelope would read as "promoted, nothing found",
    which is a different claim from "not promoted".
    """

    session_reference = serializers.CharField()
    status = serializers.CharField()
    sync_status = serializers.CharField()
    packet_count = serializers.IntegerField()
    sha256_hash = serializers.CharField(allow_null=True)
    promoted_at = serializers.DateTimeField(allow_null=True)
    promoted = serializers.DictField(allow_null=True)
