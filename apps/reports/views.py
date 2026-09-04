import logging
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, serializers
from apps.scans.selectors import ScanSelector
from .models import QualityReport
from .serializers import QualityReportSerializer
from drf_spectacular.utils import extend_schema, inline_serializer
from apps.reports.services import ReportService

logger = logging.getLogger(__name__)


from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit


class QualityReportListView(APIView):
    permission_classes = [IsAuthenticated]
    """
    List every generated QA/QC report (most recent first) so the reports
    dashboard can show real defect / anomaly / confidence figures instead of
    guessing them from session records.
    """

    @extend_schema(summary="List generated quality reports", tags=["Reports"])
    def get(self, request):
        reports = (
            QualityReport.objects.select_related('scan')
            .order_by('-generated_at')
        )
        return Response(QualityReportSerializer(reports, many=True).data)


class GenerateReportView(APIView):
    permission_classes = [IsAuthenticated]
    """
    Generate a QA/QC report for a completed scan session.

    - If a completed report already exists, it is returned immediately (200).
    - Otherwise the report is built synchronously so the API always returns a
      useful result (201).
    - A background Celery task is also dispatched when a broker is available;
      if not (e.g. during tests), the OperationalError is caught and logged so
      the synchronous path still succeeds.
    """

    @extend_schema(
        request=inline_serializer(
            name="GenerateReportRequest",
            fields={
                "report_type": serializers.ChoiceField(
                    choices=["progress", "deviation", "qaqc", "earthworks", "compliance"],
                    required=False,
                ),
            },
        ),
        responses={201: QualityReportSerializer, 200: QualityReportSerializer},
    )
    @method_decorator(ratelimit(key='ip', rate='5/m', block=True))
    def post(self, request, session_id):
        session = ScanSelector.get_session(session_id)
        report_type = request.data.get('report_type') or 'qaqc'
        if report_type not in ('progress', 'deviation', 'qaqc', 'earthworks', 'compliance'):
            return Response({'error': 'Unknown report type.'}, status=400)

        # Serve the stored report from the DB. Reports are invalidated when
        # "Process AI Data" or "Align to BIM" re-runs, so a report that is
        # still present is by definition current for the session's data -
        # it is returned as-is instead of being rebuilt.
        existing = (
            QualityReport.objects.filter(scan=session, report_type=report_type, status='completed')
            .order_by('-generated_at')
            .first()
        )
        if existing is not None:
            return Response(QualityReportSerializer(existing).data, status=status.HTTP_200_OK)

        report = ReportService.generate_qaqc_report(session, report_type=report_type)
        return Response(
            QualityReportSerializer(report).data,
            status=status.HTTP_201_CREATED,
        )


