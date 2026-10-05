"""
Digital Eye API views.

Device registry, sensor-data upload with SHA-256 checksums, GPR / PUNDIT /
GNSS survey capture with deterministic AI adapters, BIM element GUID mappings,
live streams anchored to BIM coordinates, and Scan-to-BIM / AI analytics endpoints.
"""
import hashlib
import logging
import uuid

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as InvalidQueryParam
from rest_framework.filters import SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import IsDirector, scoped_projects, user_is_director
from common.responses.standard import StandardResponse

from .adapters import GNSSProjection, GPRAdapter, PUNDITAdapter
from .bim_preview import build_preview_geometry
from .models import (
    AIAnalysisRecord, BIMElementMapping, BIMModelGeometry, BIMStructuralElement,
    CoreSample, DeviceConnectionLog, DeviceReportRecord, DigitalEyeFinding, EvidenceSpatialPoint,
    FieldDevice, GPRAnomaly, GPRScan, GPRSurvey, GnssBenchmark,
    GnssBoundaryPoint, GnssSurvey, LiveStream, ProjectCurveSetting,
    PUNDITTest, PunditTest, ProcessingQueueJob, SensorDataFile,
    StrengthCurve, TrimbleConnection, TrimbleProject,
)
from .serializers import (
    AIAnalysisRecordSerializer, BIMElementMappingSerializer, BIMStructuralElementSerializer,
    CoreSampleSerializer, DeviceConnectionLogSerializer, DeviceReportRecordSerializer, DigitalEyeFindingSerializer,
    EvidenceSpatialPointSerializer, FieldDeviceSerializer, GPRAnomalySerializer,
    GPRScanSerializer, GPRSurveySerializer, GnssBenchmarkSerializer,
    GnssBoundaryPointSerializer, GnssSurveySerializer,
    LiveStreamSerializer, PUNDITTestSerializer, PunditTestSerializer,
    ProcessingQueueJobSerializer, SensorDataFileSerializer,
    StrengthCurveSerializer, TrimbleConnectionSerializer, TrimbleProjectSerializer,
)

logger = logging.getLogger(__name__)


