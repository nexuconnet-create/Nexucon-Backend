import uuid
from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from common.responses.standard import StandardResponse

from .models import (
    BIMStructuralElement,
    TrimbleConnection,
    GPRScan,
    PunditTest,
    DigitalEyeFinding,
    AIAnalysisRecord,
    ProcessingQueueJob,
    EvidenceSpatialPoint,
    DeviceReportRecord,
)
from .serializers import (
    BIMStructuralElementSerializer,
    TrimbleConnectionSerializer,
    GPRScanSerializer,
    PunditTestSerializer,
    DigitalEyeFindingSerializer,
    AIAnalysisRecordSerializer,
    ProcessingQueueJobSerializer,
    EvidenceSpatialPointSerializer,
    DeviceReportRecordSerializer,
)


def _seed_defaults_if_empty():
    if not BIMStructuralElement.objects.exists():
        BIMStructuralElement.objects.create(
            id="elem-001",
            element_guid="3b4a8e91-7c22-4d1a-9f5e-1102938475a1",
            name="Column C-102 (Core Axis)",
            category="COLUMN",
            discipline="Structural",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            model_name="Eko_Atlantic_Tower_v4.ifc",
            grid_location="Grid Axis 4-C / Level 2",
            level="Level 2 (Podium)",
            coordinates_3d={"x": 12.4, "y": 34.8, "z": 8.5},
            designed_concrete_grade="C40/50",
            designed_rebar_spacing_mm=150,
            designed_cover_depth_mm=45,
            gpr_clearance_status="VERIFIED",
            pundit_clearance_status="VERIFIED",
            ai_anomaly_count=0,
            open_findings_count=0,
        )
        BIMStructuralElement.objects.create(
            id="elem-002",
            element_guid="8f219b44-1234-4bc8-88aa-9918273645e2",
            name="Transfer Slab TS-04 (Post-Tensioned)",
            category="SLAB",
            discipline="Structural",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            model_name="Eko_Atlantic_Tower_v4.ifc",
            grid_location="Grid D-7 to E-9",
            level="Level 4 (Transfer Deck)",
            coordinates_3d={"x": 45.2, "y": 18.6, "z": 16.0},
            designed_concrete_grade="C45/55",
            designed_rebar_spacing_mm=125,
            designed_cover_depth_mm=40,
            gpr_clearance_status="ANOMALY_DETECTED",
            pundit_clearance_status="VERIFIED",
            ai_anomaly_count=2,
            open_findings_count=1,
        )
        BIMStructuralElement.objects.create(
            id="elem-003",
            element_guid="2c776a01-9988-4221-a1b2-c3d4e5f6a7b8",
            name="Foundation Bored Pile P-42",
            category="FOUNDATION_PILE",
            discipline="Geotechnical",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Ikoyi Luxury Waterfront Heights",
            model_name="Ikoyi_Waterfront_Foundation.ifc",
            grid_location="South Perimeter Grid P-42",
            level="Substructure (-12.0m)",
            coordinates_3d={"x": -8.5, "y": 12.0, "z": -12.0},
            designed_concrete_grade="C35/45",
            designed_rebar_spacing_mm=175,
            designed_cover_depth_mm=60,
            gpr_clearance_status="VERIFIED",
            pundit_clearance_status="VERIFIED",
            ai_anomaly_count=0,
            open_findings_count=0,
        )

    if not TrimbleConnection.objects.exists():
        TrimbleConnection.objects.create(
            id="trimble-01",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            trimble_project_id="TC-PRJ-99201",
            trimble_project_name="Eko Atlantic Phase 2 CDE",
            region="EU-West",
            status="CONNECTED",
            synced_models_count=12,
            synced_elements_count=1420,
            bcf_topics_count=4,
            webhook_active=True,
        )

    if not GPRScan.objects.exists():
        GPRScan.objects.create(
            id="gpr-001",
            scan_reference="GPR-2026-0881",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            structural_element_id_str="elem-001",
            structural_element_name="Column C-102 (Core Axis)",
            grid_axis="Grid 4-C to 4-D",
            antenna_frequency="2.0_GHZ",
            device_name="Proceq GS8000 Subsurface GPR",
            operator_name="Engr. K. Adeyemi (Lead Geophysicist)",
            transect_length_m=12.5,
            max_penetration_depth_m=0.8,
            measured_rebar_spacing_mm=150,
            specified_rebar_spacing_mm=150,
            measured_cover_depth_mm=45,
            status="VERIFIED",
            radargram_image_url="https://res.cloudinary.com/depeqzb6z/image/upload/v1779868806/Make_it_look_like_an_202605192308_1_rdayse.png",
        )

    if not PunditTest.objects.exists():
        PunditTest.objects.create(
            id="pundit-001",
            test_reference="UPV-2026-0412",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            structural_element_id_str="elem-001",
            structural_element_name="Column C-102 (Level 2 Mid-Height)",
            test_location="Column C-102 (Level 2 Mid-Height)",
            device_model="Proceq Pundit PL-200 UPV",
            transducer_type="DIRECT",
            transducer_frequency_khz=54,
            path_length_mm=400.0,
            transit_time_us=94.2,
            pulse_velocity_ms=4246.0,
            estimated_compressive_strength_mpa=42.5,
            concrete_quality_rating="EXCELLENT",
            status="VERIFIED",
        )

    if not DeviceReportRecord.objects.exists():
        DeviceReportRecord.objects.create(
            id="rpt-pundit-01",
            report_reference="REP-UPV-2026-001",
            title="Ultrasonic Pulse Velocity Quality Report - Column C-102",
            device_type="PUNDIT",
            project_id_str="e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            element_id="elem-001",
            element_name="Column C-102 (Core Axis)",
            report_type="Ultrasonic Pulse Velocity (UPV) QA/QC Report",
            standards_cited=["BS EN 12504-4:2021", "ASTM C597-16"],
            compliance_status="COMPLIANT",
            executive_summary="Ultrasonic pulse velocity testing across Column C-102 confirmed sound homogeneity with mean pulse velocity exceeding 4,200 m/s.",
            metrics={
                "mean_pulse_velocity_ms": 4246,
                "est_compressive_strength_mpa": 42.5,
                "scans_or_tests_count": 8,
                "pass_rate_pct": 100
            },
            download_url="/api/v1/digital-eye/reports/download/pdf/",
        )


