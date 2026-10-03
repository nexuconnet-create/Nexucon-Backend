"""
Telemetry API views (Inspector PWA — Part 3, dual-path ingestion).

Routes:
  POST telemetry/session/start/          open a capture
  POST telemetry/session/from-file/      import an instrument export as a session
  POST telemetry/session/<uuid>/data/    append a packet
  POST telemetry/session/<uuid>/end/     close and promote into the registry
  GET  telemetry/session/<uuid>/status/  poll capture + promotion state
  GET  telemetry/sessions/               list the caller's sessions
  GET  telemetry/devices/                read-only device projection
  GET  telemetry/device-tokens/          list credentials for the caller's devices
  POST telemetry/device-tokens/          issue a credential for one device
  POST telemetry/device-tokens/<uuid>/revoke/  revoke it

Three transports reach the same session pipeline, and the endpoints differ
because the arrival does: a device streaming over Wi-Fi or a gateway pushing
over the cloud authenticates with a device credential and calls `start` then
`data`; an instrument with no radio exports a file and the inspector uploads
it at `from-file/`. All three produce an ordinary session and are promoted by
the same all-or-nothing `/end/`.
"""
import logging

from drf_spectacular.utils import extend_schema
from django.db.models import Q
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.authentication import (
    ApiKeyAuthentication, CookieJWTAuthentication,
)
from apps.audit.models import AuditEvent
from apps.digital_eye.models import FieldDevice
from common.permissions import scoped_projects, user_role_name

from .authentication import DeviceTokenAuthentication
from .export_import import SessionFromFileService
from .models import DeviceToken, TelemetrySession
from .serializers import (
    AppendPacketRequestSerializer,
    DeviceTokenSerializer,
    EndSessionResponseSerializer,
    FieldDeviceProjectionSerializer,
    ImportExportRequestSerializer,
    IssuedDeviceTokenSerializer,
    IssueDeviceTokenRequestSerializer,
    PacketAckSerializer,
    SessionStatusSerializer,
    StartSessionRequestSerializer,
    TelemetrySessionSerializer,
)
from .services import DeviceTokenService, TelemetryError, TelemetryService

logger = logging.getLogger(__name__)

#: Telemetry is the only place a device credential is accepted. Declared
#: per-view rather than added to `DEFAULT_AUTHENTICATION_CLASSES`, so a device
#: secret presented to any other endpoint authenticates as nobody and is
#: refused — a credential for pushing readings should not open a projects list.
TELEMETRY_AUTH = (
    CookieJWTAuthentication,
    ApiKeyAuthentication,
    DeviceTokenAuthentication,
)


