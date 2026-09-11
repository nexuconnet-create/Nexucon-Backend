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
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import IsDirector, scoped_projects, user_is_director
from common.responses.standard import StandardResponse

from .adapters import GNSSProjection, GPRAdapter, PUNDITAdapter
from .bim_preview import build_preview_geometry
from .models import (
    AIAnalysisRecord, BIMElementMapping, BIMModelGeometry, BIMStructuralElement, DeviceReportRecord,
    DigitalEyeFinding, EvidenceSpatialPoint, FieldDevice, GPRAnomaly, GPRScan,
    GPRSurvey, GnssBenchmark, GnssBoundaryPoint, GnssSurvey, LiveStream,
    ProjectCurveSetting, PUNDITTest, PunditTest, ProcessingQueueJob,
    SensorDataFile, StrengthCurve, TrimbleConnection,
    TrimbleProject,
)
from .serializers import (
    AIAnalysisRecordSerializer, BIMElementMappingSerializer, BIMStructuralElementSerializer,
    DeviceReportRecordSerializer, DigitalEyeFindingSerializer, EvidenceSpatialPointSerializer,
    FieldDeviceSerializer, GPRAnomalySerializer, GPRScanSerializer, GPRSurveySerializer,
    GnssBenchmarkSerializer, GnssBoundaryPointSerializer, GnssSurveySerializer,
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
        return queryset

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
                     'activate')

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
        Parse calibration points from an uploaded CSV (Nexucon Link spec).
        Format: v (m/s), f (MPa), [r (rebound number)].
        """
        import csv
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'detail': 'A "file" upload is required (.csv).'},
                            status=status.HTTP_400_BAD_REQUEST)
        
        try:
            content = uploaded.read().decode('utf-8').splitlines()
            reader = csv.DictReader(content)
            data_points = []
            for row in reader:
                # normalize keys
                row_lower = {k.strip().lower(): v for k, v in row.items() if k}
                try:
                    v = float(row_lower.get('v') or row_lower.get('velocity') or 0)
                    f = float(row_lower.get('f') or row_lower.get('strength') or 0)
                    if not v or not f:
                        continue
                    pt = {'v': v, 'f': f}
                    r_raw = row_lower.get('r') or row_lower.get('rebound')
                    if r_raw:
                        pt['r'] = float(r_raw)
                    data_points.append(pt)
                except ValueError:
                    continue
            if len(data_points) < 2:
                return Response(
                    {'detail': 'The CSV must contain at least 2 valid rows with v (m/s) and f (MPa).'},
                    status=status.HTTP_400_BAD_REQUEST)
            return Response({'data_points': data_points})
        except Exception as e:
            return Response({'detail': f'Error parsing CSV: {str(e)}'},
                            status=status.HTTP_400_BAD_REQUEST)

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
            rebound_number=rebound_number, temperature_c=temperature_c)

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
        return Response({
            'project': str(project.id) if project else None,
            'path_length_mm': path_length_mm,
            'transit_time_us': transit_time_us,
            'velocity_m_s': round(velocity_km_s * 1000.0, 2),
            'temperature_correction_applied':
                temperature_correction_applied(temperature_c),
            'corrected_velocity_m_s': round(v_ms, 2),
            'f_cu_mpa': None if strength is None else round(strength, 2),
            'status': strength_status,
            'curve_snapshot': snapshot,
        })


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
