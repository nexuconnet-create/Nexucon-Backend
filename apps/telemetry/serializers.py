"""
Telemetry API serializers.

Read shapes for sessions and packets, plus the request bodies that are not a
model: the packet append, the session start, and a device credential issue.
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
    transport_display = serializers.SerializerMethodField()

    class Meta:
        model = TelemetrySession
        fields = [
            'id', 'session_reference', 'device', 'device_id', 'device_reference',
            'project', 'project_name', 'operator', 'operator_name',
            'data_type', 'data_type_display', 'status', 'sync_status',
            'transport', 'transport_display',
            'session_start', 'session_end', 'packet_count', 'session_config',
            'source_file_name', 'source_file_sha256',
            'sha256_hash', 'sync_error', 'promoted_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_transport_display(self, obj):
        """How the capture arrived, or null when that was not recorded.

        Null, not a fallback label: a session captured before the platform
        asked the question has no transport, and naming one would be a claim
        about a measurement's provenance that nobody made.
        """
        if not obj.transport:
            return None
        return obj.get_transport_display()


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
            'status', 'sync_status', 'transport', 'packet_count',
            'source_file_name', 'source_file_sha256',
            'session_start', 'session_end', 'sync_error', 'sha256_hash',
            'promoted_at', 'chain_valid', 'promoted', 'created_at', 'updated_at',
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


#: Transports a *client* may declare. `FILE` and `MANUAL` are absent on
#: purpose: the server determines those from which endpoint was called, and a
#: client that could claim them could file a typed-in number as an instrument
#: export.
DECLARABLE_TRANSPORTS = (
    TelemetrySession.TRANSPORT_BLE,
    TelemetrySession.TRANSPORT_WIFI,
    TelemetrySession.TRANSPORT_CLOUD,
)


class StartSessionRequestSerializer(serializers.Serializer):
    """Body of `POST telemetry/session/start/`."""

    device = serializers.UUIDField()
    data_type = serializers.ChoiceField(
        choices=[c[0] for c in TelemetrySession.DATA_TYPE_CHOICES])
    project = serializers.UUIDField(required=False, allow_null=True)
    session_config = serializers.JSONField(required=False, default=dict)
    session_start = serializers.DateTimeField(required=False, allow_null=True)
    transport = serializers.ChoiceField(
        choices=DECLARABLE_TRANSPORTS, required=False, allow_blank=True,
        default='',
        help_text='How the client reached the platform. Omit rather than guess '
                  '— an unreported transport is recorded as not recorded.',
    )

    def validate_session_config(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'session_config must be an object of session-wide inputs.')
        return value


class ImportExportRequestSerializer(serializers.Serializer):
    """Body of `POST telemetry/session/from-file/` (multipart).

    The context fields are not a convenience duplicate of the file's columns —
    they are where a bare instrument export gets the element and test type it
    cannot know. Whichever side has a value wins; neither is invented.
    """

    device = serializers.UUIDField()
    project = serializers.UUIDField(required=False, allow_null=True)
    # `allow_empty_file` so the refusal comes from the service, which names the
    # file and says to export it again. DRF's own "submitted file is empty" is
    # true but leaves the operator without the next step, and this way an empty
    # upload reads the same whether it arrived over HTTP or not.
    file = serializers.FileField(allow_empty_file=True)
    data_type = serializers.ChoiceField(
        choices=[c[0] for c in TelemetrySession.DATA_TYPE_CHOICES],
        required=False, default='pundit',
        help_text='Only pundit has a file contract that describes a whole '
                  'capture; the others are refused with the reason',
    )
    test_type = serializers.CharField(
        required=False, allow_blank=True, default='')
    visual_observation = serializers.CharField(
        required=False, allow_blank=True, default='')
    attendance_log = serializers.CharField(
        required=False, allow_blank=True, default='')
    structural_element = serializers.CharField(
        required=False, allow_blank=True, default='')
    floor = serializers.CharField(required=False, allow_blank=True, default='')
    test_location = serializers.CharField(
        required=False, allow_blank=True, default='')
    weather_condition = serializers.CharField(
        required=False, allow_blank=True, default='')
    transducer_type = serializers.CharField(
        required=False, allow_blank=True, default='')
    transducer_frequency_khz = serializers.IntegerField(
        required=False, allow_null=True, min_value=1)
    injection_strategy = serializers.ChoiceField(
        choices=['append', 'override', 'new_folder'],
        required=False, default='append'
    )
    latitude = serializers.FloatField(
        required=False, allow_null=True,
        help_text='GPS latitude of the test location, decimal degrees.')
    longitude = serializers.FloatField(
        required=False, allow_null=True,
        help_text='GPS longitude of the test location, decimal degrees.')
    photos = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        allow_empty=True,
    )


class DeviceTokenSerializer(serializers.Serializer):
    """A device credential as it is listed — never the secret itself."""

    id = serializers.UUIDField(read_only=True)
    device = serializers.UUIDField(source='device_id', read_only=True)
    device_id = serializers.CharField(source='device.device_id', read_only=True)
    device_reference = serializers.CharField(
        source='device.device_reference', read_only=True)
    label = serializers.CharField(read_only=True)
    key_prefix = serializers.CharField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    issued_at = serializers.DateTimeField(read_only=True)
    last_used_at = serializers.DateTimeField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    revoked_at = serializers.DateTimeField(read_only=True, allow_null=True)


class IssuedDeviceTokenSerializer(DeviceTokenSerializer):
    """The one response that carries the plaintext, and only ever once.

    ``token`` is the credential itself. It is not stored — the row keeps a
    digest — so a client that loses this response cannot recover it and must
    have a new one issued.
    """

    token = serializers.CharField(read_only=True)


class IssueDeviceTokenRequestSerializer(serializers.Serializer):
    device = serializers.UUIDField()
    label = serializers.CharField(max_length=255)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


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
