"""
Telemetry API views (Inspector PWA — Part 3, dual-path ingestion).

Routes:
  POST telemetry/session/start/          open a capture
  POST telemetry/session/<uuid>/data/    append a packet
  POST telemetry/session/<uuid>/end/     close and promote into the registry
  GET  telemetry/session/<uuid>/status/  poll capture + promotion state
  GET  telemetry/sessions/               list the caller's sessions
  GET  telemetry/devices/                read-only device projection
"""
import logging

from drf_spectacular.utils import extend_schema
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from apps.digital_eye.models import FieldDevice
from common.permissions import scoped_projects, user_role_name

from .models import TelemetrySession
from .serializers import (
    AppendPacketRequestSerializer,
    EndSessionResponseSerializer,
    FieldDeviceProjectionSerializer,
    PacketAckSerializer,
    SessionStatusSerializer,
    StartSessionRequestSerializer,
    TelemetrySessionSerializer,
)
from .services import TelemetryError, TelemetryService

logger = logging.getLogger(__name__)


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


def _scoped_sessions(user):
    """Sessions the caller may act on.

    Scoped by project, like every other field record. A session whose project
    is outside the caller's scope is indistinguishable from one that does not
    exist — 404, never 403, so the endpoint cannot be used to enumerate other
    agencies' captures.
    """
    return TelemetrySession.objects.filter(project__in=scoped_projects(user))


def _get_session(user, session_id):
    return (_scoped_sessions(user)
            .select_related('device', 'project', 'operator')
            .filter(pk=session_id)
            .first())


class TelemetrySessionStartView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=StartSessionRequestSerializer,
                   responses={201: TelemetrySessionSerializer})
    def post(self, request):
        serializer = StartSessionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Resolve the device through the caller's project scope. A device is
        # usable if it is assigned to a project in scope; when the request
        # names a project explicitly, it must be in scope too.
        device = FieldDevice.objects.filter(pk=data['device'], is_active=True).first()
        if not device:
            return Response({'detail': 'Device not found or not active.'},
                            status=status.HTTP_404_NOT_FOUND)

        scoped = scoped_projects(request.user)
        if data.get('project'):
            project = scoped.filter(pk=data['project']).first()
        else:
            project = device.assigned_project if (
                device.assigned_project and
                scoped.filter(pk=device.assigned_project_id).exists()) else None
        if not project:
            return Response(
                {'detail': 'A project in your scope is required — pass "project" '
                           'or assign this device to one.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            session = TelemetryService.start_session(
                device=device, project=project, operator=request.user,
                data_type=data['data_type'],
                session_config=data.get('session_config') or {},
                session_start=data.get('session_start'),
            )
        except TelemetryError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, 'telemetry.session.start', session.id, {
            'session_reference': session.session_reference,
            'device_id': device.device_id,
            'data_type': session.data_type,
            'project': str(project.id),
        })
        return Response(TelemetrySessionSerializer(session).data,
                        status=status.HTTP_201_CREATED)


class TelemetryPacketAppendView(APIView):
    """`POST telemetry/session/<id>/data/` — append one packet.

    Append-only. Each packet links to its predecessor by hash, so the response
    carries the new `chain_hash` the client needs for the next one.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AppendPacketRequestSerializer,
        responses={201: PacketAckSerializer},
    )
    def post(self, request, session_id):
        session = _get_session(request.user, session_id)
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

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: EndSessionResponseSerializer})
    def post(self, request, session_id):
        session = _get_session(request.user, session_id)
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
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: SessionStatusSerializer})
    def get(self, request, session_id):
        session = _get_session(request.user, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(SessionStatusSerializer(session).data)


class TelemetrySessionListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: TelemetrySessionSerializer(many=True)})
    def get(self, request):
        queryset = _scoped_sessions(request.user).select_related(
            'device', 'project', 'operator')
        for param, field in (('device', 'device_id'),
                             ('project', 'project_id'),
                             ('data_type', 'data_type'),
                             ('status', 'status'),
                             ('sync_status', 'sync_status')):
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
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: FieldDeviceProjectionSerializer(many=True)})
    def get(self, request):
        # A device is visible when it is assigned to a project in scope, or
        # when it has actually captured a session on one. Both are real
        # relationships; neither invents an assignment.
        scoped = scoped_projects(request.user)
        queryset = FieldDevice.objects.filter(
            Q(assigned_project__in=scoped) |
            Q(telemetry_sessions__project__in=scoped)
        ).distinct()
        device_type = request.query_params.get('device_type')
        if device_type:
            queryset = queryset.filter(device_type=device_type)
        queryset = queryset.order_by('-last_seen', 'device_id')[:200]
        return Response(FieldDeviceProjectionSerializer(queryset, many=True).data)
