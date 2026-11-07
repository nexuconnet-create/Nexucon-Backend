
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
from django.db import transaction
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
                'location_address',
                'weather_condition', 'transducer_type',
                'visual_observation', 'attendance_log',
                'injection_strategy', 'folder_name', 'clear_folder'
            )
            if data.get(key)
        }
        if data.get('transducer_frequency_khz'):
            config['transducer_frequency_khz'] = data['transducer_frequency_khz']
        # GPS coordinates & 4-corner boundary survey
        if data.get('latitude') is not None:
            config['latitude'] = data['latitude']
        if data.get('longitude') is not None:
            config['longitude'] = data['longitude']
        if data.get('coordinate_system'):
            config['coordinate_system'] = data['coordinate_system']
        corner_coords = data.get('corner_coordinates')
        if isinstance(corner_coords, str):
            try:
                import json
                corner_coords = json.loads(corner_coords)
            except Exception:
                pass
        if corner_coords:
            config['corner_coordinates'] = corner_coords
            if project and (not project.corner_coordinates or not getattr(project.corner_coordinates, 'get', lambda k: None)('corners')):
                project.corner_coordinates = corner_coords
                fields_to_update = ['corner_coordinates']
                if data.get('coordinate_system'):
                    project.coordinate_system = data['coordinate_system']
                    fields_to_update.append('coordinate_system')
                if data.get('latitude') is not None and not project.latitude:
                    project.latitude = data['latitude']
                    fields_to_update.append('latitude')
                if data.get('longitude') is not None and not project.longitude:
                    project.longitude = data['longitude']
                    fields_to_update.append('longitude')
                project.save(update_fields=fields_to_update)

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
                session_config=config, request=request,
                folder_name=data.get('folder_name'))
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

    def patch(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        cfg = dict(session.session_config or {})
        payload_cfg = request.data.get('session_config') if isinstance(request.data.get('session_config'), dict) else {}

        for key in ['surface_temperature_c', 'moisture_condition', 'weather_condition', 'weather', 'visual_observation', 'location_address', 'latitude', 'longitude']:
            if key in request.data:
                cfg[key] = request.data[key]
            elif key in payload_cfg:
                cfg[key] = payload_cfg[key]

        session.session_config = cfg
        session.save(update_fields=['session_config', 'updated_at'])

        # Propagate environmental conditions to PUNDITTest instances
        from apps.digital_eye.models import PUNDITTest
        update_fields = {}
        if 'surface_temperature_c' in cfg and cfg['surface_temperature_c'] is not None:
            try:
                update_fields['surface_temperature_c'] = float(cfg['surface_temperature_c'])
            except (ValueError, TypeError):
                pass
        if 'moisture_condition' in cfg and cfg['moisture_condition']:
            update_fields['moisture_condition'] = str(cfg['moisture_condition']).lower().strip()
        if 'weather_condition' in cfg and cfg['weather_condition']:
            update_fields['weather_condition'] = str(cfg['weather_condition']).strip()

        if update_fields:
            promoted_tests = (session.data_payload or {}).get('tests', [])
            test_ids = [t['test_id'] for t in promoted_tests if isinstance(t, dict) and 'test_id' in t]
            if test_ids:
                PUNDITTest.objects.filter(id__in=test_ids).update(**update_fields)
            elif session.project_id:
                PUNDITTest.objects.filter(project_id=session.project_id).update(**update_fields)

        return Response({
            'status': 'updated',
            'session_id': str(session.id),
            'session_config': session.session_config,
            'data': SessionStatusSerializer(session).data
        })


class TelemetrySessionDetailView(APIView):
    """`GET`, `PATCH`, `DELETE` telemetry/session/<uuid:session_id>/

    DELETE: Deletes the telemetry session and its packets, with optional cleanup of promoted test records.
    PATCH: Updates session inputs, parameters, and supports replacing / re-importing the scan file.
    GET: Retrieves the telemetry session details.
    """
    authentication_classes = TELEMETRY_AUTH
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(responses={200: TelemetrySessionSerializer})
    def get(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(TelemetrySessionSerializer(session).data)

    def delete(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        delete_promoted = request.query_params.get('delete_promoted', 'true').lower() in ('true', '1', 'yes')
        if request.data and isinstance(request.data, dict) and 'delete_promoted' in request.data:
            delete_promoted = bool(request.data.get('delete_promoted'))

        session_ref = session.session_reference
        proj_id = session.project_id

        with transaction.atomic():
            if delete_promoted and proj_id:
                from apps.digital_eye.models import PUNDITTest, GPRSurvey
                promoted_info = (session.data_payload or {}).get('promoted') or (session.data_payload or {})
                test_ids = []
                if isinstance(promoted_info, dict):
                    if 'tests' in promoted_info and isinstance(promoted_info['tests'], list):
                        for t in promoted_info['tests']:
                            if isinstance(t, dict) and 'test_id' in t:
                                test_ids.append(t['test_id'])
                            elif isinstance(t, str):
                                test_ids.append(t)
                    if 'survey_id' in promoted_info:
                        GPRSurvey.objects.filter(id=promoted_info['survey_id']).delete()

                if test_ids:
                    PUNDITTest.objects.filter(id__in=test_ids).delete()
                else:
                    folder_name = (session.session_config or {}).get('folder_name') or (session.session_config or {}).get('test_location')
                    clean_qs = PUNDITTest.objects.filter(project_id=proj_id)
                    filters = Q(structural_element=f'Telemetry capture {session_ref}')
                    if folder_name:
                        filters |= Q(test_location=folder_name)
                    clean_qs.filter(filters).delete()

            if session.source_file_storage_name:
                try:
                    from django.core.files.storage import default_storage
                    default_storage.delete(session.source_file_storage_name)
                except Exception:
                    pass

            session.delete()

        _record_audit(request.user, 'telemetry.session.delete', session_id, {
            'session_reference': session_ref,
            'delete_promoted': delete_promoted,
        })
        return Response({
            'detail': f'Session {session_ref} deleted successfully.',
            'session_reference': session_ref,
        }, status=status.HTTP_200_OK)

    def patch(self, request, session_id):
        session = _get_session(request, session_id)
        if not session:
            return Response({'detail': 'Telemetry session not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        data = request.data
        config = dict(session.session_config or {})

        if data.get('session_reference') and str(data['session_reference']).strip():
            new_ref = str(data['session_reference']).strip()
            if new_ref != session.session_reference:
                if TelemetrySession.objects.filter(session_reference=new_ref).exclude(pk=session.pk).exists():
                    return Response({'detail': f'Folder name "{new_ref}" is already in use.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                session.session_reference = new_ref

        if 'project' in data:
            proj_val = data.get('project')
            if proj_val and str(proj_val).strip():
                proj = scoped_projects(request.user).filter(pk=proj_val).first()
                if proj:
                    session.project = proj
            elif proj_val == '':
                session.project = None

        CONTEXT_FIELDS = [
            'structural_element', 'floor', 'test_location', 'location_address',
            'weather_condition', 'transducer_type', 'transducer_frequency_khz',
            'visual_observation', 'attendance_log', 'folder_name', 'injection_strategy',
            'surface_temperature_c', 'moisture_condition', 'weather'
        ]
        for field in CONTEXT_FIELDS:
            if field in data:
                val = data.get(field)
                if val is not None and str(val).strip() != '':
                    config[field] = val
                elif field in config and val == '':
                    config[field] = ''

        for coord_field in ['latitude', 'longitude', 'coordinate_system']:
            if coord_field in data:
                val = data.get(coord_field)
                if val is not None and str(val).strip() != '':
                    try:
                        config[coord_field] = float(val) if coord_field in ('latitude', 'longitude') else str(val).strip()
                    except (ValueError, TypeError):
                        pass

        corner_coords = data.get('corner_coordinates')
        if isinstance(corner_coords, str):
            try:
                import json
                corner_coords = json.loads(corner_coords)
            except Exception:
                pass
        if corner_coords:
            config['corner_coordinates'] = corner_coords

        # Handle photo deletion
        delete_photos = []
        if hasattr(request.data, 'getlist'):
            delete_photos = request.data.getlist('delete_photos')
        if not delete_photos and 'delete_photos' in request.data:
            dp = request.data.get('delete_photos')
            if isinstance(dp, str):
                try:
                    import json
                    parsed = json.loads(dp)
                    delete_photos = parsed if isinstance(parsed, list) else [dp]
                except Exception:
                    delete_photos = [dp]
            elif isinstance(dp, list):
                delete_photos = dp

        remaining_photos = request.data.get('remaining_photos')
        if remaining_photos is not None and isinstance(remaining_photos, str):
            try:
                import json
                remaining_photos = json.loads(remaining_photos)
            except Exception:
                remaining_photos = None

        curr_photos = list(config.get('photos') or [])
        curr_ids = list(config.get('photo_ids') or [])

        def _clean_photo_key(val):
            if not val:
                return ''
            s = str(val).split('?')[0].strip()
            return s.split('/')[-1].lower()

        to_remove = set()
        if remaining_photos is not None and isinstance(remaining_photos, list):
            retained_keys = {_clean_photo_key(p) for p in remaining_photos}
            for p in curr_photos:
                if _clean_photo_key(p) not in retained_keys:
                    to_remove.add(p)
        if delete_photos:
            for p in delete_photos:
                to_remove.add(p)

        if to_remove:
            remove_keys = {_clean_photo_key(p) for p in to_remove}
            new_photos = []
            new_ids = []

            from apps.digital_eye.models import SensorDataFile, PUNDITTest
            sdfs = SensorDataFile.objects.filter(id__in=curr_ids)
            sdf_by_id = {str(sf.id): sf for sf in sdfs}

            for idx, p_url in enumerate(curr_photos):
                k = _clean_photo_key(p_url)
                if k in remove_keys or p_url in to_remove:
                    matching_sdf = None
                    if idx < len(curr_ids) and curr_ids[idx] in sdf_by_id:
                        matching_sdf = sdf_by_id[curr_ids[idx]]
                    else:
                        for sf in sdfs:
                            if _clean_photo_key(getattr(sf.file, 'name', '')) == k or _clean_photo_key(getattr(sf.file, 'url', '')) == k:
                                matching_sdf = sf
                                break
                    if matching_sdf:
                        try:
                            for test in PUNDITTest.objects.filter(files=matching_sdf):
                                test.files.remove(matching_sdf)
                            matching_sdf.delete()
                        except Exception as e:
                            logger.warning("Error deleting SensorDataFile %s: %s", getattr(matching_sdf, 'id', ''), e)
                else:
                    new_photos.append(p_url)
                    if idx < len(curr_ids):
                        new_ids.append(curr_ids[idx])

            config['photos'] = new_photos
            config['photo_ids'] = new_ids

            if session.project_id:
                try:
                    from apps.inspections.models import Inspection
                    for insp in Inspection.objects.filter(project_id=session.project_id):
                        if insp.visual_site_photos:
                            updated_isp = [u for u in insp.visual_site_photos if _clean_photo_key(u) not in remove_keys and u not in to_remove]
                            if len(updated_isp) != len(insp.visual_site_photos):
                                insp.visual_site_photos = updated_isp
                                insp.save(update_fields=['visual_site_photos'])
                except Exception as e:
                    logger.warning("Error updating inspection visual_site_photos: %s", e)

        # Handle newly uploaded photos
        uploaded_photos = request.FILES.getlist('photos')
        new_added_sdfs = []
        if uploaded_photos:
            from apps.digital_eye.models import SensorDataFile
            import hashlib
            saved_photo_ids = list(config.get('photo_ids') or [])
            saved_photo_urls = list(config.get('photos') or [])
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
                    description=f"Visual observation photo for {session.device.name}",
                    project=session.project,
                    uploaded_by=request.user if (request.user and request.user.is_authenticated) else None,
                )
                new_added_sdfs.append(sdf)
                saved_photo_ids.append(str(sdf.id))
                try:
                    saved_photo_urls.append(sdf.file.url)
                except Exception:
                    pass
            config['photos'] = saved_photo_urls
            config['photo_ids'] = saved_photo_ids

        session.session_config = config

        new_file = request.FILES.get('file')
        re_imported = False
        if new_file:
            from apps.telemetry.export_import import SessionFromFileService, _BuildContext
            from apps.data_import.readers import detect_import_type, read_rows
            from apps.data_import.registry import REGISTRY, group_upv_rows, UPV_CONTEXT_KEYS, RowError
            from apps.telemetry.services import TelemetryService
            from apps.evidence.files import safe_file_name

            content = SessionFromFileService._read_upload(new_file)
            name = safe_file_name(getattr(new_file, 'name', ''))
            digest = SessionFromFileService._digest(content)
            import_type = detect_import_type(content)

            try:
                rows, skipped = read_rows(content, import_type, header_map=session.device.column_mapping or None)
            except Exception as exc:
                return Response({'detail': f'Error reading scan file: {exc}'}, status=status.HTTP_400_BAD_REQUEST)

            if not rows:
                return Response({'detail': 'The replacement file held no readable rows.'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                SessionFromFileService._assert_known_columns(rows, name, session.device.column_mapping)
            except Exception as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            merged = SessionFromFileService._merge_context(rows, config)
            groups, grouping_error = group_upv_rows(merged)
            if grouping_error:
                return Response({'detail': f'{name}: {grouping_error}'}, status=status.HTTP_400_BAD_REQUEST)

            packets = []
            shared = None
            for _key, group_rows in groups:
                try:
                    payload, _ = REGISTRY['UPV'].build(group_rows, _BuildContext(session.project))
                except RowError as exc:
                    return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

                readings = payload.pop('readings', [])
                context = {key: value for key, value in payload.items() if key in UPV_CONTEXT_KEYS}
                shared = context if shared is None else {
                    key: value for key, value in shared.items() if context.get(key) == value
                }
                for reading in readings:
                    packets.append({**context, **reading})

            if not packets:
                return Response({'detail': 'No readings could be extracted from the replacement file.'}, status=status.HTTP_400_BAD_REQUEST)

            config.update(shared or {})
            session.session_config = config
            stored_name, digest = SessionFromFileService._store(content, name)

            was_synced = session.sync_status == TelemetrySession.SYNC_SYNCED

            with transaction.atomic():
                if was_synced and session.project_id:
                    from apps.digital_eye.models import PUNDITTest
                    promoted_info = (session.data_payload or {}).get('promoted') or (session.data_payload or {})
                    test_ids = []
                    if isinstance(promoted_info, dict) and 'tests' in promoted_info:
                        for t in promoted_info['tests']:
                            if isinstance(t, dict) and 'test_id' in t:
                                test_ids.append(t['test_id'])
                    if test_ids:
                        PUNDITTest.objects.filter(id__in=test_ids).delete()
                    else:
                        PUNDITTest.objects.filter(
                            project_id=session.project_id,
                            structural_element=f'Telemetry capture {session.session_reference}'
                        ).delete()

                session.packets.all().delete()
                session.packet_count = len(packets)
                session.source_file_name = name
                session.source_file_sha256 = digest
                session.source_file_storage_name = stored_name
                session.status = TelemetrySession.STATUS_OPEN
                session.sync_status = TelemetrySession.SYNC_PENDING
                session.sync_error = ''
                session.save()

                for index, packet in enumerate(packets, start=1):
                    TelemetryService.append_packet(session, packet, sequence=index)

                from django.utils import timezone
                session.status = TelemetrySession.STATUS_ENDED
                session.session_end = timezone.now()
                session.save(update_fields=['status', 'session_end', 'updated_at'])
                session.refresh_from_db()

                if was_synced:
                    promoted = TelemetryService.end_session(session, request)
                    re_imported = True

        else:
            session.save(update_fields=['session_reference', 'project', 'session_config', 'updated_at'])

            if session.sync_status == TelemetrySession.SYNC_SYNCED and session.project_id:
                from apps.digital_eye.models import PUNDITTest
                promoted_info = (session.data_payload or {}).get('promoted') or (session.data_payload or {})
                test_ids = []
                if isinstance(promoted_info, dict) and 'tests' in promoted_info:
                    for t in promoted_info['tests']:
                        if isinstance(t, dict) and 'test_id' in t:
                            test_ids.append(t['test_id'])

                update_fields = {}
                if config.get('structural_element'):
                    update_fields['structural_element'] = config['structural_element']
                if config.get('floor'):
                    update_fields['floor'] = config['floor']
                if config.get('test_location'):
                    update_fields['test_location'] = config['test_location']
                if config.get('location_address'):
                    update_fields['location_address'] = config['location_address']
                if config.get('weather_condition'):
                    update_fields['weather_condition'] = config['weather_condition']
                if config.get('latitude') is not None:
                    update_fields['latitude'] = config['latitude']
                if config.get('longitude') is not None:
                    update_fields['longitude'] = config['longitude']

                if update_fields:
                    qs = PUNDITTest.objects.filter(id__in=test_ids) if test_ids else PUNDITTest.objects.filter(
                        project_id=session.project_id,
                        structural_element=f'Telemetry capture {session.session_reference}'
                    )
                    qs.update(**update_fields)

                if new_added_sdfs:
                    tests_to_link = PUNDITTest.objects.filter(id__in=test_ids) if test_ids else PUNDITTest.objects.filter(
                        project_id=session.project_id,
                        structural_element=f'Telemetry capture {session.session_reference}'
                    )
                    for t in tests_to_link:
                        t.files.add(*new_added_sdfs)

        session.refresh_from_db()
        _record_audit(request.user, 'telemetry.session.update', session.id, {
            'session_reference': session.session_reference,
            'file_replaced': bool(new_file),
            're_imported': re_imported,
        })
        payload = TelemetrySessionSerializer(session).data
        payload['re_imported'] = re_imported
        return Response(payload, status=status.HTTP_200_OK)


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