def _record_audit(user, action, obj_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type='TelemetrySession',
            resource_id=str(obj_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


def _scoped_sessions(request):
    """Sessions the caller may act on.

    Scoped by project, like every other field record. A session whose project
    is outside the caller's scope is indistinguishable from one that does not
    exist — 404, never 403, so the endpoint cannot be used to enumerate other
    agencies' captures.

    A device credential narrows this further, to its own instrument's
    sessions. Without that, a token for one PUNDIT unit could append to any
    session its issuing officer owns — and the device serial, which is the one
    field a statutory reading cannot be wrong about, would be the client's
    choice rather than the credential's.
    """
    queryset = TelemetrySession.objects.filter(
        project__in=scoped_projects(request.user))
    device = getattr(request, 'device', None)
    if device is not None:
        queryset = queryset.filter(device=device)
    return queryset


def _get_session(request, session_id):
    return (_scoped_sessions(request)
            .select_related('device', 'project', 'operator')
            .filter(pk=session_id)
            .first())


def _scoped_devices(user):
    """Devices the caller may see.

    Three relationships, each of them real and none of them an invented
    assignment:

      * assigned to a project in scope, or
      * having actually captured a session on one, or
      * registered by the caller.

    The third is what makes the documented setup order work. An instrument is
    registered when it arrives and put on a project later, once its first job
    is known — but it cannot send anything until it holds a credential, and a
    credential is issued against the device. Without this the registry and the
    credential endpoints disagreed about the same instrument: `digital_eye`
    listed it, and `telemetry` answered 404 for it.

    Note that the first two alone never matched an unassigned device at all.
    ``assigned_project__in=...`` does not match NULL and a device with no
    sessions has no rows to join, so this was not a narrow scope that a
    privileged role could see past — no role could issue a credential for an
    instrument that had not yet been put on a project.
    """
    scoped = scoped_projects(user)
    return FieldDevice.objects.filter(
        Q(assigned_project__in=scoped) |
        Q(telemetry_sessions__project__in=scoped) |
        Q(registered_by=user)
    ).distinct()


def _resolve_device_and_project(request, data):
    """The device a write applies to, and the project it lands on.

    Returns ``(device, project, error_response)``. When the caller presented a
    device credential the named device must be that credential's own — a
    mismatch is refused rather than silently corrected, because substituting
    the right device would hide a gateway configured to push as the wrong
    instrument, which is exactly the mislabelling this check exists to catch.
    """
    credential_device = getattr(request, 'device', None)
    scoped = scoped_projects(request.user)

    if credential_device is not None:
        if str(data['device']) != str(credential_device.id):
            return None, None, Response(
                {'detail': f'This credential may only push as '
                           f'{credential_device.device_id}. It cannot open or '
                           f'write a session for another instrument.'},
                status=status.HTTP_403_FORBIDDEN)
        device = credential_device
    else:
        device = FieldDevice.objects.filter(
            pk=data['device'], is_active=True).first()
        if not device:
            return None, None, Response(
                {'detail': 'Device not found or not active.'},
                status=status.HTTP_404_NOT_FOUND)

    if data.get('project'):
        project = scoped.filter(pk=data['project']).first()
    else:
        project = device.assigned_project if (
            device.assigned_project and
            scoped.filter(pk=device.assigned_project_id).exists()) else None
    if not project:
        return None, None, Response(
            {'detail': 'A project in your scope is required — pass "project" '
                       'or assign this device to one.'},
            status=status.HTTP_400_BAD_REQUEST)
    return device, project, None


class TelemetrySessionStartView(APIView):
    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(request=StartSessionRequestSerializer,
                   responses={201: TelemetrySessionSerializer})
    def post(self, request):
        serializer = StartSessionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        device, project, error = _resolve_device_and_project(request, data)
        if error:
            return error

        try:
            session = TelemetryService.start_session(
                device=device, project=project, operator=request.user,
                data_type=data['data_type'],
                session_config=data.get('session_config') or {},
                session_start=data.get('session_start'),
                transport=data.get('transport') or '',
            )
        except TelemetryError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, 'telemetry.session.start', session.id, {
            'session_reference': session.session_reference,
            'device_id': device.device_id,
            'data_type': session.data_type,
            'transport': session.transport,
            'project': str(project.id),
        })
        return Response(TelemetrySessionSerializer(session).data,
                        status=status.HTTP_201_CREATED)


class TelemetrySessionFromFileView(APIView):
    """`POST telemetry/session/from-file/` — import an instrument export.

    The leg for a unit with no radio: the capture leaves the instrument as a
    file and the inspector uploads it. The file is retained and hashed, its
    rows are parsed against the platform's documented PUNDIT column contract,
    and the result is an ordinary ENDED/PENDING session — so it is reviewed and
    promoted like any other capture, and reaches the registry only through the
    same all-or-nothing `/end/`.

    A file the platform does not recognise is refused with the accepted column
    list. Mapping unknown columns by guesswork is how a wrong number becomes a
    statutory reading with nothing on the record to show it was guessed.
    """

    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(request=ImportExportRequestSerializer,
                   responses={201: TelemetrySessionSerializer})
    def post(self, request):
        serializer = ImportExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # `device` is the one field reused verbatim from the start body; the
        # context fields below are the UPV builder's own keys, so what the
        # operator typed is validated by the same rules a file column is.
        device, project, error = _resolve_device_and_project(
            request, {'device': data['device'], 'project': data.get('project')})
        if error:
            return error

        config = {
            key: data[key] for key in (
                'test_type', 'structural_element', 'floor', 'test_location',
                'weather_condition', 'transducer_type',
                'visual_observation', 'attendance_log',
                'injection_strategy'
            )
            if data.get(key)
        }
        if data.get('transducer_frequency_khz'):
            config['transducer_frequency_khz'] = data['transducer_frequency_khz']
        # GPS coordinates — only stored if the inspector supplied them.
        if data.get('latitude') is not None:
            config['latitude'] = data['latitude']
        if data.get('longitude') is not None:
            config['longitude'] = data['longitude']

        # Handle attached visual observation photos
        uploaded_photos = request.FILES.getlist('photos')
        saved_photo_ids = []
        saved_photo_urls = []
        if uploaded_photos:
            from apps.digital_eye.models import SensorDataFile
            import hashlib
            for p in uploaded_photos:
                digest = hashlib.sha256()
                for chunk in p.chunks():
                    digest.update(chunk)
                sdf = SensorDataFile.objects.create(
                    file=p,
                    file_type='photo',
                    file_name=getattr(p, 'name', 'photo.jpg')[:255],
                    file_size_bytes=getattr(p, 'size', 0),
                    sha256_checksum=digest.hexdigest(),
                    description=f"Visual observation photo for {device.name}",
                    project=project,
                    uploaded_by=request.user if (request.user and request.user.is_authenticated) else None,
                )
                saved_photo_ids.append(str(sdf.id))
                try:
                    saved_photo_urls.append(sdf.file.url)
                except Exception:
                    pass

        if saved_photo_urls:
            config['photos'] = saved_photo_urls
            config['photo_ids'] = saved_photo_ids

        try:
            session, stats = SessionFromFileService.create(
                uploaded_file=data['file'], device=device, project=project,
                operator=request.user, data_type=data['data_type'],
                session_config=config, request=request)
        except TelemetryError as exc:
            _record_audit(request.user, 'telemetry.session.file_import_failed',
                          device.id, {
                              'device_id': device.device_id,
                              'file': getattr(data['file'], 'name', ''),
                              'reason': str(exc),
                          })
            return Response({'detail': str(exc), 'code': exc.code},
                            status=exc.status_code)

        payload = TelemetrySessionSerializer(session).data
        payload['import_stats'] = stats

        if stats.get('duplicate'):
            # A resend, answered with the session the first attempt created.
            # 200 rather than 201 because nothing was created this time, and a
            # caller that retries after a lost response can treat either status
            # as "the file is in" — which is what stops a gateway from
            # re-sending the same export forever.
            _record_audit(request.user, 'telemetry.session.file_import_resend',
                          session.id, {
                              'session_reference': session.session_reference,
                              'device_id': device.device_id,
                              'file': session.source_file_name,
                              'sha256': session.source_file_sha256,
                          })
            return Response(payload, status=status.HTTP_200_OK)

        _record_audit(request.user, 'telemetry.session.file_import', session.id, {
            'session_reference': session.session_reference,
            'device_id': device.device_id,
            'file': session.source_file_name,
            'sha256': session.source_file_sha256,
            'readings': stats['readings'],
            'project': str(project.id),
        })
        return Response(payload, status=status.HTTP_201_CREATED)


class TelemetryPacketAppendView(APIView):
    """`POST telemetry/session/<id>/data/` — append one packet.

    Append-only. Each packet links to its predecessor by hash, so the response
    carries the new `chain_hash` the client needs for the next one.
    """

    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AppendPacketRequestSerializer,
        responses={201: PacketAckSerializer},
    )
    def post(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        serializer = AppendPacketRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            packet = TelemetryService.append_packet(
                session, data['payload'],
                sequence=data.get('sequence'),
                recorded_at=data.get('recorded_at'),
            )
        except TelemetryError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        return Response({
            'session_reference': session.session_reference,
            'sequence': packet.sequence,
            'chain_hash': packet.chain_hash,
            'packet_count': session.packet_count,
        }, status=status.HTTP_201_CREATED)


class TelemetrySessionEndView(APIView):
    """`POST telemetry/session/<id>/end/` — promote the session.

    All-or-nothing: one malformed packet fails the session with `sync_status`
    ``FAILED`` and writes nothing into the statutory registry. Re-running a
    session that already ended is a 409, because promoting twice would duplicate
    statutory rows.
    """

    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: EndSessionResponseSerializer})
    def post(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            promoted = TelemetryService.end_session(session, request)
        except TelemetryError as exc:
            _record_audit(request.user, 'telemetry.session.end_failed', session.id, {
                'session_reference': session.session_reference,
                'reason': str(exc),
            })
            return Response({'detail': str(exc), 'sync_status': session.sync_status},
                            status=exc.status_code)

        _record_audit(request.user, 'telemetry.session.end', session.id, {
            'session_reference': session.session_reference,
            'packets': session.packet_count,
            'sha256_hash': session.sha256_hash,
            'promoted': promoted,
        })
        return Response({
            'session_reference': session.session_reference,
            'status': session.status,
            'sync_status': session.sync_status,
            'packet_count': session.packet_count,
            'sha256_hash': session.sha256_hash,
            'promoted_at': session.promoted_at,
            'promoted': promoted,
        })


class TelemetrySessionStatusView(APIView):
    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: SessionStatusSerializer})
    def get(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(SessionStatusSerializer(session).data)


class TelemetrySessionListView(APIView):
    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: TelemetrySessionSerializer(many=True)})
    def get(self, request):
        queryset = _scoped_sessions(request).select_related(
            'device', 'project', 'operator')
        for param, field in (('device', 'device_id'),
                             ('project', 'project_id'),
                             ('data_type', 'data_type'),
                             ('status', 'status'),
                             ('sync_status', 'sync_status'),
                             ('transport', 'transport')):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        return Response(TelemetrySessionSerializer(queryset[:200], many=True).data)


