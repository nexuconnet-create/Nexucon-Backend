import logging
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
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
    permission_classes = [AllowAny]
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
    permission_classes = [AllowAny]
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
    permission_classes = [AllowAny]
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
            # Auto-generate if no stored report exists for this template
            report = ReportService.generate_qaqc_report(session, report_type=report_type)

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
# Report template catalogue
# ---------------------------------------------------------------------------
from rest_framework import viewsets

from .models import ReportTemplate
from .serializers import ReportTemplateSerializer


class ReportTemplateViewSet(viewsets.ModelViewSet):
    permission_classes = [AllowAny]
    """
    Catalogue of report types the platform can produce, so the dashboard does
    not have to hardcode the list.
    """
    queryset = ReportTemplate.objects.filter(is_active=True)
    serializer_class = ReportTemplateSerializer

    @extend_schema(summary="List available report templates", tags=["Reports"])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