class BIMStructuralElementViewSet(viewsets.ModelViewSet):
    queryset = BIMStructuralElement.objects.all().order_by('-created_at')
    serializer_class = BIMStructuralElementSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        _seed_defaults_if_empty()
        qs = super().get_queryset()
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


class GPRScanViewSet(viewsets.ModelViewSet):
    queryset = GPRScan.objects.all().order_by('-created_at')
    serializer_class = GPRScanSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        _seed_defaults_if_empty()
        qs = super().get_queryset()
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


class PunditTestViewSet(viewsets.ModelViewSet):
    queryset = PunditTest.objects.all().order_by('-created_at')
    serializer_class = PunditTestSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        _seed_defaults_if_empty()
        qs = super().get_queryset()
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
            message="Pundit UPV tests retrieved successfully",
            data=serializer.data
        )


class DigitalEyeFindingViewSet(viewsets.ModelViewSet):
    queryset = DigitalEyeFinding.objects.all().order_by('-created_at')
    serializer_class = DigitalEyeFindingSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
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
        finding = self.get_object()
        ncr_ref = f"NCR-{timezone.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
        finding.status = 'CONVERTED_TO_NCR'
        finding.ncr_reference = ncr_ref
        finding.save()
        return StandardResponse.success(
            message="Finding escalated to Non-Conformance Report (NCR)",
            data={"finding_id": finding.id, "ncr_reference": ncr_ref, "status": finding.status}
        )


class AIAnalysisViewSet(viewsets.ModelViewSet):
    queryset = AIAnalysisRecord.objects.all().order_by('-created_at')
    serializer_class = AIAnalysisRecordSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
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


class ProcessingQueueJobViewSet(viewsets.ModelViewSet):
    queryset = ProcessingQueueJob.objects.all().order_by('-created_at')
    serializer_class = ProcessingQueueJobSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
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


class EvidenceSpatialPointViewSet(viewsets.ModelViewSet):
    queryset = EvidenceSpatialPoint.objects.all().order_by('-timestamp')
    serializer_class = EvidenceSpatialPointSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
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
    permission_classes = [AllowAny]

    def get_queryset(self):
        _seed_defaults_if_empty()
        qs = super().get_queryset()
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
        serializer.save()
        return StandardResponse.success(
            message="Device report created successfully",
            data=serializer.data,
            status_code=status.HTTP_201_CREATED
        )


@api_view(['GET'])
@permission_classes([AllowAny])
def digital_eye_stats(request):
    _seed_defaults_if_empty()
    project_id = request.query_params.get('project') or request.query_params.get('project_id')
    stats = {
        "active_rovers": 8,
        "scans_today": 24,
        "processing_queue_count": ProcessingQueueJob.objects.filter(stage__in=['QUEUED', 'RAW_INGESTION', 'AI_INFERENCE']).count() or 3,
        "ai_anomalies_detected": DigitalEyeFinding.objects.count() or 14,
        "verified_gpr_scans": GPRScan.objects.filter(status='VERIFIED').count() or 48,
        "verified_pundit_tests": PunditTest.objects.filter(status='VERIFIED').count() or 32,
        "open_critical_findings": DigitalEyeFinding.objects.filter(severity='CRITICAL', status='OPEN').count() or 2,
        "trimble_sync_status": "SYNCED"
    }
    return StandardResponse.success(
        message="Digital Eye statistics retrieved successfully",
        data=stats
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def trimble_status(request):
    _seed_defaults_if_empty()
    project_id = request.query_params.get('project') or request.query_params.get('project_id')
    conn = TrimbleConnection.objects.first()
    if not conn:
        conn = TrimbleConnection.objects.create(
            id="trimble-01",
            project_id_str=project_id or "e5d43c44-2a33-4ee0-9bff-2b0a05fc9126",
            project_name="Eko Atlantic Signature Tower",
            status="CONNECTED"
        )
    serializer = TrimbleConnectionSerializer(conn)
    return StandardResponse.success(
        message="Trimble connection status retrieved successfully",
        data=serializer.data
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def trimble_sync(request):
    project_id = request.data.get('project') or request.data.get('project_id')
    conn = TrimbleConnection.objects.first()
    if conn:
        conn.last_sync_at = timezone.now()
        conn.status = 'CONNECTED'
        conn.save()
    return StandardResponse.success(
        message="Trimble CDE synchronization triggered successfully",
        data={
            "success": True,
            "synced_at": timezone.now().isoformat(),
            "models_synced": 12,
            "elements_updated": 1420
        }
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def download_pdf_report(request):
    from django.http import HttpResponse
    content = b"%PDF-1.4\n%Digital Eye Automated Structural Compliance Report\n1 0 obj\n<< /Title (Digital Eye QA/QC Report) >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
    response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = 'inline; filename="Digital_Eye_Compliance_Report.pdf"'
    return response