class TelemetryDeviceListView(APIView):
    """Read-only projection over the existing device registry.

    No second registry: `POST` here would be a duplicate of
    `/api/v1/digital-eye/devices/`, and two registries for one physical
    instrument is how a device ends up with two calibration histories.
    """

    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: FieldDeviceProjectionSerializer(many=True)})
    def get(self, request):
        queryset = _scoped_devices(request.user)
        device_type = request.query_params.get('device_type')
        if device_type:
            queryset = queryset.filter(device_type=device_type)
        queryset = queryset.order_by('-last_seen', 'device_id')[:200]
        return Response(FieldDeviceProjectionSerializer(queryset, many=True).data)


class DeviceTokenListCreateView(APIView):
    """`GET` the caller's device credentials, `POST` to issue one.

    The plaintext is in the POST response and nowhere else — the row keeps a
    digest, so a credential that is lost is replaced rather than recovered.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: DeviceTokenSerializer(many=True)})
    def get(self, request):
        queryset = (DeviceToken.objects
                    .filter(device__in=_scoped_devices(request.user))
                    .select_related('device'))
        device = request.query_params.get('device')
        if device:
            queryset = queryset.filter(device_id=device)
        active = request.query_params.get('active')
        if active and active.lower() in ('1', 'true', 'yes'):
            queryset = queryset.filter(revoked_at__isnull=True)
        return Response(DeviceTokenSerializer(queryset[:200], many=True).data)

    @extend_schema(request=IssueDeviceTokenRequestSerializer,
                   responses={201: IssuedDeviceTokenSerializer})
    def post(self, request):
        serializer = IssueDeviceTokenRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Scoped like every other device read: a device outside the caller's
        # projects is 404, so this cannot be used to mint a credential for
        # another agency's instrument.
        device = _scoped_devices(request.user).filter(pk=data['device']).first()
        if not device:
            return Response(
                {'detail': 'Device not found among the devices you can see.'},
                status=status.HTTP_404_NOT_FOUND)

        try:
            token, raw = DeviceTokenService.issue(
                device=device, label=data['label'], issued_by=request.user,
                expires_at=data.get('expires_at'))
        except TelemetryError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, 'telemetry.device_token.issue', token.id, {
            'device_id': device.device_id,
            'label': token.label,
            'key_prefix': token.key_prefix,
            'expires_at': token.expires_at.isoformat() if token.expires_at else None,
        })
        payload = DeviceTokenSerializer(token).data
        # Only this response carries the secret, and only this once.
        payload['token'] = raw
        return Response(payload, status=status.HTTP_201_CREATED)


class DeviceTokenRevokeView(APIView):
    """`POST telemetry/device-tokens/<id>/revoke/` — effective immediately."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: DeviceTokenSerializer})
    def post(self, request, token_id):
        token = (DeviceToken.objects
                 .filter(device__in=_scoped_devices(request.user))
                 .select_related('device')
                 .filter(pk=token_id).first())
        if not token:
            return Response({'detail': 'Device credential not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        already = token.is_revoked
        DeviceTokenService.revoke(token, revoked_by=request.user)
        if not already:
            _record_audit(request.user, 'telemetry.device_token.revoke', token.id, {
                'device_id': token.device.device_id,
                'label': token.label,
                'key_prefix': token.key_prefix,
            })
        return Response(DeviceTokenSerializer(token).data)