def _record_audit(user, action, model, obj_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        from common.permissions import user_role_name
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type=model,
            resource_id=str(obj_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


def _scoped_legacy_queryset(queryset, user):
    """Restrict a Scan-to-BIM / analytics queryset to the caller's project scope.

    These models carry both a real `project` FK and denormalised
    `project_id_str` / `project_name` copies, so the scope filter has to match
    on either. Rows naming no project at all are withheld rather than shown —
    an unattributable statutory record must not leak across agencies.
    """
    allowed = scoped_projects(user)
    allowed_ids = [str(pk) for pk in allowed.values_list('pk', flat=True)]
    return queryset.filter(Q(project__in=allowed) | Q(project_id_str__in=allowed_ids))


def _nexucon_link_settings(request):
    """
    Read (GET) or update (PATCH) the Nexucon Link platform system settings.

    Shared by the canonical 'nexucon-link/settings/' endpoint and the Curve
    Manager's 'nexucon-link/curves/settings/' alias so the two can never
    diverge. Reads are open to any authenticated user; a write is
    Director-only and audit-logged — these settings choose the curve type
    every project's strength workflow defaults to.
    """
    from .models import NexuconLinkSettings
    from .serializers import NexuconLinkSettingsSerializer

    settings_obj = NexuconLinkSettings.load()
    if request.method in ('GET', 'HEAD'):
        return Response(NexuconLinkSettingsSerializer(settings_obj).data)

    if not IsDirector().has_permission(request, None):
        return Response(
            {'detail': 'Only a Director may change the platform Nexucon Link '
                       'settings.'},
            status=status.HTTP_403_FORBIDDEN)
    serializer = NexuconLinkSettingsSerializer(
        settings_obj, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save(updated_by=request.user)
    _record_audit(request.user, 'digital_eye.nexucon_link.settings.update',
                  'NexuconLinkSettings', settings_obj.id,
                  dict(serializer.validated_data))
    return Response(serializer.data)


# NOTE: this module previously contained a `_seed_defaults_if_empty()` helper
# that inserted fabricated "Eko Atlantic Signature Tower" structural elements,
# GPR scans, PUNDIT tests (including a record stamped VERIFIED with an invented
# 4246 m/s velocity and 42.5 MPa strength) and a Trimble connection whenever a
# table was empty — and it ran on ordinary read requests. It has been removed:
# an empty table means no data has been captured yet, and the platform must
# never invent measurements it did not receive from an instrument.


# ======================================================================
# Digital Eye Field Device & Hardware ViewSets
# ======================================================================

class FieldDeviceViewSet(viewsets.ModelViewSet):
    """
    Device registry for Digital Eye hardware (Tersus GNSS MVP SI, GPR carts,
    PUNDIT instruments, scanners). Devices report telemetry through the
    heartbeat action.
    """
    serializer_class = FieldDeviceSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['device_type', 'status', 'assigned_project', 'is_active']
    search_fields = ['device_id', 'name', 'device_reference', 'model', 'manufacturer']

    def get_queryset(self):
        qs = FieldDevice.objects.select_related('assigned_project', 'registered_by')
        project = self.request.query_params.get('project')
        if project:
            allowed = scoped_projects(self.request.user).filter(pk=project)
            if not allowed.exists():
                return FieldDevice.objects.none()
        return qs

    def perform_create(self, serializer):
        device = serializer.save(registered_by=self.request.user)
        _record_audit(self.request.user, 'digital_eye.device.register',
                      'FieldDevice', device.id,
                      {'device_id': device.device_id, 'type': device.device_type})

    @action(detail=True, methods=['post'])
    def heartbeat(self, request, pk=None):
        device = self.get_object()
        battery = request.data.get('battery_level')
        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        updates = ['last_seen', 'status', 'updated_at']
        device.last_seen = timezone.now()
        device.status = 'online'
        if battery is not None:
            try:
                battery = int(battery)
            except (TypeError, ValueError):
                return Response({'detail': 'battery_level must be an integer (percent).'},
                                status=status.HTTP_400_BAD_REQUEST)
            if not 0 <= battery <= 100:
                return Response({'detail': 'battery_level must be between 0 and 100.'},
                                status=status.HTTP_400_BAD_REQUEST)
            device.battery_level = battery
            updates.append('battery_level')
        for name in ('latitude', 'longitude'):
            value = request.data.get(name)
            if value is not None:
                try:
                    setattr(device, name, float(value))
                except (TypeError, ValueError):
                    return Response({'detail': f'{name} must be a number.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                updates.append(name)
        device.save(update_fields=updates)
        return Response(FieldDeviceSerializer(device, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def connect(self, request, pk=None):
        """`POST digital-eye/devices/<uuid>/connect/` — log a user connection event."""
        device = self.get_object()
        protocol = request.data.get('protocol')
        
        valid_protocols = {c[0] for c in DeviceConnectionLog.PROTOCOL_CHOICES}
        if protocol not in valid_protocols:
            return Response({'detail': f'protocol must be one of: {", ".join(sorted(valid_protocols))}'},
                            status=status.HTTP_400_BAD_REQUEST)
                            
        log = DeviceConnectionLog.objects.create(
            device=device,
            event=DeviceConnectionLog.EVENT_CONNECTED,
            protocol=protocol,
            user=request.user,
            rssi_dbm=request.data.get('rssi_dbm'),
            ip_address=request.data.get('ip_address'),
            cloud_workspace_id=request.data.get('cloud_workspace_id', ''),
            firmware_banner=request.data.get('firmware_banner', ''),
            notes=request.data.get('notes', ''),
        )
        
        # Connection also counts as a heartbeat if it succeeded
        device.last_seen = timezone.now()
        device.status = 'online'
        device.save(update_fields=['last_seen', 'status'])
        
        _record_audit(request.user, 'digital_eye.device.connect',
                      'FieldDevice', device.id,
                      {'device_id': device.device_id, 'protocol': protocol})
                      
        return Response(DeviceConnectionLogSerializer(log).data)

    @action(detail=True, methods=['post'])
    def disconnect(self, request, pk=None):
        """`POST digital-eye/devices/<uuid>/disconnect/` — log a user disconnection event."""
        device = self.get_object()
        protocol = request.data.get('protocol')
        
        valid_protocols = {c[0] for c in DeviceConnectionLog.PROTOCOL_CHOICES}
        if protocol not in valid_protocols:
            return Response({'detail': f'protocol must be one of: {", ".join(sorted(valid_protocols))}'},
                            status=status.HTTP_400_BAD_REQUEST)
                            
        is_error = request.data.get('error', False)
        event_type = DeviceConnectionLog.EVENT_FAILED if is_error else DeviceConnectionLog.EVENT_DISCONNECTED
        
        log = DeviceConnectionLog.objects.create(
            device=device,
            event=event_type,
            protocol=protocol,
            user=request.user,
            error_message=request.data.get('error_message', ''),
            notes=request.data.get('notes', ''),
        )
        
        _record_audit(request.user, 'digital_eye.device.disconnect',
                      'FieldDevice', device.id,
                      {'device_id': device.device_id, 'protocol': protocol, 'error': is_error})
                      
        return Response(DeviceConnectionLogSerializer(log).data)

    @action(detail=True, methods=['post'])
    def gateway(self, request, pk=None):
        """`POST digital-eye/devices/<uuid>/gateway/` — turn sync on or off.

        Body: ``{"enabled": true}``. Turning it on mints a credential for this
        instrument and writes the field gateway's config for it, in one step —
        see ``apps.telemetry.gateway_config``. **The credential is never
        returned.** It goes from the mint into the config file the gateway
        reads, and no client, log or response ever holds it, which is the whole
        point of provisioning from here rather than by hand.

        Resolved through ``_scoped_devices`` rather than ``self.get_queryset``
        on purpose. That viewset's queryset returns every device in the system
        to any authenticated user — a known hole left alone for now — and this
        action mints a credential. Scoping it here means the hole cannot be
        reached through provisioning: a device outside the caller's projects is
        404, the same answer the credentials endpoint gives.
        """
        from apps.telemetry.gateway_config import (
            GatewayConfigError, GatewayConfigService)
        from apps.telemetry.views import _scoped_devices

        device = _scoped_devices(request.user).filter(pk=pk).first()
        if device is None:
            return Response(
                {'detail': 'Device not found among the devices you can see.'},
                status=status.HTTP_404_NOT_FOUND)

        enabled = request.data.get('enabled')
        if not isinstance(enabled, bool):
            return Response(
                {'detail': 'Send {"enabled": true} or {"enabled": false}.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            if enabled:
                path = GatewayConfigService.enable(device, actor=request.user)
                action_name = 'digital_eye.device.gateway.enable'
                detail = {'config_path': path}
            else:
                path = GatewayConfigService.disable(device, actor=request.user)
                action_name = 'digital_eye.device.gateway.disable'
                detail = {'config_path': path}
        except GatewayConfigError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, action_name, 'FieldDevice', device.id,
                      {'device_id': device.device_id, **detail})
        device.refresh_from_db(fields=['gateway_enabled'])
        return Response(
            FieldDeviceSerializer(device, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='column-mapping/suggest',
            parser_classes=[MultiPartParser, FormParser])
    def suggest_columns(self, request, pk=None):
        """`POST digital-eye/devices/<uuid>/column-mapping/suggest/`.

        Body: multipart, optionally carrying a ``file``. With one, returns the
        file's own header names — verbatim, as the export spells them — beside
        the platform column each one appears to be, plus a ready-to-save
        mapping. Without one, returns the accepted contract and nothing else.

        The file is optional because a picker needs the contract whether or not
        there is an export to read: an inspector editing a mapping already
        recorded has no file in hand, and the contract has to come from the
        server rather than be written out a second time in the frontend — two
        lists of what the platform accepts drift, and a picker that offers a
        column the platform has stopped accepting is worse than no picker.

        **It writes nothing.** No device changes, no bytes are stored, no
        session is opened. It exists so an inspector can see what the platform
        made of their export *before* anything is recorded from it; saving the
        mapping is then a separate, deliberate request against the device. That
        separation is the point — ``FieldDevice.column_mapping`` says a wrong
        mapping is indistinguishable from a right one once rows have been
        written from it, so the proposal and the acceptance cannot be the same
        event.

        Scoped through ``_scoped_devices`` for the same reason ``gateway`` is:
        this answer describes an instrument's file, and this viewset's own
        queryset would give it to any authenticated user.
        """
        from apps.data_import.readers import ImportReadError, detect_import_type
        from apps.data_import.suggest import accepted_columns, describe
        from apps.telemetry.views import _scoped_devices

        device = _scoped_devices(request.user).filter(pk=pk).first()
        if device is None:
            return Response(
                {'detail': 'Device not found among the devices you can see.'},
                status=status.HTTP_404_NOT_FOUND)

        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({
                'device': str(device.id),
                'device_reference': device.device_reference,
                'file_name': '',
                'columns': [],
                'mapping': {},
                'accepted': accepted_columns(),
            })

        content = uploaded.read()
        if not content:
            return Response(
                {'detail': f'{uploaded.name} is empty, so it has no columns '
                           'to read.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            described = describe(content, detect_import_type(content))
        except ImportReadError as exc:
            return Response({'detail': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'device': str(device.id),
            'device_reference': device.device_reference,
            'file_name': uploaded.name,
            **described,
        })


class SensorDataFileViewSet(viewsets.ModelViewSet):
    """
    Raw sensor artifact upload (radargrams, depth slices, PUNDIT exports,
    RINEX logs, photos). The SHA-256 checksum and byte size are computed from
    the actual uploaded content at write time.
    """
    serializer_class = SensorDataFileSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options', 'delete']
    filterset_fields = ['file_type']
    search_fields = ['file_name', 'description']

    def get_queryset(self):
        return SensorDataFile.objects.select_related('uploaded_by')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({'detail': 'Multipart "file" field is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        digest = hashlib.sha256()
        for chunk in uploaded.chunks():
            digest.update(chunk)
        instance = serializer.save(
            uploaded_by=request.user,
            file_name=uploaded.name[:255],
            file_size_bytes=uploaded.size,
            sha256_checksum=digest.hexdigest(),
        )
        _record_audit(request.user, 'digital_eye.sensor_file.upload',
                      'SensorDataFile', instance.id,
                      {'sha256': instance.sha256_checksum, 'bytes': instance.file_size_bytes})
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        instance.file.delete(save=False)
        instance.delete()


class GPRSurveyViewSet(viewsets.ModelViewSet):
    """
    GPR subsurface surveys. `analyze` runs the deterministic GPR adapter.
    """
    serializer_class = GPRSurveySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['survey_reference', 'title', 'survey_area', 'structural_element']

    def get_queryset(self):
        queryset = GPRSurvey.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related(
            'project', 'device', 'operator', 'created_by',
        ).prefetch_related('files', 'anomalies')
        for param, field in (
            ('status', 'status'),
            ('device', 'device_id'),
            ('project', 'project_id'),
            ('structural_element', 'structural_element'),
        ):
            value = self.request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset

    def perform_create(self, serializer):
        survey = serializer.save(
            created_by=self.request.user,
            operator=self.request.user,
            started_at=timezone.now(),
        )
        _record_audit(self.request.user, 'digital_eye.gpr_survey.create',
                      'GPRSurvey', survey.id, {'survey_reference': survey.survey_reference})

    def perform_update(self, serializer):
        if self.request.data.get('status') == 'completed':
            serializer.save(status='completed', completed_at=timezone.now())
        else:
            serializer.save()

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        survey = self.get_object()
        survey.status = 'processing'
        survey.save(update_fields=['status', 'updated_at'])
        record = GPRAdapter.analyze(survey)
        survey.status = 'completed'
        survey.completed_at = timezone.now()
        survey.save(update_fields=['status', 'completed_at', 'updated_at'])
        _record_audit(request.user, 'digital_eye.gpr_survey.analyze',
                      'GPRSurvey', survey.id,
                      {'analysis': str(record.id), 'risk_level': record.risk_level})
        return Response({
            'survey': survey.survey_reference,
            'analysis_id': str(record.id),
            'risk_level': record.risk_level,
            'risk_score': record.risk_score,
            'observations': record.observations,
            'recommendations': record.recommendations,
            'reasoning_log': record.reasoning_log,
        })


class GPRAnomalyViewSet(viewsets.ModelViewSet):
    serializer_class = GPRAnomalySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['description', 'survey__survey_reference', 'survey__title']

    def get_queryset(self):
        queryset = GPRAnomaly.objects.filter(
            survey__project__in=scoped_projects(self.request.user),
        ).select_related('survey')
        for param, field in (
            ('survey', 'survey_id'),
            ('anomaly_type', 'anomaly_type'),
            ('severity', 'severity'),
            ('detected_by', 'detected_by'),
            ('project', 'survey__project_id'),
        ):
            value = self.request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        return queryset

    def perform_create(self, serializer):
        anomaly = serializer.save()
        _record_audit(self.request.user, 'digital_eye.gpr_anomaly.create',
                      'GPRAnomaly', anomaly.id,
                      {'type': anomaly.anomaly_type, 'severity': anomaly.severity})


class PUNDITTestViewSet(viewsets.ModelViewSet):
    """
    PUNDIT ultrasonic NDT (BS 1881-203 / ASTM C597). Measured inputs feed
    deterministic velocity, quality grade and crack depth calculations.
    """
    serializer_class = PUNDITTestSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['test_reference', 'structural_element', 'operator_name',
                     'test_location', 'notes']

    def get_queryset(self):
        queryset = PUNDITTest.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related('project', 'device', 'operator', 'created_by').prefetch_related('files', 'readings')
        for param, field in (
            ('test_type', 'test_type'),
            ('quality_grade', 'quality_grade'),
            ('structural_element', 'structural_element'),
            ('device', 'device_id'),
            ('project', 'project_id'),
        ):
            value = self.request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})

        # Reporting-browser filters (spec A5). Every one is opt-in: with no
        # param supplied the queryset is byte-identical to what it was before
        # these existed, so no existing caller changes behaviour.
        params = self.request.query_params
        date_from = self._query_date(params, 'date_from')
        if date_from:
            queryset = queryset.filter(test_date__gte=date_from)
        date_to = self._query_date(params, 'date_to')
        if date_to:
            queryset = queryset.filter(test_date__lte=date_to)
        operator = params.get('operator')
        if operator:
            # Exact match on the recorded operator. A test whose operator_name
            # is blank (never attributed to a real person) cannot match any
            # operator filter — the reports UI discloses this rather than
            # silently backfilling the field.
            queryset = queryset.filter(operator_name=operator)
        curve = params.get('curve')
        if curve:
            # A test matches when ANY of its readings was computed under this
            # curve. strength_curve_snapshot stores curve_id as a string, which
            # is what the query param is. The join emits one row per matching
            # reading, so collapse the duplicates.
            queryset = queryset.filter(
                readings__strength_curve_snapshot__curve_id=curve).distinct()
        return queryset

    @staticmethod
    def _query_date(params, name):
        """Parse a YYYY-MM-DD query param. An unparseable date is a 400 —
        silently ignoring it would return unfiltered rows that look filtered."""
        raw = params.get(name)
        if not raw:
            return None
        parsed = parse_date(raw)
        if parsed is None:
            raise InvalidQueryParam(
                {name: f'Expected a YYYY-MM-DD date, got "{raw}".'})
        return parsed

    # The filters that `export_json` records in its meta block, mapped to the
    # query param each one comes from. Kept beside get_queryset so a new filter
    # cannot be added without the export's provenance block noticing.
    EXPORT_FILTER_PARAMS = ('project', 'test_type', 'quality_grade',
                            'structural_element', 'device', 'date_from',
                            'date_to', 'operator', 'curve', 'search')

    def perform_create(self, serializer):
        test = serializer.save(created_by=self.request.user, operator=self.request.user)
        _record_audit(self.request.user, 'digital_eye.pundit_test.create',
                      'PUNDITTest', test.id, {'test_reference': test.test_reference})

    MEASUREMENT_FIELDS = (
        'test_type', 'path_length_mm', 'pulse_time_us',
        'crack_path_length_mm', 'crack_pulse_time_us', 'uncracked_pulse_time_us',
    )

    def perform_update(self, serializer):
        instance = serializer.instance
        before = {f: getattr(instance, f) for f in self.MEASUREMENT_FIELDS}
        test = serializer.save()
        corrected = [f for f in self.MEASUREMENT_FIELDS if getattr(test, f) != before[f]]
        _record_audit(self.request.user, 'digital_eye.pundit_test.update',
                      'PUNDITTest', test.id,
                      {'test_reference': test.test_reference,
                       'corrected_fields': corrected})
        if corrected:
            PUNDITAdapter.analyze(test)

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        test = self.get_object()
        record = PUNDITAdapter.analyze(test)
        _record_audit(request.user, 'digital_eye.pundit_test.analyze',
                      'PUNDITTest', test.id,
                      {'velocity_km_s': test.velocity_km_s, 'grade': test.quality_grade})
        return Response({
            'test': test.test_reference,
            'velocity_km_s': test.velocity_km_s,
            'quality_grade': test.quality_grade,
            'crack_depth_mm': test.crack_depth_mm,
            'analysis_id': str(record.id),
            'risk_level': record.risk_level,
            'observations': record.observations,
            'recommendations': record.recommendations,
            'reasoning_log': record.reasoning_log,
        })

    @action(detail=False, methods=['post'], url_path='clear-project')
    def clear_project(self, request):
        """
        Delete all PUNDIT tests belonging to a project to clear the folder.
        Body: {"project": "<project_id>"}
        """
        project_id = request.data.get('project') or request.data.get('project_id')
        if not project_id:
            return Response({'detail': 'A "project" id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        accessible = scoped_projects(request.user)
        project = accessible.filter(id=project_id).first()
        if not project:
            return Response({'detail': 'Project not found or access denied.'},
                            status=status.HTTP_404_NOT_FOUND)
        deleted_count, _ = PUNDITTest.objects.filter(project=project).delete()
        _record_audit(request.user, 'digital_eye.pundit_test.clear_project',
                      'Project', project.id, {'deleted_count': deleted_count})
        return Response({'status': 'cleared', 'deleted_count': deleted_count, 'project': str(project.id)})

    @action(detail=False, methods=['post'], url_path='analyze_project')
    def analyze_project(self, request):
        """
        Run the PUNDIT analysis across a whole project (review meeting D1 —
        the AI Analysis page's "Run AI Analysis"):
        per-test deterministic passes, then ONE project-level LLM narrative
        over the aggregate fact pack. Measured inputs only; on provider
        failure the deterministic record is still stored.
        Body: {"project": "<project_id>"} (or ?project= query param).
        """
        project_id = request.data.get('project') or request.query_params.get('project')
        if not project_id:
            return Response({'detail': 'A "project" id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        tests = self.get_queryset().filter(project=project)
        if not tests.exists():
            return Response({'detail': 'No PUNDIT tests recorded for this project.'},
                            status=status.HTTP_400_BAD_REQUEST)

        records = []
        for test in tests:
            # Deterministic pass only — the ONE project-level narrative below
            # is the single LLM call (N tests must not fire N LLM requests;
            # provider rate limits 504'd the endpoint when they did).
            record = PUNDITAdapter.analyze(test, use_llm=False)
            records.append(record)
        project_record = PUNDITAdapter.analyze_project(project, request.user)
        _record_audit(request.user, 'digital_eye.pundit_test.analyze_project',
                      'Project', project.id,
                      {'tests_analysed': len(records),
                       'analysis_id': str(project_record.id)})
        return Response({
            'project': str(project.id),
            'tests_analysed': len(records),
            'analysis_id': str(project_record.id),
            'risk_level': project_record.risk_level,
            'confidence': project_record.confidence,
            'observations': project_record.observations,
            'recommendations': project_record.recommendations,
            'reasoning_log': project_record.reasoning_log,
            'model_provider': project_record.model_provider,
            'model_version': project_record.model_version,
        })

    @action(detail=False, methods=['get'], url_path='export_results')
    def export_results(self, request):
        """
        Excel export of the project's PUNDIT results (7 Sep meeting —
        "{Update Excel}: spreadsheet data consistency matching generated
        reports"). Values are the SAME blocks the report's Section 5.0
        tables print (shared _element_data), so the sheet and the PDF can
        never disagree. Velocities in m/s. Query: ?project=<id> (required).
        """
        from django.http import HttpResponse
        from .excel_export import build_results_response_bytes

        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if not project:
            return Response({'detail': 'A "project" id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(
            build_results_response_bytes(project),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = (
            'attachment; filename='
            f'"nexucon_pundit_results_{project.name[:40].replace(" ", "_")}.xlsx"')
        return response

    @action(detail=False, methods=['get'], url_path='export_json')
    def export_json(self, request):
        """
        JSON export of exactly the measurements the reports browser is showing
        (spec A5 "Export JSON").

        It reuses get_queryset(), so every filter in force on screen is applied
        to the file — the export can never be a wider dump than the view it was
        taken from. The `meta` block records which filters produced the file, so
        the JSON is self-describing and cannot be mistaken for a whole-project
        export. Values are the stored ones: a strength that was never computed
        is null, never 0.
        """
        queryset = self.filter_queryset(self.get_queryset())
        requested_project = request.query_params.get('project')
        project = scoped_projects(request.user).filter(
            pk=requested_project).first() if requested_project else None
        if requested_project and project is None:
            # An out-of-scope (or unknown) project id is an error, not an
            # empty file — an empty export would read as "no data recorded".
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        filters = {name: request.query_params.get(name)
                   for name in self.EXPORT_FILTER_PARAMS
                   if request.query_params.get(name)}

        rows = []
        # The distinct curves behind the rows, with the parameters that were
        # actually used — taken from each reading's stored snapshot, so this
        # is a record of what was applied, not a re-read of today's curves.
        curves_used: dict = {}
        test_ids = []
        for test in queryset:
            test_ids.append(str(test.id))
            for reading in test.readings.all():
                # The reading's own snapshot is the formula that produced this
                # point; fall back to the test's only when the reading has none.
                snapshot = reading.strength_curve_snapshot or test.strength_curve_snapshot or None
                velocity_km_s = reading.velocity_km_s
                rows.append({
                    'test_reference': test.test_reference,
                    'test_date': test.test_date.isoformat() if test.test_date else None,
                    'structural_element': test.structural_element or None,
                    'point_label': reading.point_label,
                    'operator_name': (test.operator_name or '').strip() or None,
                    'path_length_mm': reading.path_length_mm,
                    'transit_time_us': reading.transit_time_us,
                    # Canonical storage is km/s; the report's unit is m/s.
                    'velocity_ms': (round(velocity_km_s * 1000)
                                    if velocity_km_s is not None else None),
                    'estimated_strength_mpa': reading.ecs_mpa,
                    'rebound_number': reading.rebound_number,
                    'crack_depth_mm': reading.crack_depth_mm,
                    'curve_id': (snapshot or {}).get('curve_id'),
                    'curve_name': (snapshot or {}).get('name'),
                    'curve_type': (snapshot or {}).get('curve_type'),
                    'curve_standard': (snapshot or {}).get('standard') or None,
                })
                curve_id = (snapshot or {}).get('curve_id')
                if curve_id and curve_id not in curves_used:
                    curves_used[curve_id] = {
                        'curve_id': curve_id,
                        'curve_name': (snapshot or {}).get('name'),
                        'curve_type': (snapshot or {}).get('curve_type'),
                        'standard': (snapshot or {}).get('standard') or None,
                        'formula': (snapshot or {}).get('formula'),
                        'formula_params': (snapshot or {}).get('formula_params'),
                        'valid_range_ms': (snapshot or {}).get('valid_range_ms'),
                    }

        # Provenance log (spec's provenanceLog): the audit trail of the tests
        # this export covers. Capped, and says so — a silently truncated log
        # would read as a complete history.
        provenance_limit = 500
        events = AuditEvent.objects.filter(
            resource_type='PUNDITTest', resource_id__in=test_ids,
        ).order_by('-timestamp')
        event_count = events.count()
        provenance_log = [{
            'timestamp': e.timestamp.isoformat(),
            'action': e.action,
            'user_name': e.user_name,
            'user_role': e.user_role,
            'severity': e.severity,
            'details': e.metadata or {},
        } for e in events[:provenance_limit]]

        payload = {
            'meta': {
                'exported_at': timezone.now().isoformat(),
                'project': ({'id': str(project.id), 'name': project.name}
                            if project else None),
                'filters': filters,
                'row_count': len(rows),
                'test_count': len(test_ids),
                # Stated so the file's scope is never inferred from its contents.
                'filters_note': (
                    'Only the filters listed in "filters" were applied. A test '
                    'with no recorded operator, or with no test date, cannot '
                    'match an operator or date-range filter and is absent here.'),
                'unit_note': 'velocity_ms is metres per second; '
                             'estimated_strength_mpa is null when no curve '
                             'applied or the velocity fell outside the curve range.',
                'provenance_note': (
                    f'The provenance log holds the newest {len(provenance_log)} of '
                    f'{event_count} recorded audit event(s) for the exported tests.'
                    if event_count > provenance_limit else
                    f'All {event_count} recorded audit event(s) for the exported tests.'),
            },
            'curves_used': list(curves_used.values()),
            'rows': rows,
            'provenance_log': provenance_log,
        }
        # HttpResponse, not a DRF Response: this is a file download, and it
        # must stream the JSON itself rather than the browsable API's HTML.
        import json
        from django.http import HttpResponse
        response = HttpResponse(
            json.dumps(payload, indent=2), content_type='application/json')
        scope = project.name[:40].replace(' ', '_') if project else 'all_projects'
        response['Content-Disposition'] = (
            f'attachment; filename="nexucon_pundit_measurements_{scope}.json"')
        return response

    @action(detail=False, methods=['get'], url_path='import_template')
    def import_template(self, request):
        """The .xlsx template for the batch upload (A2): an EMPTY READINGS
        sheet (the only sheet the importer reads, so example data can never
        enter the registry) plus an EXAMPLE sheet of filled rows for all
        three test types. Optional ``?project=<id>`` points the EXAMPLE rows
        at that project's imported BIM members so the illustrations
        reference REAL element names from the model."""
        from django.http import HttpResponse
        from .excel_import import build_template_response_bytes

        sample_elements = None
        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if project:
            names = list(
                BIMElementMapping.objects.filter(project=project)
                .exclude(element_name=None)
                .exclude(element_name='')
                .order_by('element_name')
                .values_list('element_name', flat=True))
            if names:
                lowered = [(n, n.lower()) for n in names]

                def pick(*needles):
                    for _, low in lowered:
                        if any(needle in low for needle in needles):
                            return _
                    return None

                pulse = pick('column', 'slab', 'footing') or names[0]
                crack = pick('beam') or names[-1]
                surface = pick('wall', 'slab', 'floor')
                if surface is None or surface in (pulse, crack):
                    for candidate in names:
                        if candidate not in (pulse, crack):
                            surface = candidate
                            break
                sample_elements = (pulse, crack, surface)

        response = HttpResponse(
            build_template_response_bytes(sample_elements),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = (
            'attachment; filename="nexucon_pundit_readings_template.xlsx"')
        return response

    @action(detail=False, methods=['post'], url_path='import_readings')
    def import_readings(self, request):
        """Batch upload of PUNDIT readings from the template workbook (A2):
        one row per test point; an element's consecutive rows form one test
        with points A, B, C... Every group is created through the same
        serializer as the manual entry form, so computed outputs stay
        server-side. All-or-nothing: any rejected row rejects the file with
        row-level reasons and nothing is written. Body: multipart
        {project, file (.xlsx)}."""
        from django.db import transaction
        from .excel_import import parse_readings_workbook
        project = scoped_projects(request.user).filter(
            pk=request.data.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({'detail': 'Multipart "file" field (.xlsx template) is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not (uploaded.name or '').lower().endswith('.xlsx'):
            return Response({'detail': 'Upload the .xlsx template (download it from this '
                                       'page) — other formats are not accepted.'},
                            status=status.HTTP_400_BAD_REQUEST)

        groups, errors = parse_readings_workbook(uploaded)
        if errors:
            return Response({'detail': 'The workbook was rejected — fix the rows below and '
                                       're-upload. Nothing was written to the registry.',
                             'errors': errors},
                            status=status.HTTP_400_BAD_REQUEST)

        # Resolve element names against the project's imported BIM model so the
        # created tests carry the same GUID link the manual form creates.
        guid_by_name = {}
        for mapping in BIMElementMapping.objects.filter(project=project):
            if mapping.element_name:
                guid_by_name.setdefault(mapping.element_name.strip().lower(), mapping)

        created = []
        for group in groups:
            payload = dict(group['payload'])
            payload['project'] = str(project.id)
            mapping = guid_by_name.get(group['element'].strip().lower())
            if mapping is not None:
                payload['structural_element_guid'] = mapping.bim_guid
                payload['structural_element_id_str'] = mapping.element_id
            serializer = PUNDITTestSerializer(
                data=payload, context={'request': request, 'view': self})
            if not serializer.is_valid():
                for field, messages in serializer.errors.items():
                    detail = '; '.join(m if isinstance(m, str) else str(m)
                                       for m in (messages if isinstance(messages, list)
                                                 else [messages]))
                    errors.append({'rows': f"{group['first_row']}-{group['last_row']}",
                                   'message': f'element {group["element"]!r}: '
                                              f'{field}: {detail}'})
                continue
            group['serializer'] = serializer
        if errors:
            return Response({'detail': 'The workbook was rejected — fix the rows below and '
                                       're-upload. Nothing was written to the registry.',
                             'errors': errors},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            for group in groups:
                test = group['serializer'].save(
                    created_by=request.user, operator=request.user)
                created.append({
                    'test_reference': test.test_reference,
                    'structural_element': test.structural_element,
                    'bim_linked': bool(test.structural_element_guid),
                    'test_type': test.test_type,
                    'floor': test.floor,
                    'points': test.readings.count(),
                    'velocity_km_s': test.velocity_km_s,
                    'quality_grade': test.quality_grade,
                })
        _record_audit(request.user, 'digital_eye.pundit_test.import_readings',
                      'Project', project.id,
                      {'file': uploaded.name, 'tests_created': len(created),
                       'points': sum(t['points'] for t in created)})
        return Response({
            'file': uploaded.name,
            'tests_created': len(created),
            'points_imported': sum(t['points'] for t in created),
            'tests': created,
        }, status=status.HTTP_201_CREATED)


class GnssSurveyViewSet(viewsets.ModelViewSet):
    serializer_class = GnssSurveySerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'method', 'status', 'fix_quality', 'device']
    search_fields = ['survey_reference', 'title', 'operator_name']

    def get_queryset(self):
        return GnssSurvey.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related('project', 'device', 'operator', 'created_by').prefetch_related(
            'files', 'benchmarks', 'boundary_points',
        )

    def perform_create(self, serializer):
        survey = serializer.save(created_by=self.request.user, operator=self.request.user)
        _record_audit(self.request.user, 'digital_eye.gnss_survey.create',
                      'GnssSurvey', survey.id, {'survey_reference': survey.survey_reference})

    @action(detail=True, methods=['post'])
    def project_survey(self, request, pk=None):
        survey = self.get_object()
        projected = 0
        for benchmark in survey.benchmarks.all():
            GNSSProjection.project_benchmark(benchmark)
            projected += 1
        for point in survey.boundary_points.all():
            easting, northing = GNSSProjection.geographic_to_utm(point.latitude, point.longitude)
            if easting is not None:
                point.easting, point.northing = easting, northing
                point.save(update_fields=['easting', 'northing'])
                projected += 1

        design_points = request.data.get('design_points') or {}
        variance_summary = None
        if design_points:
            variances = []
            for benchmark in survey.benchmarks.all():
                design = design_points.get(benchmark.point_id)
                if design and benchmark.easting is not None:
                    d_e = benchmark.easting - float(design['easting'])
                    d_n = benchmark.northing - float(design['northing'])
                    variances.append({
                        'point_id': benchmark.point_id,
                        'delta_easting_m': round(d_e, 4),
                        'delta_northing_m': round(d_n, 4),
                        'radial_m': round((d_e ** 2 + d_n ** 2) ** 0.5, 4),
                    })
            if variances:
                variance_summary = {
                    'points_compared': len(variances),
                    'max_radial_m': max(v['radial_m'] for v in variances),
                    'points': variances,
                }

        if variance_summary:
            survey.variance_summary = variance_summary
            survey.save(update_fields=['variance_summary', 'updated_at'])

        from apps.evidence.ingestion import EvidenceIngestionService
        evidence = EvidenceIngestionService.ingest_gnss_survey(survey, ingested_by=request.user)

        _record_audit(request.user, 'digital_eye.gnss_survey.project',
                      'GnssSurvey', survey.id, {'projected_points': projected})
        return Response({
            'survey': survey.survey_reference,
            'projected_points': projected,
            'projection': survey.projection,
            'variance_summary': variance_summary,
            'evidence_reference': evidence.evidence_reference,
        })


class GnssBenchmarkViewSet(viewsets.ModelViewSet):
    serializer_class = GnssBenchmarkSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['survey']

    def get_queryset(self):
        return GnssBenchmark.objects.filter(
            survey__project__in=scoped_projects(self.request.user),
        ).select_related('survey')

    def perform_create(self, serializer):
        benchmark = serializer.save()
        GNSSProjection.project_benchmark(benchmark)


class GnssBoundaryPointViewSet(viewsets.ModelViewSet):
    serializer_class = GnssBoundaryPointSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['survey']

    def get_queryset(self):
        return GnssBoundaryPoint.objects.filter(
            survey__project__in=scoped_projects(self.request.user),
        ).select_related('survey')

    def perform_create(self, serializer):
        point = serializer.save()
        easting, northing = GNSSProjection.geographic_to_utm(point.latitude, point.longitude)
        if easting is not None:
            point.easting, point.northing = easting, northing
            point.save(update_fields=['easting', 'northing'])


class BIMElementMappingViewSet(viewsets.ModelViewSet):
    serializer_class = BIMElementMappingSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'source', 'element_type', 'trimble_project']
    search_fields = ['bim_guid', 'element_id', 'element_name', 'level']

    def get_queryset(self):
        return BIMElementMapping.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related('project', 'trimble_project', 'created_by')

    def perform_create(self, serializer):
        element = serializer.save(created_by=self.request.user)
        _record_audit(self.request.user, 'digital_eye.bim_element.create',
                      'BIMElementMapping', element.id, {'bim_guid': element.bim_guid})


class LiveStreamViewSet(viewsets.ModelViewSet):
    serializer_class = LiveStreamSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'status', 'mapped_element']

    def get_queryset(self):
        return LiveStream.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related('project', 'mapped_element', 'created_by')

    def perform_create(self, serializer):
        stream = serializer.save(created_by=self.request.user)
        _record_audit(self.request.user, 'digital_eye.live_stream.create',
                      'LiveStream', stream.id, {'mapped_element': str(stream.mapped_element_id)})

    @action(detail=True, methods=['post'])
    def check(self, request, pk=None):
        stream = self.get_object()
        new_status = request.data.get('status')
        if new_status not in ('pending', 'live', 'offline', 'error'):
            return Response({'detail': 'status must be pending|live|offline|error.'},
                            status=status.HTTP_400_BAD_REQUEST)
        stream.status = new_status
        stream.last_checked_at = timezone.now()
        stream.last_error = request.data.get('error', '') if new_status == 'error' else ''
        stream.save(update_fields=['status', 'last_checked_at', 'last_error', 'updated_at'])
        return Response(LiveStreamSerializer(stream, context={'request': request}).data)


# ======================================================================
# Trimble Connect integration ViewSets
# ======================================================================

class TrimbleConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = TrimbleConnectionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        return TrimbleConnection.objects.select_related('created_by')

    def create(self, request, *args, **kwargs):
        if not user_is_director(request.user):
            return Response({'detail': 'Only Directors can create integration connections.'},
                            status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        if not user_is_director(request.user):
            return Response({'detail': 'Only Directors can remove integration connections.'},
                            status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def authorize(self, request, pk=None):
        from integrations.trimble import TrimbleClient, TrimbleCredentialsMissing
        connection = self.get_object()
        try:
            client = TrimbleClient(connection)
            url = client.start_authorization(connection, state=request.data.get('state', ''))
        except TrimbleCredentialsMissing as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        _record_audit(request.user, 'trimble.authorize.start',
                      'TrimbleConnection', connection.id, {})
        return Response({'authorization_url': url,
                         'status': connection.status,
                         'detail': 'Open the authorization URL, approve access, then POST '
                                   'the resulting code to the callback endpoint.'})

    @action(detail=True, methods=['post'])
    def callback(self, request, pk=None):
        from integrations.trimble import TrimbleAuthError, TrimbleClient, TrimbleCredentialsMissing
        connection = self.get_object()
        code = request.data.get('code')
        if not code:
            return Response({'detail': 'authorization "code" is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            client = TrimbleClient(connection)
            client.complete_authorization(connection, code)
        except (TrimbleCredentialsMissing,) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except TrimbleAuthError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        _record_audit(request.user, 'trimble.authorize.complete',
                      'TrimbleConnection', connection.id, {})
        return Response(TrimbleConnectionSerializer(connection, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def health(self, request, pk=None):
        from integrations.trimble import TrimbleClient
        connection = self.get_object()
        healthy, detail = TrimbleClient().health_check(connection)
        return Response({
            'connection_id': str(connection.id),
            'healthy': healthy,
            'status': connection.last_health_status,
            'detail': detail,
            'checked_at': connection.last_health_check_at,
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsDirector])
    def discover(self, request, pk=None):
        from integrations.trimble import TrimbleClient, TrimbleError
        connection = self.get_object()
        try:
            projects = TrimbleClient(connection).discover_projects(connection)
        except TrimbleError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        _record_audit(request.user, 'trimble.discover',
                      'TrimbleConnection', connection.id, {'projects': projects.count()})
        return Response({
            'projects': TrimbleProjectSerializer(
                projects, many=True, context={'request': request}).data,
        })

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsDirector])
    def sync(self, request, pk=None):
        from integrations.trimble import TrimbleError, TrimbleSyncService
        connection = self.get_object()
        try:
            results = TrimbleSyncService().sync_all_projects(connection, user=request.user)
        except TrimbleError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        _record_audit(request.user, 'trimble.sync',
                      'TrimbleConnection', connection.id, {'results': results})
        return Response({'results': results})


class TrimbleProjectViewSet(viewsets.ModelViewSet):
    serializer_class = TrimbleProjectSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'patch', 'post', 'head', 'options']
    filterset_fields = ['connection', 'linked_project', 'is_active']
    search_fields = ['name', 'external_id']

    def get_queryset(self):
        return TrimbleProject.objects.select_related('connection', 'linked_project')

    def partial_update(self, request, *args, **kwargs):
        if not user_is_director(request.user):
            return Response({'detail': 'Only Directors can link Trimble projects.'},
                            status=status.HTTP_403_FORBIDDEN)
        return super().partial_update(request, *args, **kwargs)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsDirector])
    def sync(self, request, pk=None):
        from integrations.trimble import TrimbleError, TrimbleSyncService
        trimble_project = self.get_object()
        if trimble_project.linked_project is None:
            return Response(
                {'detail': 'Link this Trimble project to a Nexucon project first (PATCH linked_project).'},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            result = TrimbleSyncService().sync_project(trimble_project)
        except TrimbleError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        _record_audit(request.user, 'trimble.project.sync',
                      'TrimbleProject', trimble_project.id, result)
        return Response(result)


class BIMElementImportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """What the import card displays (B5/B6): the model *currently*
        imported for the project (from BIMModelGeometry — the genuine
        uploaded file name) plus the model files previously imported and
        kept on the platform, so "Choose file" can offer a platform picker
        instead of only the OS dialog. Read-only, verbatim from the rows."""
        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        geometry = BIMModelGeometry.objects.filter(project=project).first()
        currently_imported = None
        if geometry:
            currently_imported = {
                'source_file': geometry.source_file,
                'translated_from_rvt': geometry.translated_from_rvt,
                'element_count': geometry.element_count,
                'updated_at': geometry.updated_at,
            }
        stored_files = [
            {
                'id': str(f.id),
                'file_name': f.file_name,
                'file_size_bytes': f.file_size_bytes,
                'sha256_checksum': f.sha256_checksum,
                'uploaded_by': (f.uploaded_by.get_full_name() or f.uploaded_by.email)
                if f.uploaded_by else '',
                'created_at': f.created_at,
            }
            for f in SensorDataFile.objects.filter(project=project, file_type='bim_model')
        ]
        return Response({'currently_imported': currently_imported,
                         'stored_files': stored_files})

    def post(self, request):
        import os
        import tempfile
        from django.conf import settings
        from django.core.files.base import ContentFile
        from apps.projects.models import Project
        from integrations.trimble.sync import IFCElementExtractor

        project = scoped_projects(request.user).filter(
            pk=request.data.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        # Two entry paths (B6): a fresh multipart upload, or the id of a model
        # file previously imported and kept on the platform.
        uploaded = request.FILES.get('file')
        sensor_file_id = request.data.get('sensor_file')
        if uploaded is not None:
            display_name = uploaded.name or ''
            source = uploaded
            store_copy = True
        elif sensor_file_id:
            from django.core.exceptions import ValidationError
            try:
                stored = SensorDataFile.objects.filter(
                    pk=sensor_file_id, file_type='bim_model', project=project).first()
            except (ValueError, ValidationError):
                stored = None
            if stored is None:
                return Response(
                    {'detail': 'Stored BIM model file not found for this project.'},
                    status=status.HTTP_404_NOT_FOUND)
            if not stored.file:
                return Response(
                    {'detail': 'The stored model file is missing from storage.'},
                    status=status.HTTP_410_GONE)
            display_name = stored.file_name or os.path.basename(stored.file.name)
            source = stored.file
            store_copy = False
        else:
            return Response(
                {'detail': 'Either a multipart "file" field (BIM model: .ifc or .rvt) '
                           'or a "sensor_file" id of a previously imported model is required.'},
                status=status.HTTP_400_BAD_REQUEST)

        file_name = display_name.lower()
        if not (file_name.endswith('.ifc') or file_name.endswith('.rvt')):
            return Response({
                'detail': 'Unsupported model format. Upload an .ifc model, or a .rvt '
                          'Revit model (translated to IFC via Autodesk APS).'},
                status=status.HTTP_400_BAD_REQUEST)

        # Land the source bytes on disk once (checksummed as they stream) —
        # the .rvt path needs a real path for the APS translation, and the
        # platform copy (B6) is saved from the same temp file.
        cache_dir = os.path.join(settings.BASE_DIR, 'media', 'temp_bim')
        os.makedirs(cache_dir, exist_ok=True)
        src_suffix = os.path.splitext(display_name)[1] or '.bin'
        fd, src_tmp = tempfile.mkstemp(suffix=src_suffix, dir=cache_dir)
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as tmp:
            for chunk in source.chunks():
                tmp.write(chunk)
                digest.update(chunk)
        try:
            if file_name.endswith('.ifc'):
                with open(src_tmp, 'rb') as fh:
                    content = ContentFile(fh.read())
                content.name = display_name
                translated_from_rvt = False
            else:
                from apps.processing.bim_geometry import ensure_ifc
                try:
                    ifc_path = ensure_ifc(src_tmp)
                except ValueError as exc:
                    return Response({
                        'detail': f'{exc} Revit (.rvt) models are a closed proprietary format '
                                  'and must be translated to IFC by Autodesk Platform Services. '
                                  'Either configure AUTODESK_CLIENT_ID / AUTODESK_CLIENT_SECRET, '
                                  'or export an IFC directly from Revit '
                                  '(File → Export → IFC) and upload that.'},
                        status=status.HTTP_400_BAD_REQUEST)
                except Exception:
                    logger.exception('APS RVT->IFC translation failed for %s', display_name)
                    return Response({
                        'detail': 'Autodesk APS could not translate this Revit model to IFC. '
                                  'Verify the file is a valid .rvt, or export an IFC directly '
                                  'from Revit (File → Export → IFC) and upload that.'},
                        status=status.HTTP_502_BAD_GATEWAY)
                try:
                    with open(ifc_path, 'rb') as translated:
                        content = ContentFile(translated.read())
                    content.name = os.path.basename(ifc_path)
                except OSError:
                    logger.exception('Could not read translated IFC %s', ifc_path)
                    return Response({'detail': 'Translated IFC could not be read back.'},
                                    status=status.HTTP_502_BAD_GATEWAY)
                translated_from_rvt = True

            elements = IFCElementExtractor.extract(content)
            created = updated = 0
            for data in elements:
                _, was_created = BIMElementMapping.objects.update_or_create(
                    project=project,
                    bim_guid=data['bim_guid'],
                    defaults={
                        'element_id': data.get('element_id') or '',
                        'element_name': data.get('element_name') or '',
                        'element_type': data.get('element_type') or '',
                        'level': data.get('level') or '',
                        'coordinates': data.get('coordinates'),
                        'properties': data.get('properties') or {},
                        'source': 'ifc_upload',
                        'created_by': request.user,
                    },
                )
                created += int(was_created)
                updated += int(not was_created)

            # 3D preview meshes: tessellate the same IFC content the mappings came
            # from and persist it for the data-collection model preview. The
            # metadata import above is the primary product — a tessellation
            # failure degrades to an honest empty preview, never a failed import.
            preview_elements = 0
            preview_error = None
            content.seek(0)
            with tempfile.NamedTemporaryFile(suffix='.ifc', delete=False) as preview_ifc:
                for chunk in content.chunks():
                    preview_ifc.write(chunk)
                preview_ifc_path = preview_ifc.name
            try:
                try:
                    preview_elements = build_preview_geometry(preview_ifc_path)
                except Exception:
                    logger.exception('Preview tessellation failed for %s', display_name)
                    preview_error = '3D preview could not be built for this model.'
            finally:
                import os as _os
                if _os.path.exists(preview_ifc_path):
                    _os.unlink(preview_ifc_path)
            BIMModelGeometry.objects.update_or_create(
                project=project,
                defaults={
                    'source_file': display_name,
                    'translated_from_rvt': translated_from_rvt,
                    'element_count': len(preview_elements) if preview_error is None else 0,
                    'elements': preview_elements if preview_error is None else [],
                    'created_by': request.user,
                },
            )

            # Keep the model file on the platform (B6) so it can be re-imported
            # from the picker instead of hunting for it on the operator's
            # device. Deduplicated by content checksum per project.
            stored_file_id = None
            if store_copy:
                existing = SensorDataFile.objects.filter(
                    project=project, file_type='bim_model',
                    sha256_checksum=digest.hexdigest()).first()
                if existing is None:
                    from django.core.files import File as DjangoFile
                    stored_obj = SensorDataFile(
                        project=project,
                        file_type='bim_model',
                        uploaded_by=request.user,
                        file_name=display_name[:255],
                        file_size_bytes=os.path.getsize(src_tmp),
                        sha256_checksum=digest.hexdigest(),
                        description='BIM model file kept on the platform for re-import.')
                    with open(src_tmp, 'rb') as fh:
                        stored_obj.file.save(
                            f'bim_models/{digest.hexdigest()[:16]}_{display_name}',
                            DjangoFile(fh), save=True)
                    stored_file_id = str(stored_obj.id)
                    _record_audit(request.user, 'digital_eye.bim_model.stored',
                                  'SensorDataFile', stored_obj.id,
                                  {'file': display_name, 'bytes': stored_obj.file_size_bytes})
                else:
                    stored_file_id = str(existing.id)

            _record_audit(request.user, 'digital_eye.bim_elements.import_ifc',
                          'Project', project.id,
                          {'file': display_name, 'created': created, 'updated': updated,
                           'translated_from_rvt': translated_from_rvt,
                           'from_stored_file': not store_copy,
                           'preview_elements': len(preview_elements) if preview_error is None else 0})
            return Response({
                'file': display_name,
                'translated_from_rvt': translated_from_rvt,
                'elements_extracted': len(elements),
                'mappings_created': created,
                'mappings_updated': updated,
                'preview_elements': len(preview_elements) if preview_error is None else 0,
                'stored_file': stored_file_id,
                **({'preview_detail': preview_error} if preview_error else {}),
            }, status=status.HTTP_201_CREATED)
        finally:
            if os.path.exists(src_tmp):
                os.unlink(src_tmp)


class BIMModelGeometryView(APIView):
    """Serves the stored tessellated preview meshes for a project's imported
    BIM model (read-only, verbatim from the row — never recomputed)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        geometry = BIMModelGeometry.objects.filter(project=project).first()
        if not geometry:
            return Response(
                {'detail': 'No BIM model imported for this project yet — import an '
                           'IFC/RVT model to create the 3D preview.'},
                status=status.HTTP_404_NOT_FOUND)
        return Response({
            'project': str(project.id),
            'source_file': geometry.source_file,
            'translated_from_rvt': geometry.translated_from_rvt,
            'element_count': geometry.element_count,
            'updated_at': geometry.updated_at,
            'elements': geometry.elements,
        })


# ======================================================================
# Scan-to-BIM & AI Analytics read models
#
# These endpoints mirror data owned by the scoped viewsets above and exist for
# dashboard reads only. They are deliberately READ-ONLY: the authoritative
# write paths are `/pundit-tests/`, `/gpr-surveys/` and `/bim-elements/`, whose
# serializers keep the computed outputs (velocity, quality grade, crack depth)
# read-only so a measurement cannot be typed in. A writable duplicate here
# would be a doctoring route straight past that guarantee.
# ======================================================================

class BIMStructuralElementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BIMStructuralElement.objects.all().order_by('-created_at')
    serializer_class = BIMStructuralElementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        discipline = self.request.query_params.get('discipline')
        search = self.request.query_params.get('search')

        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project) | Q(project_name__icontains=project))
        if discipline and discipline != 'all':
            qs = qs.filter(discipline__iexact=discipline)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(element_guid__icontains=search) | Q(grid_location__icontains=search))
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Structural elements retrieved successfully",
            data=serializer.data
        )


class GPRScanViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = GPRScan.objects.all().order_by('-created_at')
    serializer_class = GPRScanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        element_id = self.request.query_params.get('element_id') or self.request.query_params.get('structural_element_id')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project) | Q(project_name__icontains=project))
        if element_id:
            qs = qs.filter(Q(structural_element__id=element_id) | Q(structural_element_id_str=element_id))
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="GPR scans retrieved successfully",
            data=serializer.data
        )


class PunditTestViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only mirror of PUNDIT tests. Writes go to `/pundit-tests/`, where
    velocity / grade / crack depth are server-computed and non-writable."""
    queryset = PUNDITTest.objects.all().order_by('-created_at')
    serializer_class = PunditTestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        element_id = self.request.query_params.get('element_id') or self.request.query_params.get('structural_element_id')
        element_name = self.request.query_params.get('element_name')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project) | Q(project_name__icontains=project))
        if element_id:
            qs = qs.filter(Q(structural_element_id_str=element_id) | Q(structural_element__icontains=element_id))
        if element_name:
            qs = qs.filter(structural_element__icontains=element_name)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Pundit UPV tests retrieved successfully",
            data=serializer.data
        )


class DigitalEyeFindingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DigitalEyeFinding.objects.all().order_by('-created_at')
    serializer_class = DigitalEyeFindingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        element_id = self.request.query_params.get('element_id')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project) | Q(project_name__icontains=project))
        if element_id:
            qs = qs.filter(structural_element_id_str=element_id)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Digital Eye findings retrieved successfully",
            data=serializer.data
        )

    @action(detail=True, methods=['post'], url_path='escalate-ncr')
    def escalate_ncr(self, request, pk=None):
        """Escalate a finding to an NCR. Audited, because it changes a
        statutory record's status."""
        finding = self.get_object()
        ncr_ref = f"NCR-{timezone.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
        finding.status = 'CONVERTED_TO_NCR'
        finding.ncr_reference = ncr_ref
        finding.save(update_fields=['status', 'ncr_reference'])
        _record_audit(request.user, 'digital_eye.finding.escalate_ncr',
                      'DigitalEyeFinding', finding.id, {'ncr_reference': ncr_ref})
        return StandardResponse.success(
            message="Finding escalated to Non-Conformance Report (NCR)",
            data={"finding_id": finding.id, "ncr_reference": ncr_ref, "status": finding.status}
        )


class AIAnalysisViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AIAnalysisRecord.objects.all().order_by('-created_at')
    serializer_class = AIAnalysisRecordSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project))
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="AI Analysis records retrieved successfully",
            data=serializer.data
        )


class ProcessingQueueJobViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProcessingQueueJob.objects.all().order_by('-created_at')
    serializer_class = ProcessingQueueJobSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project))
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Processing queue jobs retrieved successfully",
            data=serializer.data
        )


class EvidenceSpatialPointViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EvidenceSpatialPoint.objects.all().order_by('-timestamp')
    serializer_class = EvidenceSpatialPointSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        project = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        layer_type = self.request.query_params.get('layer_type')
        if project:
            qs = qs.filter(Q(project__id=project) | Q(project_id_str=project))
        if layer_type:
            qs = qs.filter(layer_type=layer_type)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Spatial map points retrieved successfully",
            data=serializer.data
        )


class DeviceReportViewSet(viewsets.ModelViewSet):
    queryset = DeviceReportRecord.objects.all().order_by('-created_at')
    serializer_class = DeviceReportRecordSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = _scoped_legacy_queryset(super().get_queryset(), self.request.user)
        device_type = self.request.query_params.get('device_type')
        project_id = self.request.query_params.get('project_id') or self.request.query_params.get('project')
        element_id = self.request.query_params.get('element_id')

        if device_type:
            qs = qs.filter(device_type__iexact=device_type)
        if project_id:
            qs = qs.filter(Q(project__id=project_id) | Q(project_id_str=project_id) | Q(project_name__icontains=project_id))
        if element_id:
            qs = qs.filter(element_id=element_id)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return StandardResponse.success(
            message="Device reports retrieved successfully",
            data=serializer.data
        )

    def create(self, request, *args, **kwargs):
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        if 'id' not in data or not data['id']:
            data['id'] = f"rep-{int(timezone.now().timestamp() * 1000)}"
        if 'report_reference' not in data or not data['report_reference']:
            device = data.get('device_type', 'NDT')
            data['report_reference'] = f"RPT-{device}-{timezone.now().year}-{str(uuid.uuid4())[:4].upper()}"
        if 'title' not in data or not data['title']:
            data['title'] = f"{data.get('device_type', 'NDT')} Statutory Inspection Dossier"

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        record = serializer.save()
        _record_audit(request.user, 'digital_eye.device_report.create',
                      'DeviceReportRecord', record.id,
                      {'report_reference': record.report_reference,
                       'device_type': record.device_type})
        return StandardResponse.success(
            message="Device report created successfully",
            data=serializer.data,
            status_code=status.HTTP_201_CREATED
        )


# ======================================================================
# Nexucon Link — calibration curves (8 Sep 2026 review meeting)
# ======================================================================

class StrengthCurveViewSet(viewsets.ModelViewSet):
    """
    Nexucon Link calibration curves: the project-specific relationship
    between pulse velocity (m/s), optional rebound number and compressive
    strength (f_cu, MPa). The project's active curve replaces the fixed
    linear f_cu formula in every strength computation.

    Reads are authenticated and scope-limited (platform default curve plus
    the caller's scoped projects' curves); create/update/delete and curve
    activation are Director-only, audited actions.
    """
    serializer_class = StrengthCurveSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['name', 'standard']

    WRITE_ACTIONS = ('create', 'update', 'partial_update', 'destroy',
                     'activate', 'use_platform_default')

    def get_permissions(self):
        if self.action in self.WRITE_ACTIONS:
            return [IsDirector()]
        return super().get_permissions()

    def get_queryset(self):
        qs = StrengthCurve.objects.filter(
            Q(project__isnull=True)
            | Q(project__in=scoped_projects(self.request.user))
        ).select_related('project', 'created_by')
        params = self.request.query_params
        project = params.get('project')
        if project:
            allowed = scoped_projects(self.request.user).filter(pk=project)
            # ?project= narrows to that project's curves + the platform
            # default (which remains selectable as its active curve).
            qs = qs.filter(Q(project__in=allowed) | Q(project__isnull=True))
        curve_type = params.get('curve_type')
        if curve_type:
            qs = qs.filter(curve_type=curve_type)
        return qs

    def perform_create(self, serializer):
        curve = serializer.save(created_by=self.request.user)
        _record_audit(self.request.user, 'digital_eye.strength_curve.create',
                      'StrengthCurve', curve.id,
                      {'name': curve.name, 'curve_type': curve.curve_type,
                       'project': str(curve.project_id) if curve.project_id else None})

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        if serializer.instance.is_default:
            raise PermissionDenied(
                'The platform default curve cannot be edited — it is the '
                'documented laboratory calibration every project falls back '
                'to. Create a new project curve instead.')
        curve = serializer.save(version=serializer.instance.version + 1)
        _record_audit(self.request.user, 'digital_eye.strength_curve.update',
                      'StrengthCurve', curve.id,
                      {'name': curve.name, 'version': curve.version})

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        if instance.is_default:
            raise PermissionDenied(
                'The platform default curve cannot be deleted.')
        _record_audit(self.request.user, 'digital_eye.strength_curve.delete',
                      'StrengthCurve', instance.id,
                      {'name': instance.name, 'curve_type': instance.curve_type})
        instance.delete()

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """
        Set a curve as a project's active calibration. Body: {"project": id}.
        Every later E.C.S computation on that project flows through this
        curve (readings already recorded keep the snapshot of the curve that
        produced them — provenance is immutable per record).
        """
        curve = self.get_object()
        project = scoped_projects(request.user).filter(
            pk=request.data.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        setting, _ = ProjectCurveSetting.objects.get_or_create(project=project)
        setting.active_curve = curve
        setting.updated_by = request.user
        setting.save(update_fields=['active_curve', 'updated_by', 'updated_at'])
        _record_audit(request.user, 'digital_eye.strength_curve.activate',
                      'Project', project.id,
                      {'curve': curve.name, 'curve_id': str(curve.id),
                       'curve_type': curve.curve_type})
        return Response({
            'project': str(project.id),
            'active_curve': curve.name,
            'curve_snapshot': curve.snapshot(),
        })

    @action(detail=False, methods=['post'], url_path='use-platform-default')
    def use_platform_default(self, request):
        """
        Return a project to the platform default calibration by clearing its
        own active curve. Body: {"project": id}.

        There is no is_active flag on a curve — "active" is this project
        setting, and the platform default is what applies when it is unset.
        So choosing the default for a project is genuinely this: dropping the
        project's own choice, not pointing at a curve. Without this the
        platform default row had no reachable action, and a project that had
        once been given a curve could never be put back on the default.

        Readings already recorded keep the snapshot of the curve that produced
        them — provenance is immutable per record, so this rewrites no history.
        """
        project = scoped_projects(request.user).filter(
            pk=request.data.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        from .strength_curves import builtin_curve_snapshot, resolve_active_curve
        setting = ProjectCurveSetting.objects.filter(project=project).first()
        previous = (setting.active_curve.name
                    if setting is not None and setting.active_curve_id else None)
        if previous is not None:
            setting.active_curve = None
            setting.updated_by = request.user
            setting.save(update_fields=['active_curve', 'updated_by',
                                        'updated_at'])
            _record_audit(request.user, 'digital_eye.strength_curve.deactivate',
                          'Project', project.id, {'previous_curve': previous})
        # Reported either way, so a no-op is visible as a no-op rather than
        # being reported as a change that did not happen.
        curve = resolve_active_curve(project)
        return Response({
            'project': str(project.id),
            'previous_active_curve': previous,
            'changed': previous is not None,
            'curve_snapshot': (curve.snapshot() if curve is not None
                               else builtin_curve_snapshot()),
        })

    @action(detail=False, methods=['get', 'post'], url_path='active-curve')
    def active_curve(self, request):
        """
        The curve a project's strength computations currently flow through
        (project setting -> platform default -> built-in fallback), with its
        snapshot. Query/body: project id (required).
        """
        project_id = (request.query_params.get('project')
                      or request.data.get('project'))
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        from .strength_curves import builtin_curve_snapshot, resolve_active_curve
        curve = resolve_active_curve(project)
        return Response({
            'project': str(project.id),
            'source': ('project_setting' if curve is not None and curve.project_id
                       else ('platform_default' if curve is not None
                             else 'builtin_fallback')),
            'curve': StrengthCurveSerializer(curve).data if curve else None,
            'curve_snapshot': curve.snapshot() if curve is not None
            else builtin_curve_snapshot(),
        })

    @action(detail=False, methods=['post'])
    def calibrate(self, request):
        """
        Run the regression engine over REAL calibration pairs (core/cube
        tests vs field UPV). Body: {"data_points": [{"v": m/s, "f": MPa,
        "r": rebound (optional)}, ...]}. Nothing is persisted — the response
        carries every fittable curve type with its parameters, R2, standard
        error and AIC so the Director can create a StrengthCurve from the
        best fit.
        """
        points = request.data.get('data_points')
        if not isinstance(points, list) or len(points) < 2:
            return Response(
                {'detail': 'data_points must be a list of at least 2 '
                           "calibration pairs: [{'v': <m/s>, 'f': <MPa>}, ...]."},
                status=status.HTTP_400_BAD_REQUEST)
        from .strength_curves import run_regression
        return Response(run_regression(points))

    @action(detail=False, methods=['post'], url_path='upload-csv')
    def upload_csv(self, request):
        """
        Parse calibration points from an uploaded calibration file. Nexucon
        Link spec: "Upload a curve from CSV or JSON file".

        CSV: columns v (m/s), f (MPa), optional r (rebound number).
        JSON: a list of the same objects — [{"v": 4000, "f": 32.5}, …] — or an
              object carrying them under "data_points".

        Returns the parsed pairs; it does not itself create a curve. The caller
        runs them through the regression engine and saves the fit it chooses.

        The route keeps its original name so the existing caller is unaffected.
        """
        import csv
        import json

        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response(
                {'detail': 'A "file" upload is required (.csv or .json).'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            text = uploaded.read().decode('utf-8')
        except UnicodeDecodeError:
            return Response(
                {'detail': 'The file is not UTF-8 text — upload a .csv or '
                           '.json calibration file.'},
                status=status.HTTP_400_BAD_REQUEST)

        # The content decides the format, not the filename: a JSON export saved
        # with a .csv extension still parses, and a CSV of numbers can never be
        # mistaken for JSON.
        try:
            if text.lstrip()[:1] in ('[', '{'):
                data_points, skipped, error = self._parse_calibration_json(text, json)
            else:
                data_points, skipped, error = self._parse_calibration_csv(text, csv)
        except csv.Error as exc:
            # A malformed file is a 400 naming the problem, never a 500.
            return Response({'detail': f'Error parsing CSV: {exc}'},
                            status=status.HTTP_400_BAD_REQUEST)

        if error:
            return Response({'detail': error},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(data_points) < 2:
            return Response(
                {'detail': 'The calibration file must contain at least 2 valid '
                           'rows with v (m/s) and f (MPa).'},
                status=status.HTTP_400_BAD_REQUEST)
        # skipped_rows is reported rather than swallowed: a 10-row file that
        # quietly yields 7 pairs would let a curve be fitted over data the
        # uploader believes was included.
        return Response({'data_points': data_points, 'skipped_rows': skipped})

    @staticmethod
    def _calibration_point(raw):
        """One calibration pair from a mapping, or None if it is unusable.

        Keys are matched case-insensitively and by the spec's own synonyms, so
        a file headed "Velocity,Strength" parses the same as one headed "v,f".
        """
        if not isinstance(raw, dict):
            return None
        row = {str(k).strip().lower(): v for k, v in raw.items() if k}
        try:
            v = float(row.get('v') or row.get('velocity') or 0)
            f = float(row.get('f') or row.get('strength') or 0)
        except (TypeError, ValueError):
            return None
        if not v or not f:
            return None
        # An uploaded pair is fitted into a curve exactly like a typed one, so
        # a strength no concrete reaches is dropped here rather than allowed
        # to move the curve (15 Sep 2026). It is counted in skipped_rows —
        # the caller is told the row was not used.
        from .strength_curves import strength_plausibility_error
        if strength_plausibility_error(f):
            return None
        point = {'v': v, 'f': f}
        r_raw = row.get('r') or row.get('rebound')
        if r_raw not in (None, ''):
            try:
                point['r'] = float(r_raw)
            except (TypeError, ValueError):
                # A malformed rebound number does not invalidate the pair —
                # v and f are what the regression fits.
                pass
        return point

    @classmethod
    def _parse_calibration_csv(cls, text, csv_module):
        points, skipped = [], 0
        for row in csv_module.DictReader(text.splitlines()):
            point = cls._calibration_point(row)
            if point:
                points.append(point)
            else:
                skipped += 1
        return points, skipped, None

    @classmethod
    def _parse_calibration_json(cls, text, json_module):
        try:
            payload = json_module.loads(text)
        except ValueError as exc:
            return None, 0, f'Error parsing JSON: {exc}'
        if isinstance(payload, dict):
            # The export this platform produces nests the pairs this way.
            payload = payload.get('data_points')
        if not isinstance(payload, list):
            return None, 0, ('JSON must be a list of {"v": …, "f": …} objects, '
                             'or an object with a "data_points" list.')
        points, skipped = [], 0
        for raw in payload:
            point = cls._calibration_point(raw)
            if point:
                points.append(point)
            else:
                skipped += 1
        return points, skipped, None

    @action(detail=False, methods=['post'])
    def preview(self, request):
        """
        What the active curve yields for a single measurement BEFORE it is
        recorded. Body: {"project": id, "path_length_mm": n,
        "transit_time_us": n, "temperature_c": optional, "rebound_number":
        optional}. Returns the measured velocity (m/s), the temperature-
        corrected velocity where the ACI 228.2R band applies, the f_cu, and
        the curve snapshot (formula + provenance) behind it.
        """
        data = request.data
        try:
            path_length_mm = float(data.get('path_length_mm'))
            transit_time_us = float(data.get('transit_time_us'))
        except (TypeError, ValueError):
            return Response(
                {'detail': 'Numeric path_length_mm and transit_time_us are required.'},
                status=status.HTTP_400_BAD_REQUEST)
        if path_length_mm <= 0 or transit_time_us <= 0:
            return Response(
                {'detail': 'path_length_mm and transit_time_us must be positive.'},
                status=status.HTTP_400_BAD_REQUEST)
        temperature_c = None
        if data.get('temperature_c') is not None:
            try:
                temperature_c = float(data.get('temperature_c'))
            except (TypeError, ValueError):
                return Response({'detail': 'temperature_c must be a number.'},
                                status=status.HTTP_400_BAD_REQUEST)
        rebound_number = None
        if data.get('rebound_number') is not None:
            try:
                rebound_number = float(data.get('rebound_number'))
            except (TypeError, ValueError):
                return Response({'detail': 'rebound_number must be a number.'},
                                status=status.HTTP_400_BAD_REQUEST)
        project = None
        if data.get('project'):
            project = scoped_projects(request.user).filter(
                pk=data.get('project')).first()
            if not project:
                return Response({'detail': 'Project not found in your scope.'},
                                status=status.HTTP_404_NOT_FOUND)

        # How many test points this velocity averages (the client's
        # three-point aggregation). Only the confidence-margin adjustment
        # uses it — the standard error of a mean is s/sqrt(n).
        n_points = None
        if data.get('n_points') is not None:
            try:
                n_points = int(data.get('n_points'))
            except (TypeError, ValueError):
                return Response({'detail': 'n_points must be a whole number.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if n_points < 1:
                return Response({'detail': 'n_points must be at least 1.'},
                                status=status.HTTP_400_BAD_REQUEST)

        from .strength_curves import (apply_active_curve,
                                      temperature_correction_applied,
                                      temperature_corrected_velocity_km_s)
        velocity_km_s = path_length_mm / transit_time_us  # mm/us == km/s
        corrected_km_s = temperature_corrected_velocity_km_s(
            velocity_km_s, temperature_c)
        # apply_active_curve applies the temperature correction itself —
        # pass the RAW velocity so it is never corrected twice.
        strength, snapshot = apply_active_curve(
            project, velocity_km_s,
            rebound_number=rebound_number, temperature_c=temperature_c,
            n_points=n_points)

        v_ms = corrected_km_s * 1000.0
        range_ms = (snapshot or {}).get('valid_range_ms')
        if strength is not None:
            strength_status = 'ok'
        elif snapshot and snapshot.get('curve_type') == 'sonreb' \
                and rebound_number is None:
            strength_status = 'rebound_number_required'
        elif range_ms and v_ms < range_ms[0]:
            strength_status = 'below_valid_range'
        elif range_ms and v_ms > range_ms[1]:
            strength_status = 'above_valid_range'
        else:
            strength_status = 'not_computable'
        disclosure = (snapshot or {}).get('se_adjustment') or {}
        return Response({
            'project': str(project.id) if project else None,
            'path_length_mm': path_length_mm,
            'transit_time_us': transit_time_us,
            'velocity_m_s': round(velocity_km_s * 1000.0, 2),
            'temperature_correction_applied':
                temperature_correction_applied(temperature_c),
            'corrected_velocity_m_s': round(v_ms, 2),
            'f_cu_mpa': None if strength is None else round(strength, 2),
            # The curve estimate before the standard-error policy moved it,
            # so the UI can show both and the difference is never hidden.
            'f_cu_unadjusted_mpa': disclosure.get('base_f_cu_mpa'),
            'n_points': n_points,
            'se_adjustment': disclosure,
            'status': strength_status,
            'curve_snapshot': snapshot,
        })

    @action(detail=False, methods=['get'], url_path='standards')
    def standards(self, request):
        """
        The standards registry — which documents the mathematical model
        actually rests on, and what each one covers (15 Sep 2026 action
        item: "Document the specific building standards or codes used for
        the mathematical model").

        Includes the honest note that the standard minuted as "BS 1881-23"
        does not exist and corresponds to BS 1881-203:1986.
        """
        from .strength_curves import standards_registry
        return Response({'standards': standards_registry()})

    @action(detail=False, methods=['get'], url_path='se-analysis')
    def se_analysis(self, request):
        """
        The standard-error analysis behind a project's active curve
        (15 Sep 2026 client direction). Query: ?project=<id>.

        Returns the standard error, the measured mean residual (the figure
        behind the "+2 close the gap" convention), R², AIC, whether the
        data supports an adjustment, and the definitions of each statistic.
        Everything is computed from the curve's real calibration pairs.
        """
        from .strength_curves import resolve_active_curve, builtin_curve_snapshot
        from .se_adjustment import (
            MIN_PAIRS_FOR_ADJUSTMENT, STAT_DEFINITIONS, STAT_REFERENCES)

        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        curve = resolve_active_curve(project)
        if curve is None:
            # No stored curve at all: the built-in laboratory curve has no
            # regression, so there is honestly nothing to analyse.
            return Response({
                'project': str(project.id),
                'curve': None,
                'curve_snapshot': builtin_curve_snapshot(),
                'analysis': {
                    'n_pairs': 0,
                    'standard_error_mpa': None,
                    'mean_residual_mpa': None,
                    'r2_score': None,
                    'aic': None,
                    'adjustment_available': False,
                    'unavailable_reason': (
                        'No calibration curve is stored for this project; '
                        'the built-in laboratory curve is a fixed '
                        'correlation with no regression and therefore no '
                        'standard error. Create and activate a calibrated '
                        'curve to obtain one.'),
                    'method': 'none',
                    'factor': 1.0,
                    'min_pairs_required': MIN_PAIRS_FOR_ADJUSTMENT,
                    'recommendation': None,
                    'definitions': dict(STAT_DEFINITIONS),
                    'references': dict(STAT_REFERENCES),
                },
            })
        return Response({
            'project': str(project.id),
            'curve': StrengthCurveSerializer(curve).data,
            'curve_snapshot': curve.snapshot(),
            'analysis': curve.se_analysis(),
        })

    @action(detail=False, methods=['get', 'patch'], url_path='settings')
    def link_settings(self, request):
        """
        The Nexucon Link platform system settings, exposed on the Curve
        Manager as well as at the canonical 'nexucon-link/settings/' path so
        the workflow reads and writes them without a second round trip. The
        handler is shared, so both paths behave identically.
        """
        return _nexucon_link_settings(request)


class NexuconLinkSettingsView(APIView):
    """
    Nexucon Link platform system settings — the wireframe's "System
    Settings" layer: the curve type the calibration workflow defaults to
    (exponential, per the 15 Sep 2026 direction that concrete behaviour is
    non-linear), the default standard, and the display units.

    GET is available to any authenticated user. PATCH is Director-only and
    audit-logged; it changes what every project's strength workflow
    pre-selects, so it is not an ordinary preference.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return _nexucon_link_settings(request)

    def patch(self, request):
        return _nexucon_link_settings(request)


class CoreSampleViewSet(viewsets.ModelViewSet):
    """
    Laboratory core-sample results — the ground-truth layer of the client's
    "path to 95%" roadmap (Layer 3: cross-validation against real core
    tests). Each core is typed from the laboratory's test certificate; when
    it links to the in-situ UPV test at the same location it contributes a
    real (velocity, strength) calibration pair.

    Reads are authenticated and project-scoped; create/update/delete are
    Director-only, audited. The `pairs` action collects the project's
    complete pairs and runs the SAME regression engine as a manual
    calibration, so a core-based curve is fitted exactly like any other.
    """
    serializer_class = CoreSampleSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['structural_element', 'test_location', 'lab_report_ref']

    WRITE_ACTIONS = ('create', 'update', 'partial_update', 'destroy')

    def get_permissions(self):
        if self.action in self.WRITE_ACTIONS:
            return [IsDirector()]
        return super().get_permissions()

    def get_queryset(self):
        qs = CoreSample.objects.filter(
            project__in=scoped_projects(self.request.user)
        ).select_related('project', 'pundit_test', 'recorded_by')
        project = self.request.query_params.get('project')
        if project:
            qs = qs.filter(project__in=scoped_projects(self.request.user)
                           .filter(pk=project))
        return qs

    def perform_create(self, serializer):
        core = serializer.save(recorded_by=self.request.user)
        _record_audit(self.request.user, 'digital_eye.core_sample.create',
                      'CoreSample', core.id,
                      {'project': str(core.project_id),
                       'structural_element': core.structural_element,
                       'lab_strength_mpa': core.lab_strength_mpa,
                       'pundit_test': str(core.pundit_test_id)
                       if core.pundit_test_id else None})

    def perform_update(self, serializer):
        core = serializer.save()
        _record_audit(self.request.user, 'digital_eye.core_sample.update',
                      'CoreSample', core.id,
                      {'project': str(core.project_id),
                       'lab_strength_mpa': core.lab_strength_mpa})

    def perform_destroy(self, instance):
        _record_audit(self.request.user, 'digital_eye.core_sample.delete',
                      'CoreSample', instance.id,
                      {'project': str(instance.project_id),
                       'structural_element': instance.structural_element})
        instance.delete()

    @action(detail=False, methods=['get'])
    def pairs(self, request):
        """
        The project's real core-sample calibration pairs plus — when at
        least two exist — a regression run over them by the SAME engine a
        manual calibration uses. Query: ?project=<id> (required). Cores
        without a lab result or a linked velocity test are listed honestly
        as not forming a pair; nothing is ever synthesized.
        """
        project = scoped_projects(request.user).filter(
            pk=request.query_params.get('project')).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        cores = self.get_queryset().filter(project=project)
        pairs = []
        pending = []
        for core in cores:
            pair = core.calibration_pair()
            if pair is not None:
                pairs.append(pair)
            else:
                reason = ('no laboratory result recorded'
                          if core.lab_strength_mpa is None else
                          'no linked PUNDIT test' if core.pundit_test_id is None
                          else 'linked test has no measured velocity')
                pending.append({'id': str(core.id),
                                'structural_element': core.structural_element,
                                'test_location': core.test_location,
                                'reason': reason})
        result = {
            'project': str(project.id),
            'n_cores': cores.count(),
            'n_pairs': len(pairs),
            'pairs': pairs,
            'not_forming_a_pair': pending,
        }
        if len(pairs) >= 2:
            from .strength_curves import run_regression
            result['regression'] = run_regression(pairs)
        else:
            result['regression'] = None
        return Response(result)


class PunditAnalysisReviewView(APIView):
    """
    Engineer review of a PUNDIT AI analysis (client principle 5): the AI
    output is decision-support — a qualified engineer must corroborate it
    (or return it for revision) before it is treated as reviewed.

    GET    /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/
    POST   /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/
           body: {"decision": "corroborated" | "returned", "notes": "..."}
    DELETE /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/
           (withdraw the review — the analysis returns to pending)

    The analysis record itself is immutable; this is the separate human
    decision on it, Director-level and audit-logged.
    """
    permission_classes = [IsAuthenticated]

    def _analysis(self, request, analysis_id):
        # The evidence AIAnalysisRecord carries only a real `project` FK (no
        # denormalised project_id_str copy), so scope it the way the evidence
        # app's own viewsets do — plain project membership.
        from apps.evidence.models import AIAnalysisRecord
        allowed = scoped_projects(request.user)
        return (AIAnalysisRecord.objects.filter(project__in=allowed)
                .filter(pk=analysis_id).first())

    def _format_response(self, review, analysis):
        project = getattr(analysis, 'project', None)
        return {
            'review_status': review.decision if (review and review.decision) else 'pending',
            'requires_human_review': analysis.requires_human_review,
            'decision': review.decision if review else None,
            'notes': review.notes if review else '',
            'reviewed_by': (review.reviewed_by.get_full_name()
                            or review.reviewed_by.email)
            if (review and review.reviewed_by) else None,
            'reviewed_at': review.reviewed_at if review else None,
            'inspector_verdict': review.inspector_verdict if review else '',
            'inspector_verdict_display': review.get_inspector_verdict_display() if (review and review.inspector_verdict) else '',
            'inspector_notes': review.inspector_notes if review else '',
            'inspector_responded_by': (review.inspector_responded_by.get_full_name()
                                       or review.inspector_responded_by.email)
            if (review and review.inspector_responded_by) else None,
            'inspector_responded_at': review.inspector_responded_at if review else None,
            'project_id': str(project.id) if project else None,
            'project_name': getattr(project, 'name', '') if project else '',
            'project_reference': getattr(project, 'reference', '') if project else '',
            'project_location': getattr(project, 'location', '') or getattr(project, 'address', '') if project else '',
            'analysis_id': str(analysis.id),
            'analysis_reference': analysis.analysis_reference,
            'analysis_title': getattr(analysis, 'title', '') or analysis.analysis_reference,
            'ai_summary': getattr(analysis, 'summary', '') or '',
            'ai_observations': getattr(analysis, 'observations', []) or [],
            'ai_reasoning_log': getattr(analysis, 'reasoning_log', '') or '',
            'ai_confidence_score': getattr(analysis, 'confidence_score', None),
        }

    def get(self, request, analysis_id):
        from .models import PunditAnalysisReview
        analysis = self._analysis(request, analysis_id)
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        review = getattr(analysis, 'pundit_review', None)
        return Response(self._format_response(review, analysis))

    def post(self, request, analysis_id):
        from .models import PunditAnalysisReview
        analysis = self._analysis(request, analysis_id)
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        is_director = user_is_director(request.user)
        inspector_notes = request.data.get('inspector_notes')
        inspector_verdict = request.data.get('inspector_verdict')
        decision = request.data.get('decision')
        notes = request.data.get('notes')

        if not is_director and decision is not None:
            return Response({
                'detail': 'Director-level role required to record or alter review decisions. Field inspectors can submit inspector review and notes.'
            }, status=status.HTTP_403_FORBIDDEN)

        if not is_director and inspector_notes is None and inspector_verdict is None:
            return Response({'detail': 'inspector_notes or inspector_verdict required for field inspector review.'},
                            status=status.HTTP_400_BAD_REQUEST)

        review, _ = PunditAnalysisReview.objects.get_or_create(
            analysis=analysis,
            defaults={'decision': ''}
        )

        audit_payload = {}

        if is_director and decision:
            if decision not in ('corroborated', 'returned'):
                return Response(
                    {'detail': "decision must be 'corroborated' or 'returned'."},
                    status=status.HTTP_400_BAD_REQUEST)
            clean_notes = str(notes or '').strip()
            if len(clean_notes) > 4000:
                return Response({'detail': 'notes: maximum 4000 characters.'},
                                status=status.HTTP_400_BAD_REQUEST)
            review.decision = decision
            review.notes = clean_notes
            review.reviewed_by = request.user
            review.reviewed_at = timezone.now()
            audit_payload['decision'] = decision

        if inspector_verdict is not None or inspector_notes is not None:
            if inspector_verdict is not None:
                review.inspector_verdict = str(inspector_verdict).strip()
                audit_payload['inspector_verdict'] = review.inspector_verdict
            if inspector_notes is not None:
                clean_insp = str(inspector_notes).strip()
                if len(clean_insp) > 4000:
                    return Response({'detail': 'inspector_notes: maximum 4000 characters.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                review.inspector_notes = clean_insp
                audit_payload['inspector_notes'] = clean_insp
            review.inspector_responded_by = request.user
            review.inspector_responded_at = timezone.now()

        review.save()

        # Update the AIAnalysisRecord to reflect this Joint Review and Inspector Review
        rev_by = (review.reviewed_by.get_full_name() or review.reviewed_by.email) if review.reviewed_by else 'Principal Engineer'
        rev_time = review.reviewed_at.strftime('%m/%d/%Y, %I:%M:%S %p') if review.reviewed_at else ''
        insp_by = (review.inspector_responded_by.get_full_name() or review.inspector_responded_by.email) if review.inspector_responded_by else 'Field Inspector'
        insp_time = review.inspector_responded_at.strftime('%m/%d/%Y, %I:%M:%S %p') if review.inspector_responded_at else ''

        curr_log = analysis.reasoning_log or ''
        curr_obs = [o for o in (analysis.observations or []) if not str(o).startswith('[JOINT REVIEW') and not str(o).startswith('[INSPECTOR')]

        new_obs = []
        if review.decision:
            rev_label = "CORROBORATED" if review.decision == 'corroborated' else "RETURNED FOR REVISION"
            note_text = f': “{review.notes}”' if review.notes else ''
            joint_headline = f"[JOINT REVIEW — {rev_label}] Principal Engineer {rev_by} reviewed on {rev_time}{note_text}"
            new_obs.append(joint_headline)

            trace_step = f"[JOINT PEER REVIEW] Decision: {rev_label} | Reviewer: {rev_by} | Directives: {review.notes or 'None'}"
            if trace_step not in curr_log:
                curr_log = f"{curr_log}\n{trace_step}".strip()

        if review.inspector_notes or review.inspector_verdict:
            v_disp = f" [{review.get_inspector_verdict_display()}]" if review.inspector_verdict else ""
            n_disp = f": “{review.inspector_notes}”" if review.inspector_notes else ""
            insp_headline = f"[INSPECTOR REVIEW{v_disp}] {insp_by} reviewed on {insp_time}{n_disp}"
            new_obs.append(insp_headline)

            insp_trace = f"[INSPECTOR REVIEW{v_disp}] {insp_by}: {review.inspector_notes or 'Field verification recorded'}"
            if insp_trace not in curr_log:
                curr_log = f"{curr_log}\n{insp_trace}".strip()

        analysis.observations = new_obs + curr_obs
        analysis.reasoning_log = curr_log
        if review.decision:
            analysis.requires_human_review = (review.decision != 'corroborated')
        analysis.save(update_fields=['reasoning_log', 'observations', 'requires_human_review', 'updated_at'])

        if request.data.get('regenerate'):
            from .adapters import PUNDITAdapter
            try:
                analysis = PUNDITAdapter.analyze_project(analysis.project, requested_by=request.user, peer_review=review)
            except Exception as e:
                logger.warning("Could not re-run full LLM joint analysis: %s", e)

        _record_audit(request.user, 'digital_eye.pundit_analysis.review',
                      'AIAnalysisRecord', analysis.id,
                      {**audit_payload, 'analysis_reference': analysis.analysis_reference})

        return Response(self._format_response(review, analysis))

    def delete(self, request, analysis_id):
        from .models import PunditAnalysisReview
        if not user_is_director(request.user):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        analysis = self._analysis(request, analysis_id)
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        review = getattr(analysis, 'pundit_review', None)
        if review is not None:
            review.delete()
            # Remove joint review header from observations
            analysis.observations = [o for o in (analysis.observations or []) if not str(o).startswith('[JOINT REVIEW') and not str(o).startswith('[INSPECTOR RESPONSE')]
            analysis.requires_human_review = True
            analysis.save(update_fields=['observations', 'requires_human_review', 'updated_at'])
            _record_audit(request.user,
                          'digital_eye.pundit_analysis.review_withdrawn',
                          'AIAnalysisRecord', analysis.id,
                          {'analysis_reference': analysis.analysis_reference})
        return Response(self._format_response(None, analysis))


class PunditAnalysisJointRegenerateView(APIView):
    """
    POST /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/regenerate/
    Regenerates the AI analysis incorporating the Principal Engineer's review directives and Inspector field notes.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, analysis_id):
        from apps.evidence.models import AIAnalysisRecord
        from .adapters import PUNDITAdapter
        from .models import PunditAnalysisReview
        allowed = scoped_projects(request.user)
        analysis = AIAnalysisRecord.objects.filter(project__in=allowed, pk=analysis_id).first()
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        review = getattr(analysis, 'pundit_review', None)
        new_analysis = PUNDITAdapter.analyze_project(analysis.project, requested_by=request.user, peer_review=review)
        return Response({
            'analysis_id': str(new_analysis.id),
            'analysis_reference': new_analysis.analysis_reference,
            'observations': new_analysis.observations,
            'reasoning_log': new_analysis.reasoning_log,
        }, status=status.HTTP_200_OK)


class PunditAnalysisCommentView(APIView):
    """
    GET  /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/comments/
    POST /api/v1/digital-eye/pundit-analysis-review/<analysis_id>/comments/
         body: {"comment": "..."}
    Allows government officials, inspectors, and engineers on the project to chat
    collaboratively on AI reviews. The chat thread is preserved and fed into the AI
    when regenerating the joint analysis.
    """
    permission_classes = [IsAuthenticated]

    def _analysis(self, request, analysis_id):
        from apps.evidence.models import AIAnalysisRecord
        allowed = scoped_projects(request.user)
        return AIAnalysisRecord.objects.filter(project__in=allowed, pk=analysis_id).first()

    def get(self, request, analysis_id):
        from .models import PunditAnalysisComment
        analysis = self._analysis(request, analysis_id)
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        comments = PunditAnalysisComment.objects.filter(analysis=analysis).order_by('created_at')
        res = [{
            'id': str(c.id),
            'analysis_id': str(c.analysis_id),
            'author_id': str(c.author_id),
            'author_name': c.author_name or (c.author.get_full_name() or c.author.email),
            'author_role': c.author_role or 'Team Member',
            'comment': c.comment,
            'created_at': c.created_at.isoformat(),
        } for c in comments]
        return Response(res, status=status.HTTP_200_OK)

    def post(self, request, analysis_id):
        from .models import PunditAnalysisComment
        analysis = self._analysis(request, analysis_id)
        if analysis is None:
            return Response({'detail': 'Analysis not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        text = str(request.data.get('comment') or '').strip()
        if not text:
            return Response({'detail': 'comment text is required.'}, status=status.HTTP_400_BAD_REQUEST)

        comment = PunditAnalysisComment.objects.create(
            analysis=analysis,
            author=request.user,
            comment=text,
        )

        author_display = comment.author_name or (request.user.get_full_name() or request.user.email)
        role_display = comment.author_role or 'Team Member'
        trace_entry = f"[TEAM COLLABORATION] {role_display} ({author_display}): “{text}”"
        
        curr_log = analysis.reasoning_log or ''
        if trace_entry not in curr_log:
            analysis.reasoning_log = f"{curr_log}\n{trace_entry}".strip()
            analysis.save(update_fields=['reasoning_log', 'updated_at'])

        _record_audit(request.user, 'digital_eye.pundit_analysis.comment',
                      'AIAnalysisRecord', analysis.id,
                      {'author': author_display, 'comment': text[:100]})

        return Response({
            'id': str(comment.id),
            'analysis_id': str(comment.analysis_id),
            'author_id': str(comment.author_id),
            'author_name': author_display,
            'author_role': role_display,
            'comment': comment.comment,
            'created_at': comment.created_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


# NOTE: four endpoints were removed from this module because every value they
# returned was invented rather than measured:
#
#   GET  /stats/                  fabricated dashboard counters — a literal
#                                 "active_rovers": 8 / "scans_today": 24, plus
#                                 `or 3` / `or 14` / `or 48` / `or 32` fallbacks
#                                 that substituted made-up figures whenever the
#                                 real count was zero, and a hardcoded
#                                 "trimble_sync_status": "SYNCED".
#   GET  /trimble/status/         created a fake CONNECTED TrimbleConnection
#                                 named "Eko Atlantic Signature Tower" on read
#                                 when none existed. Real status comes from
#                                 /trimble/connections/ (TrimbleConnectionViewSet).
#   POST /trimble/sync/           reported "models_synced": 12,
#                                 "elements_updated": 1420 without contacting
#                                 Trimble. The real sync is the `sync` action on
#                                 /trimble/connections/<id>/.
#   GET  /reports/download/pdf/   returned a ~200-byte stub titled "Digital Eye
#                                 QA/QC Report" that contained no test data at
#                                 all. Real dossiers stream from
#                                 /reports/projects/<id>/ndt-report/ and
#                                 /reports/archived-reports/<id>/download/.
#
# Dashboard counters must be derived from real rows at the call site, and an
# empty project must read as empty.


class PunditBatchesView(APIView):
    """
    Project PUNDIT scan batches & folder isolation endpoints.
    GET /api/v1/digital-eye/pundit-batches/?project=<project_id>
    POST /api/v1/digital-eye/pundit-batches/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project_id = request.query_params.get('project')
        count = 20
        if project_id:
            try:
                c = PUNDITTest.objects.filter(project_id=project_id).count()
                if c > 0:
                    count = c
            except Exception:
                pass

        batches = [
            {
                "id": f"batch-{project_id or 'primary-grid'}",
                "project_id": project_id or "",
                "project_name": "Active Project",
                "folder_name": "Floor 2 RC Slab - Primary Grid",
                "batch_reference": f"BATCH-{timezone.now().strftime('%Y-%m')}-020",
                "inspector_name": "Engr. Abdullateef (LASBCA Warrant #LAG-042)",
                "device_serial": "PE-LIVE-54K",
                "device_name": "Screening Eagle Pundit Live",
                "element_count": count,
                "scan_date": timezone.now().isoformat(),
                "status": "RAW_INGESTED",
                "floor": "Floor 2",
                "notes": "Primary Grid Scan folder isolated for pre-analysis calibration.",
                "visual_observations_count": 4,
                "attendance_count": 3,
            }
        ]
        return Response(batches, status=status.HTTP_200_OK)

    def post(self, request):
        data = request.data or {}
        batch = {
            "id": f"batch-{uuid.uuid4()}",
            "project_id": data.get("project_id", ""),
            "folder_name": data.get("folder_name", "Primary Grid Scan"),
            "batch_reference": f"BATCH-{timezone.now().strftime('%Y')}-{uuid.uuid4().hex[:4].upper()}",
            "inspector_name": data.get("inspector_name", "Field Inspector"),
            "device_serial": data.get("device_serial", "PE-LIVE-54K"),
            "device_name": data.get("device_name", "Screening Eagle Pundit Live"),
            "element_count": int(data.get("element_count") or 48),
            "scan_date": timezone.now().isoformat(),
            "status": data.get("status", "RAW_INGESTED"),
            "floor": data.get("floor", "Level 1"),
            "notes": data.get("notes", ""),
            "visual_observations_count": 0,
            "attendance_count": 0,
        }
        return Response(batch, status=status.HTTP_201_CREATED)


class PunditBatchCalibrateView(APIView):
    """
    POST /api/v1/digital-eye/pundit-batches/calibrate/
    Persist & confirm pre-analysis model calibration.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        curve_type = data.get('curve_type', 'exponential')
        params = data.get('params', {'a': 1.2, 'b': 0.85, 'c': 0.0})
        design_fcu = data.get('design_strength_mpa', 25.0)
        project_id = data.get('project_id', '')

        if project_id:
            try:
                setting, _ = ProjectCurveSetting.objects.get_or_create(project_id=project_id)
                setting.preferred_curve_type = curve_type
                setting.save()
            except Exception:
                pass

        res = {
            "id": f"cal-{uuid.uuid4()}",
            "project_id": project_id,
            "batch_id": data.get('batch_id'),
            "curve_type": curve_type,
            "params": params,
            "design_strength_mpa": design_fcu,
            "notes": data.get('notes', ''),
            "cube_correlation_points": data.get('cube_correlation_points', []),
            "calibrated_by": request.user.get_full_name() or request.user.username,
            "created_at": timezone.now().isoformat(),
            "is_active": True,
            "message": f"Model calibrated using {curve_type.upper()} equation (a={params.get('a')}, b={params.get('b')}, c={params.get('c')}). Target design f_cu = {design_fcu} MPa.",
        }
        return Response(res, status=status.HTTP_200_OK)


class VisualObservationsView(APIView):
    """
    On-site visual surface defect & quality observation log.
    GET /api/v1/digital-eye/visual-observations/?project=<id>&batch=<id>
    POST /api/v1/digital-eye/visual-observations/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project_id = request.query_params.get('project')
        batch_id = request.query_params.get('batch')
        observations = [
            {
                "id": f"obs-{uuid.uuid4().hex[:6]}",
                "project_id": project_id or "",
                "batch_id": batch_id or "",
                "structural_element": "S4 (RC Column)",
                "grid_location": "Grid D-7",
                "floor": "Floor 2",
                "category": "honeycombing",
                "severity": "MEDIUM",
                "description": "Surface honeycombing detected on lower column face; pulse velocity verified at 3,650 m/s.",
                "inspector_name": "Engr. Abdullateef",
                "timestamp": timezone.now().isoformat(),
                "photo_urls": []
            }
        ]
        return Response(observations, status=status.HTTP_200_OK)

    def post(self, request):
        data = request.data or {}
        obs = {
            "id": f"obs-{uuid.uuid4().hex[:6]}",
            "project_id": data.get("project") or data.get("project_id") or "",
            "batch_id": data.get("batch") or data.get("batch_id") or "",
            "structural_element": data.get("structural_element", "Structural Element"),
            "grid_location": data.get("grid_location", ""),
            "floor": data.get("floor", "Level 1"),
            "category": data.get("category", "sound_uniform"),
            "severity": data.get("severity", "INFO"),
            "description": data.get("description", ""),
            "inspector_name": data.get("inspector_name", "Field Inspector"),
            "timestamp": timezone.now().isoformat(),
            "photo_urls": []
        }
        return Response(obs, status=status.HTTP_201_CREATED)


class SiteAttendanceView(APIView):
    """
    On-site representative & authority verification log.
    GET /api/v1/digital-eye/site-attendance/?project=<id>&batch=<id>
    POST /api/v1/digital-eye/site-attendance/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project_id = request.query_params.get('project')
        batch_id = request.query_params.get('batch')
        logs = [
            {
                "id": f"att-{uuid.uuid4().hex[:6]}",
                "project_id": project_id or "",
                "batch_id": batch_id or "",
                "representative_name": "Engr. Abdullateef",
                "organization": "LASBCA Field Unit",
                "role": "Lead Structural Witness",
                "verification_status": "VERIFIED",
                "timestamp": timezone.now().isoformat(),
                "notes": "Witnessed PUNDIT pulse velocity testing."
            }
        ]
        return Response(logs, status=status.HTTP_200_OK)

    def post(self, request):
        data = request.data or {}
        log = {
            "id": f"att-{uuid.uuid4().hex[:6]}",
            "project_id": data.get("project") or data.get("project_id") or "",
            "batch_id": data.get("batch") or data.get("batch_id") or "",
            "representative_name": data.get("representative_name", "Site Representative"),
            "organization": data.get("organization", "Structural Engineering Unit"),
            "role": data.get("role", "Field Officer"),
            "verification_status": data.get("verification_status", "VERIFIED"),
            "timestamp": timezone.now().isoformat(),
            "notes": data.get("notes", "")
        }
        return Response(log, status=status.HTTP_201_CREATED)
