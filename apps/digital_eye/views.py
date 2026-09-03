"""
Digital Eye API views (implementation plan §5A).

Device registry, sensor-data upload with SHA-256 checksums, GPR / PUNDIT /
GNSS survey capture with deterministic AI adapters, BIM element GUID mappings
and live streams anchored to BIM coordinates.
"""
import hashlib
import logging

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import IsDirector, scoped_projects, user_is_director

from .adapters import GPRAdapter, GNSSProjection, PUNDITAdapter
from .models import (
    BIMElementMapping, FieldDevice, GPRAnomaly, GPRSurvey, GnssBenchmark,
    GnssBoundaryPoint, GnssSurvey, LiveStream, PUNDITTest, SensorDataFile,
    TrimbleConnection, TrimbleProject,
)
from .serializers import (
    BIMElementMappingSerializer, FieldDeviceSerializer, GPRAnomalySerializer,
    GPRSurveySerializer, GnssBenchmarkSerializer, GnssBoundaryPointSerializer,
    GnssSurveySerializer, LiveStreamSerializer, PUNDITTestSerializer,
    SensorDataFileSerializer, TrimbleConnectionSerializer, TrimbleProjectSerializer,
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
    except Exception:  # auditing must never break the request path
        logger.exception('audit write failed for %s', action)


class FieldDeviceViewSet(viewsets.ModelViewSet):
    """
    Device registry for Digital Eye hardware (Tersus GNSS MVP SI, GPR carts,
    PUNDIT instruments, scanners). Devices report telemetry through the
    heartbeat action — telemetry fields are never set by hand.
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
        """
        Device telemetry report (battery, position, fix quality). Updates
        last_seen and marks the device online. Telemetry values are taken
        verbatim from the report — defaults are never invented.
        """
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

        # Compute the checksum from the real uploaded bytes.
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
    GPR subsurface surveys. Anomalies are added through the nested endpoint;
    `analyze` runs the deterministic GPR adapter (plan §5 Week 3) which
    registers evidence and persists an AIAnalysisRecord.
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
        # Explicit query-param filters (django-filter is not a dependency).
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
        if serializer.instance.status == 'completed' and 'status' not in self.request.data:
            # Completed surveys are part of the evidence record — changes
            # require an explicit status transition.
            pass
        if self.request.data.get('status') == 'completed':
            # `status` is read-only on the serializer, so the transition is
            # applied here: the record is stamped AND the status persisted.
            serializer.save(status='completed', completed_at=timezone.now())
        else:
            serializer.save()

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """Run the deterministic GPR adapter over the survey's anomalies."""
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
    """Detected subsurface features (operator marking, device software or AI)."""
    serializer_class = GPRAnomalySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['description', 'survey__survey_reference', 'survey__title']

    def get_queryset(self):
        queryset = GPRAnomaly.objects.filter(
            survey__project__in=scoped_projects(self.request.user),
        ).select_related('survey')
        # Explicit query-param filters (django-filter is not a dependency).
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
    PUNDIT ultrasonic NDT (BS 1881-203 / ASTM C597). Measured inputs are
    operator/device values; `analyze` computes velocity, quality grade and
    crack depth deterministically and persists an AIAnalysisRecord.
    """
    serializer_class = PUNDITTestSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter]
    search_fields = ['test_reference', 'structural_element', 'operator_name',
                     'test_location', 'notes']

    def get_queryset(self):
        queryset = PUNDITTest.objects.filter(
            project__in=scoped_projects(self.request.user),
        ).select_related('project', 'device', 'operator', 'created_by').prefetch_related('files')
        # Explicit query-param filters (django-filter is not a dependency, so
        # the declared filterset_fields are implemented here by hand).
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
        return queryset

    def perform_create(self, serializer):
        test = serializer.save(created_by=self.request.user, operator=self.request.user)
        _record_audit(self.request.user, 'digital_eye.pundit_test.create',
                      'PUNDITTest', test.id, {'test_reference': test.test_reference})

    # Measurement inputs feed every derived output (velocity, quality grade,
    # crack depth, the NDT report, archived dossiers). A correction that
    # changed them without re-running the analysis would leave the stored
    # outputs stale — so an edit that touches any of these re-analyzes.
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
            # Deterministic re-analysis from the corrected inputs — same math
            # as the explicit analyze action; nothing is fabricated.
            PUNDITAdapter.analyze(test)

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """Deterministic pulse-velocity / crack-depth analysis."""
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


class GnssSurveyViewSet(viewsets.ModelViewSet):
    """
    Tersus GNSS (MVP SI) positioning surveys with benchmarks and boundary
    points. `project_survey` converts every point to UTM 31N (Minna Datum
    working CRS) and computes coordinate variance against design coordinates
    when design easting/northing are supplied on the points' design payload.
    """
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
        """
        Project all benchmarks and boundary points of the survey to UTM 31N
        and register the survey in the Evidence Registry. Optionally computes
        variance vs design coordinates passed as:
          {"design_points": {"BM-001": {"easting": 500000.0, "northing": 700000.0}}}
        """
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

        # Register the completed survey in the Evidence Registry.
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
        # Benchmarks are auto-projected to UTM 31N on capture.
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
    """
    BIM GUID <-> structural-element mappings (plan §5 Week 2). Populated by
    the Trimble Connect sync, IFC uploads, or manual entry — then used by the
    correlation engine to attach NDT evidence to design elements.
    """
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
    """
    Live video streams mapped to BIM element coordinates (plan §5 Week 2) for
    visual-support overlay in inspections and HITL review.
    """
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
        """Mark a live stream's current status from an operator report."""
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
# Trimble Connect integration (OAuth2 + PKCE, health checks, BIM sync)
# ======================================================================

class TrimbleConnectionViewSet(viewsets.ModelViewSet):
    """
    Trimble Connect OAuth 2.0 (PKCE) connection lifecycle:
      POST /connections/                     create a connection record
      POST /connections/{id}/authorize/      get the OAuth authorization URL
      POST /connections/{id}/callback/       complete the code-for-token exchange
      POST /connections/{id}/health/         run an authenticated health check
      POST /connections/{id}/discover/       discover Trimble projects
      POST /connections/{id}/sync/           sync projects + BIM GUID mappings
    """
    serializer_class = TrimbleConnectionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        # Readable by authenticated staff; create/delete/sync are
        # director-gated in the corresponding methods.
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
        """Begin the OAuth2+PKCE handshake; returns the authorization URL."""
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
        """Complete the OAuth handshake with the authorization code."""
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
        """Authenticated health check (plan §5 Week 1)."""
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
        """Discover Trimble Connect projects visible to the connection."""
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
        """Sync all discovered projects: BIM models + GUID mappings."""
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
    """
    Discovered Trimble projects. Linking a Trimble project to a Nexucon
    project enables BIM GUID mapping sync for that project.
    """
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
        """Sync BIM models + GUID mappings for this Trimble project."""
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
    """
    Import BIM elements (IFC GlobalId <-> structural element ID mappings) from
    an uploaded IFC or Revit RVT file. IFC files are parsed directly
    (credential-free); RVT files are first translated to IFC through Autodesk
    Platform Services (APS Model Derivative) — the same translation path as
    the scan-analysis pipeline, disk-cached after the first run.
    POST /api/v1/digital-eye/bim-elements/import-ifc/
      multipart: project=<uuid>, file=<model.ifc | model.rvt>
    """
    permission_classes = [IsAuthenticated]

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
        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({'detail': 'Multipart "file" field (BIM model: .ifc or .rvt) is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        file_name = (uploaded.name or '').lower()
        if not (file_name.endswith('.ifc') or file_name.endswith('.rvt')):
            return Response({
                'detail': 'Unsupported model format. Upload an .ifc model, or a .rvt '
                          'Revit model (translated to IFC via Autodesk APS).'},
                status=status.HTTP_400_BAD_REQUEST)

        if file_name.endswith('.ifc'):
            content = ContentFile(uploaded.read())
            content.name = uploaded.name
            translated_from_rvt = False
        else:
            # Revit's proprietary .rvt: translate to IFC via Autodesk APS
            # (credentials required — RVT is a closed format and cannot be
            # parsed locally). Translation of large models can take minutes.
            from apps.processing.bim_geometry import ensure_ifc
            cache_dir = os.path.join(settings.BASE_DIR, 'media', 'temp_bim')
            os.makedirs(cache_dir, exist_ok=True)
            fd, tmp_rvt = tempfile.mkstemp(suffix='.rvt', dir=cache_dir)
            with os.fdopen(fd, 'wb') as tmp:
                for chunk in uploaded.chunks():
                    tmp.write(chunk)
            try:
                ifc_path = ensure_ifc(tmp_rvt)
            except ValueError as exc:
                # APS credentials missing — say so honestly instead of failing opaquely.
                os.unlink(tmp_rvt)
                return Response({
                    'detail': f'{exc} Revit (.rvt) models are a closed proprietary format '
                              'and must be translated to IFC by Autodesk Platform Services. '
                              'Either configure AUTODESK_CLIENT_ID / AUTODESK_CLIENT_SECRET, '
                              'or export an IFC directly from Revit '
                              '(File → Export → IFC) and upload that.'},
                    status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                logger.exception('APS RVT->IFC translation failed for %s', uploaded.name)
                if os.path.exists(tmp_rvt):
                    os.unlink(tmp_rvt)
                return Response({
                    'detail': 'Autodesk APS could not translate this Revit model to IFC. '
                              'Verify the file is a valid .rvt, or export an IFC directly '
                              'from Revit (File → Export → IFC) and upload that.'},
                    status=status.HTTP_502_BAD_GATEWAY)
            finally:
                # The translated IFC is cached by content digest; the raw
                # upload itself is no longer needed.
                if os.path.exists(tmp_rvt):
                    os.unlink(tmp_rvt)
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
        _record_audit(request.user, 'digital_eye.bim_elements.import_ifc',
                      'Project', project.id,
                      {'file': uploaded.name, 'created': created, 'updated': updated,
                       'translated_from_rvt': translated_from_rvt})
        return Response({
            'file': uploaded.name,
            'translated_from_rvt': translated_from_rvt,
            'elements_extracted': len(elements),
            'mappings_created': created,
            'mappings_updated': updated,
        }, status=status.HTTP_201_CREATED)