class DownloadReportView(APIView):
    permission_classes = [IsAuthenticated]
    """
    Return a pre-signed download URL for a previously generated QA/QC report.

    URL parameter ``file_format`` selects the artifact type (e.g. ``pdf``).
    The parameter is intentionally NOT named ``format`` to avoid clashing with
    DRF's internal content-negotiation keyword.
    """

    @extend_schema(
        parameters=[
            serializers.CharField(required=False, help_text="Template id: progress, deviation, qaqc, earthworks or compliance"),
            serializers.CharField(required=False, help_text="Cover-page override: client name"),
            serializers.CharField(required=False, help_text="Cover-page override: project number"),
            serializers.CharField(required=False, help_text="Cover-page override: site address"),
            serializers.CharField(required=False, help_text="Cover-page override: client contact"),
        ],
        responses={
            200: inline_serializer(
                name="DownloadReportResponse",
                fields={
                    "message": serializers.CharField(),
                    "url": serializers.URLField(),
                },
            )
        }
    )
    @method_decorator(ratelimit(key='ip', rate='10/m', block=True))
    def get(self, request, session_id, file_format):
        session = ScanSelector.get_session(session_id)

        report_type = request.query_params.get('template') or 'qaqc'
        if report_type not in ('progress', 'deviation', 'qaqc', 'earthworks', 'compliance'):
            report_type = 'qaqc'

        # Optional cover-page overrides supplied by the dashboard form
        cover_overrides = {
            'client_name': request.query_params.get('client_name'),
            'project_number': request.query_params.get('project_number'),
            'site_address': request.query_params.get('site_address'),
            'client_contact': request.query_params.get('client_contact'),
        }

        # Serve the stored report for this template from the DB.
        report = (
            QualityReport.objects.filter(scan=session, report_type=report_type, status='completed')
            .order_by("-generated_at")
            .first()
        )
        if report is None:
            # No stored report for this template — the caller must generate it
            # first (POST generate_report). Nothing is fabricated on download.
            return Response(
                {
                    "error": (
                        f"No {report_type} report has been generated for this scan "
                        "session yet. Generate the report first via the report "
                        "generation endpoint."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        has_cover_overrides = any(v for v in cover_overrides.values())

        if not has_cover_overrides and report.report_url:
            # The stored PDF is the artifact of record - stream its bytes
            # instead of re-rendering the document.
            try:
                import requests
                stored = requests.get(report.report_url, timeout=30)
                stored.raise_for_status()
                response = HttpResponse(stored.content, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="report_{session_id}_{report_type}.pdf"'
                response['X-PDF-Source'] = 'stored'
                return response
            except Exception as e:
                logger.warning(
                    'Could not fetch stored PDF %s (%s); re-rendering locally',
                    report.report_url, e,
                )

        # Cover-page overrides require a re-render (the stored PDF has the
        # default cover), as does a stored PDF that cannot be fetched.
        from apps.reports.services import TEMPLATE_SECTIONS
        import traceback
        try:
            pdf_bytes = ReportService.generate_pdf_bytes(
                report,
                cover_overrides=cover_overrides,
                include_sections=TEMPLATE_SECTIONS.get(report_type),
            )
            response = HttpResponse(pdf_bytes, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="report_{session_id}_{report_type}.pdf"'
            response['X-PDF-Source'] = 'rendered'
            return response
        except Exception as e:
            err = traceback.format_exc()
            with open('pdf_error.txt', 'w') as f:
                f.write(err)
            logger.error(f"Failed to generate PDF locally: {err}")
            return Response({"error": f"Failed to download PDF. {str(e)}"}, status=500)


# ---------------------------------------------------------------------------
# AI Report Generator (implementation plan §5 Weeks 5–6): auditable PDF
# reports for project intelligence, statutory inspections and NCRs.
# ---------------------------------------------------------------------------
from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated

from common.permissions import scoped_projects


def _pdf_response(pdf_bytes, filename):
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


class ProjectIntelligenceReportView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/intelligence-report/
    AI-assisted project intelligence report (scores, findings, HITL status,
    recommendations, sign-off block) rendered from live records.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        from .ai_reports import AIReportService
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            pdf_bytes = AIReportService.generate_project_intelligence_report(project, request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Project intelligence report generation failed')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _pdf_response(pdf_bytes, f'intelligence_report_{project_id}.pdf')


class InspectionReportView(APIView):
    """
    GET /api/v1/reports/inspections/{inspection_id}/report/
    Statutory inspection execution report with mandatory GPS verification,
    checklist results and sign-off block.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, inspection_id):
        from apps.inspections.models import Inspection
        from .ai_reports import AIReportService
        inspection = (Inspection.objects
                      .filter(pk=inspection_id,
                              project__in=scoped_projects(request.user))
                      .select_related('project', 'inspector')).first()
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            pdf_bytes = AIReportService.generate_inspection_report(inspection, request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Inspection report generation failed')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _pdf_response(pdf_bytes, f'inspection_{inspection.inspection_reference}.pdf')


class NCRReportView(APIView):
    """
    GET /api/v1/reports/ncrs/{ncr_id}/report/
    Formal Non-Conformance Report document with corrective actions and the
    originating AI correlation finding when applicable.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, ncr_id):
        from apps.compliance.models import NonConformanceReport
        from .ai_reports import AIReportService
        ncr = (NonConformanceReport.objects
               .filter(pk=ncr_id, project__in=scoped_projects(request.user))
               .select_related('project', 'reporter')).first()
        if not ncr:
            return Response({'detail': 'NCR not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            pdf_bytes = AIReportService.generate_ncr_report(ncr, request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NCR report generation failed')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _pdf_response(pdf_bytes, f'ncr_{ncr.ncr_reference}.pdf')


class NDTReportView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-report/
    Lagos State Materials Testing Laboratory-style ultrasonic pulse velocity
    (PUNDIT) NDT report rendered from live digital-eye records for the
    project. The exact generated bytes are also archived (checksummed) so the
    certified dossier registry lists real, re-downloadable documents —
    identical content is not archived twice.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        from .ndt_reports import NDTReportService
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            pdf_bytes = NDTReportService.generate_ndt_report(project, request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT report generation failed')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        try:
            NDTReportService.archive_ndt_report(project, request.user, pdf_bytes)
        except Exception:  # noqa: BLE001 — archive failure must not block the stream
            logger.exception('NDT report archiving failed')
        return _pdf_response(pdf_bytes, f'ndt_report_{project_id}.pdf')


class ArchivedReportListView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/archived-reports/?kind=ndt
    Lists the checksummed dossiers generated for a project, newest first.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        from .models import ArchivedReport
        from .serializers import ArchivedReportSerializer
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        kind = request.query_params.get('kind') or 'ndt'
        reports = (ArchivedReport.objects
                   .filter(project=project, report_kind=kind)
                   .select_related('project', 'generated_by'))
        return Response(ArchivedReportSerializer(reports, many=True).data)


class ArchivedReportDownloadView(APIView):
    """
    GET /api/v1/reports/archived-reports/{report_id}/download/
    Streams the exact archived PDF bytes for the stored dossier.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, report_id):
        from .models import ArchivedReport
        report = (ArchivedReport.objects
                  .filter(pk=report_id, project__in=scoped_projects(request.user))
                  .select_related('project').first())
        if not report:
            return Response({'detail': 'Archived report not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        if not report.file:
            return Response({'detail': 'The archived file for this dossier is missing '
                                       'from storage.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            file_bytes = report.file.read()
        except Exception as exc:  # noqa: BLE001 — remote storage may raise
            logger.exception('Failed to read archived report %s', report.id)
            return Response({'detail': f'Could not retrieve the archived file: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        filename = f"ndt_report_{report.report_reference.replace(' / ', '_').replace('/', '_')}.pdf"
        return _pdf_response(file_bytes, filename)


# ---------------------------------------------------------------------------
# Report template catalogue
# ---------------------------------------------------------------------------
from rest_framework import viewsets

from .models import ReportTemplate
from .serializers import ReportTemplateSerializer


class ReportTemplateViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    """
    Catalogue of report types the platform can produce, so the dashboard does
    not have to hardcode the list.
    """
    queryset = ReportTemplate.objects.filter(is_active=True)
    serializer_class = ReportTemplateSerializer

    @extend_schema(summary="List available report templates", tags=["Reports"])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
