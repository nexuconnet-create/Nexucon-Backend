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


# ---------------------------------------------------------------------------
# Report CMS (8 Sep 2026 review meeting, item H7; 4 Sep register C4/C5):
# password-protected editable template sections for the statutory NDT
# report, plus a Word (.docx) export of the same content.
# ---------------------------------------------------------------------------
from django.contrib.auth.hashers import check_password, make_password

from common.permissions import IsDirector as IsCMSDirector

from .models import ReportCMSPassword, ReportSectionOverride
from .report_cms import CMS_SECTIONS, get_cms_text


def _cms_gate(request):
    """
    Director role + CMS password check for section edits. Returns a DRF
    Response when the request must be rejected, else None.
    """
    credential = ReportCMSPassword.objects.first()
    if credential is None:
        return Response(
            {'detail': 'No report-CMS password has been set yet. A Director '
                       'must set one via POST /api/v1/reports/cms/password/ '
                       'before template sections can be edited.'},
            status=status.HTTP_403_FORBIDDEN)
    try:
        body = request.data or {}
    except Exception:  # noqa: BLE001 — bodyless DELETE
        body = {}
    supplied = (body.get('cms_password')
                or request.query_params.get('cms_password')
                or '')
    if not supplied or not check_password(str(supplied),
                                          credential.password_hash):
        return Response({'detail': 'Incorrect report-CMS password.'},
                        status=status.HTTP_403_FORBIDDEN)
    return None


def _scoped_project_or_none(user, project_id):
    """Resolve a project id string inside the user's scope. Anything that is
    not a valid UUID for a project in scope returns None (404 upstream) —
    never a 500 from an unparseable id."""
    if not project_id:
        return None
    try:
        import uuid as _uuid
        pk = _uuid.UUID(str(project_id))
    except (ValueError, AttributeError, TypeError):
        return None
    from apps.projects.models import Project
    return scoped_projects(user).filter(pk=pk).first()


class ReportCMSSectionsView(APIView):
    """
    GET /api/v1/reports/cms/sections/?project=<uuid>
    Every editable template section with its effective body and where that
    body comes from (project override / platform override / computed /
    default). Read access is authenticated-only — the password only guards
    edits. With a project selected, the generated-content sections carry
    the exact wording computed from that project's recorded data (a
    ``requires_project`` section without a project shows no body; a
    ``requires_data`` section with no data behind it is honestly marked
    and cannot be edited).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        project = _scoped_project_or_none(
            request.user, request.query_params.get('project'))
        if request.query_params.get('project') and project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        computed = {}
        if project is not None:
            from .ndt_reports import NDTReportService
            try:
                computed = NDTReportService.computed_section_bodies(project)
            except Exception:  # noqa: BLE001 — CMS listing must not 500
                logger.exception('computed CMS bodies failed')
                computed = {}
        sections = []
        for key, meta in CMS_SECTIONS.items():
            body, source = get_cms_text(project, key, computed=computed)
            sections.append({
                'key': key,
                'label': meta['label'],
                'kind': meta['kind'],
                'help': meta['help'],
                'default': meta['default'],
                'body': body,
                'source': source,
                'computed': bool(meta.get('computed')),
                # Generated-content sections are per-project: they have no
                # body at all without a project selected.
                'requires_project': bool(meta.get('computed')),
                # Sections whose wording states recorded results: no data
                # behind them means nothing to reword — never an invention.
                'requires_data': bool(meta.get('computed'))
                and key in ('executive_summary', 'visual_observations',
                            'rebar_statement', 'findings_statement',
                            'conclusion_items'),
            })
        return Response({
            'project': str(project.id) if project else None,
            'password_set': ReportCMSPassword.objects.exists(),
            'sections': sections,
        })


class ReportCMSSectionView(APIView):
    """
    PUT    /api/v1/reports/cms/sections/<key>/   {body, project?, cms_password}
    DELETE /api/v1/reports/cms/sections/<key>/?project=<uuid>
    Save or revert one template section. Directors only, and only with the
    CMS password. An override with ``project`` applies to that project
    alone; without it the override applies platform-wide. Every change is
    audit-logged.
    """
    permission_classes = [IsCMSDirector]

    def _section(self, key):
        if key not in CMS_SECTIONS:
            return Response(
                {'detail': f'Unknown report section {key!r}. Valid keys: '
                           f'{", ".join(CMS_SECTIONS)}.'},
                status=status.HTTP_404_NOT_FOUND)
        return None

    def _project(self, request):
        """Resolve the optional target project from the request."""
        try:
            body = request.data or {}
        except Exception:  # noqa: BLE001 — bodyless DELETE
            body = {}
        project_id = body.get('project') or request.query_params.get('project')
        if not project_id:
            return None, None
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return None, Response(
                {'detail': 'Project not found in your scope.'},
                status=status.HTTP_404_NOT_FOUND)
        return project, None

    def put(self, request, key):
        rejected = self._section(key)
        if rejected:
            return rejected
        rejected = _cms_gate(request)
        if rejected:
            return rejected
        # Generated-content sections are per-project: their body is the
        # wording computed from one project's recorded data.
        if CMS_SECTIONS[key].get('computed'):
            try:
                body_json = request.data or {}
            except Exception:  # noqa: BLE001
                body_json = {}
            if not (body_json.get('project')
                    or request.query_params.get('project')):
                return Response(
                    {'detail': 'This generated-content section is '
                               'project-specific. Select a project to edit '
                               'its wording.'},
                    status=status.HTTP_400_BAD_REQUEST)
        body = (request.data or {}).get('body')
        if body is None or not str(body).strip():
            return Response({'detail': 'body is required (use DELETE to '
                                       'revert to the default text).'},
                            status=status.HTTP_400_BAD_REQUEST)
        # A line-kind section is a single-line reference whose two parts
        # print in different places (serial box / header text) — the shape
        # is part of the statutory layout, so it is validated, not assumed.
        # The exact "NNNN / MTL/NDT/YYYY" shape is enforced (12 Sep 2026):
        # a loose " / " check let typos like "MTL/NDR" onto statutory
        # documents, whose QR then referenced a serial the laboratory never
        # issued.
        if CMS_SECTIONS[key]['kind'] == 'line':
            import re
            candidate = str(body).strip()
            if '\n' in candidate or '\r' in candidate:
                return Response(
                    {'detail': 'The report reference must be a single line.'},
                    status=status.HTTP_400_BAD_REQUEST)
            if not re.fullmatch(r'\d{1,4} / MTL/NDT/\d{4}', candidate):
                return Response(
                    {'detail': 'The report reference must keep the exact '
                               'shape "NNNN / MTL/NDT/YYYY" (e.g. '
                               '"0420 / MTL/NDT/2027") — the part before '
                               '" / " prints inside the blue serial box on '
                               'every page.'},
                    status=status.HTTP_400_BAD_REQUEST)
        project, rejected = self._project(request)
        if rejected:
            return rejected
        # A generated-content section with no computed body has no recorded
        # data behind it — an override there could only invent results.
        if CMS_SECTIONS[key].get('computed') and project is not None:
            from .ndt_reports import NDTReportService
            if key not in NDTReportService.computed_section_bodies(project):
                return Response(
                    {'detail': 'No recorded data backs this section yet '
                               '(no test results, observations or rebar '
                               'survey recorded for the project). Its '
                               'wording stays fixed at the honest "not '
                               'recorded" statement — it cannot be edited '
                               'until the underlying data exists.'},
                    status=status.HTTP_409_CONFLICT)
        override, _created = ReportSectionOverride.objects.update_or_create(
            project=project, section_key=key,
            defaults={'body': str(body), 'updated_by': request.user})
        from apps.evidence.review import record_audit
        record_audit(
            request.user, 'report_cms_section_saved', 'ReportSectionOverride',
            override.id,
            new_state={'section_key': key,
                       'project': str(project.id) if project else None,
                       'body_length': len(override.body)},
            metadata={'section_key': key, 'scope': 'project' if project
                      else 'platform'})
        _, source = get_cms_text(project, key)
        return Response({'key': key, 'source': source, 'body': override.body})

    def delete(self, request, key):
        rejected = self._section(key)
        if rejected:
            return rejected
        rejected = _cms_gate(request)
        if rejected:
            return rejected
        # A generated-content section can only hold per-project overrides,
        # so reverting one requires the project it belongs to.
        if CMS_SECTIONS[key].get('computed'):
            project, rejected = self._project(request)
            if rejected:
                return rejected
            if project is None:
                return Response(
                    {'detail': 'This generated-content section is '
                               'project-specific. Select the project whose '
                               'wording you want to revert.'},
                    status=status.HTTP_400_BAD_REQUEST)
        else:
            project, rejected = self._project(request)
            if rejected:
                return rejected
        deleted, _ = ReportSectionOverride.objects.filter(
            project=project, section_key=key).delete()
        from apps.evidence.review import record_audit
        record_audit(
            request.user, 'report_cms_section_reverted',
            'ReportSectionOverride', key,
            new_state={'section_key': key,
                       'project': str(project.id) if project else None},
            metadata={'section_key': key, 'scope': 'project' if project
                      else 'platform'})
        computed = None
        if CMS_SECTIONS[key].get('computed') and project is not None:
            from .ndt_reports import NDTReportService
            computed = NDTReportService.computed_section_bodies(project)
        body, source = get_cms_text(project, key, computed=computed)
        return Response({'key': key, 'source': source, 'body': body,
                         'reverted': bool(deleted)})


class ReportCMSPasswordView(APIView):
    """
    POST /api/v1/reports/cms/password/   {new_password, current_password?}
    Set (or change) the password guarding report-CMS edits. Directors only.
    Changing an existing password requires the current one.
    """
    permission_classes = [IsCMSDirector]

    def post(self, request):
        new_password = (request.data or {}).get('new_password')
        if not new_password or len(str(new_password)) < 8:
            return Response({'detail': 'new_password is required and must be '
                                       'at least 8 characters.'},
                            status=status.HTTP_400_BAD_REQUEST)
        credential = ReportCMSPassword.objects.first()
        if credential is not None:
            current = (request.data or {}).get('current_password', '')
            if not current or not check_password(str(current),
                                                 credential.password_hash):
                return Response(
                    {'detail': 'A report-CMS password already exists; the '
                               'current password is required to change it.'},
                    status=status.HTTP_403_FORBIDDEN)
            credential.password_hash = make_password(str(new_password))
            credential.set_by = request.user
            credential.save()
        else:
            credential = ReportCMSPassword.objects.create(
                password_hash=make_password(str(new_password)),
                set_by=request.user)
        from apps.evidence.review import record_audit
        record_audit(request.user, 'report_cms_password_set',
                     'ReportCMSPassword', credential.id)
        return Response({'detail': 'Report-CMS password saved.'})


class NDTWordExportView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-report-word/
    The statutory NDT report as an editable Word document (.docx) — the
    same sections, the same CMS overrides and the same server-computed
    figures as the PDF, built with python-docx.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        from .word_export import NDTWordExporter
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            docx_bytes = NDTWordExporter.export_docx(project, request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT Word export failed')
            return Response({'detail': f'Word export failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        response = HttpResponse(
            docx_bytes,
            content_type=('application/vnd.openxmlformats-officedocument'
                          '.wordprocessingml.document'))
        response['Content-Disposition'] = \
            f'attachment; filename="ndt_report_{project_id}.docx"'
        return response


# ---------------------------------------------------------------------------
# Public report verification (REFINED EXECUTIVE SUMMARY, PART B missing
# item 2): the QR code on the report cover resolves here. The endpoint is
# deliberately public — a report recipient has no platform account. It
# discloses ONLY whether an archived dossier with this reference + content
# digest exists and its verification facts; no project data.
# ---------------------------------------------------------------------------
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.permissions import AllowAny


@method_decorator(csrf_exempt, name='dispatch')
class ReportVerifyView(APIView):
    """
    GET /api/v1/reports/verify/?ref=<report reference>&digest=<content digest>
    Public verification of one archived NDT dossier. Returns the archive's
    verification facts (reference, digest, checksum, counts, compliance
    verdict, archive date) or an honest 'not found' — never project data.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from .models import ArchivedReport
        ref = (request.query_params.get('ref') or '').strip()
        digest = (request.query_params.get('digest') or '').strip()
        if not ref or not digest:
            return Response(
                {'verified': False,
                 'detail': 'Both a report reference and a content digest are '
                           'required (as encoded in the report QR code).'},
                status=status.HTTP_400_BAD_REQUEST)
        report = (ArchivedReport.objects
                  .filter(report_reference=ref, content_key=digest)
                  .first())
        if report is None:
            return Response(
                {'verified': False,
                 'detail': 'No archived report matches this reference and '
                           'digest. The document may have been altered, or it '
                           'was never generated by this platform.'},
                status=status.HTTP_404_NOT_FOUND)
        return Response({
            'verified': True,
            'report_reference': report.report_reference,
            'title': report.title,
            'content_digest': report.content_key,
            'sha256_checksum': report.sha256_checksum,
            'test_count': report.test_count,
            'assessed_count': report.assessed_count,
            'passed_count': report.passed_count,
            'compliance_status': report.compliance_status,
            'archived_at': report.created_at,
        })


@method_decorator(csrf_exempt, name='dispatch')
class ReportVerifyDownloadView(APIView):
    """
    GET /api/v1/reports/verify/download/?ref=<reference>&digest=<content digest>
    Public download of the authentic archived original for the dossier a
    cover QR resolves to — the same ref+digest pair the verification endpoint
    checks. Deliberately public (a report recipient has no platform account)
    but gated on BOTH query parameters: someone holding a tampered or
    forged document does not carry the matching digest, so they get an
    honest 404, never the original. Streams the exact archived bytes.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        from .models import ArchivedReport
        ref = (request.query_params.get('ref') or '').strip()
        digest = (request.query_params.get('digest') or '').strip()
        if not ref or not digest:
            return Response(
                {'verified': False,
                 'detail': 'Both a report reference and a content digest are '
                           'required (as encoded in the report QR code).'},
                status=status.HTTP_400_BAD_REQUEST)
        report = (ArchivedReport.objects
                  .filter(report_reference=ref, content_key=digest)
                  .first())
        if report is None:
            return Response(
                {'verified': False,
                 'detail': 'No archived report matches this reference and '
                           'digest. The document may have been altered, or it '
                           'was never generated by this platform.'},
                status=status.HTTP_404_NOT_FOUND)
        if not report.file:
            return Response(
                {'verified': False,
                 'detail': 'The archived original for this dossier is missing '
                           'from storage.'},
                status=status.HTTP_404_NOT_FOUND)
        try:
            file_bytes = report.file.read()
        except Exception as exc:  # noqa: BLE001 — remote storage may raise
            logger.exception('Failed to read archived report %s', report.id)
            return Response({'detail': f'Could not retrieve the archived file: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        filename = (f"ndt_report_{report.report_reference}"
                    .replace(' / ', '_').replace('/', '_'))
        return _pdf_response(file_bytes, filename)


# ---------------------------------------------------------------------------
# Report preview (REFINED EXECUTIVE SUMMARY §2.1): the exact report the
# generate endpoint will produce, streamed without archiving — so the
# operator can inspect it before committing to the certified dossier.
# ---------------------------------------------------------------------------
class NDTReportPreviewView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-report-preview/
    Renders the identical PDF bytes the generate endpoint produces (same
    service, same CMS overrides, same branding) but does NOT archive the
    result. Watermarked 'PREVIEW' by disposition filename.
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
            pdf_bytes = NDTReportService.generate_ndt_report(project,
                                                             request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT report preview failed')
            return Response({'detail': f'Report preview failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _pdf_response(pdf_bytes,
                             f'ndt_report_PREVIEW_{project_id}.pdf')


# ---------------------------------------------------------------------------
# Report branding (REFINED EXECUTIVE SUMMARY §2.3): per-project logo /
# watermark the report render picks up. Directors only — branding a
# statutory document is a controlled act.
# ---------------------------------------------------------------------------
class ReportBrandingView(APIView):
    """
    GET    /api/v1/reports/projects/{project_id}/branding/
    PATCH  /api/v1/reports/projects/{project_id}/branding/  (multipart:
           logo / watermark files + position/size/opacity fields)
    DELETE /api/v1/reports/projects/{project_id}/branding/  (remove branding)
    """
    permission_classes = [IsAuthenticated]

    _IMAGE_FIELDS = ('logo', 'watermark', 'cover_logo')

    def get(self, request, project_id):
        from .models import ReportBranding
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row = getattr(project, 'report_branding', None)
        if row is None:
            return Response({'branding_configured': False})
        return Response(self._payload(row))

    def patch(self, request, project_id):
        from apps.evidence.review import record_audit
        from .models import ReportBranding
        if not IsCMSDirector().has_permission(request, self):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row, _ = ReportBranding.objects.get_or_create(project=project)
        data = request.data
        errors = []
        for field in ('logo_position', 'watermark_position'):
            if field in data and data[field] not in dict(
                    ReportBranding._meta.get_field(field).choices):
                errors.append(f'{field}: invalid choice.')
        if 'logo_size' in data and data['logo_size'] not in dict(
                ReportBranding._meta.get_field('logo_size').choices):
            errors.append('logo_size: invalid choice.')
        if 'watermark_opacity_pct' in data:
            try:
                op = int(data['watermark_opacity_pct'])
                if not 0 <= op <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append('watermark_opacity_pct must be an integer '
                              '0-100.')
        for field in self._IMAGE_FIELDS:
            f = data.get(field)
            if f is None or f is False or field not in data:
                continue
            if hasattr(f, 'content_type') and \
                    f.content_type not in ('image/png', 'image/jpeg'):
                errors.append(f'{field}: PNG or JPEG images only.')
        if errors:
            return Response({'detail': ' '.join(errors)},
                            status=status.HTTP_400_BAD_REQUEST)
        for field in ('logo_position', 'logo_size', 'watermark_position'):
            if field in data:
                setattr(row, field, data[field])
        if 'watermark_opacity_pct' in data:
            row.watermark_opacity_pct = int(data['watermark_opacity_pct'])
        for field in self._IMAGE_FIELDS:
            f = data.get(field)
            if hasattr(f, 'read'):
                getattr(row, field).save(
                    f'branding_{project_id}_{field}_{f.name}',
                    f, save=False)
                # Uploading a cover image implies showing it — clear a
                # previously-set hide flag (an explicit cover_logo_hidden
                # below still wins when both arrive in one request).
                if field == 'cover_logo':
                    row.cover_logo_hidden = False
            elif data.get(field) in ('', False, 'remove'):
                getattr(row, field).delete(save=False)
                setattr(row, field, '')
        # Cover-logo visibility flag: uploading a cover image implies showing
        # it; an explicit false/true (or the string forms) toggles hiding the
        # cover logo entirely — including the statutory default.
        if 'cover_logo_hidden' in data:
            raw = data['cover_logo_hidden']
            if isinstance(raw, str):
                row.cover_logo_hidden = raw.strip().lower() in ('true', '1', 'yes')
            else:
                row.cover_logo_hidden = bool(raw)
            if row.cover_logo_hidden:
                row.cover_logo.delete(save=False)
                row.cover_logo = ''
        row.updated_by = (request.user
                          if getattr(request.user, 'is_authenticated', False)
                          else None)
        row.save()
        record_audit(request.user, 'report_branding_updated',
                     'ReportBranding', row.id)
        return Response(self._payload(row))

    def delete(self, request, project_id):
        from apps.evidence.review import record_audit
        from .models import ReportBranding
        if not IsCMSDirector().has_permission(request, self):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row = getattr(project, 'report_branding', None)
        if row is not None:
            row.logo.delete(save=False)
            row.watermark.delete(save=False)
            row.cover_logo.delete(save=False)
            row.delete()
            record_audit(request.user, 'report_branding_removed',
                         'ReportBranding', project_id)
        return Response({'branding_configured': False})

    @staticmethod
    def _payload(row):
        def _url(f):
            try:
                return f.url if f else None
            except Exception:  # noqa: BLE001 — remote storage may raise
                return None
        return {
            'branding_configured': True,
            'logo_url': _url(row.logo),
            'logo_position': row.logo_position,
            'logo_size': row.logo_size,
            'cover_logo_url': _url(row.cover_logo),
            'cover_logo_hidden': row.cover_logo_hidden,
            'watermark_url': _url(row.watermark),
            'watermark_opacity_pct': row.watermark_opacity_pct,
            'watermark_position': row.watermark_position,
            'updated_at': row.updated_at,
        }


# ---------------------------------------------------------------------------
# Approving-engineer sign-off credentials (C11, 4 Sep meeting): the
# COREN-registered engineer who gives final input before the report is
# issued. Director-gated — the credentials are part of the statutory
# document, so who may set them is restricted. Every value is typed by a
# Director; nothing is derived from platform data.
# ---------------------------------------------------------------------------
class ReportSignOffView(APIView):
    """
    GET    /api/v1/reports/projects/{project_id}/signoff/
    PATCH  /api/v1/reports/projects/{project_id}/signoff/  (multipart:
           text fields + optional signature_image)
    DELETE /api/v1/reports/projects/{project_id}/signoff/  (remove the row)
    """
    permission_classes = [IsAuthenticated]

    _TEXT_FIELDS = ('approved_by_name', 'qualification',
                    'coren_registration_no', 'firm_name')
    # Per-field limits, kept in lockstep with the model's max_lengths so a
    # view-level pass can never reach the DB and raise there.
    _FIELD_MAX = {'approved_by_name': 150, 'qualification': 150,
                  'coren_registration_no': 60, 'firm_name': 150}

    def get(self, request, project_id):
        from .models import ReportSignOff
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row = getattr(project, 'report_signoff', None)
        if row is None:
            return Response({'signoff_configured': False})
        return Response(self._payload(row))

    def patch(self, request, project_id):
        from apps.evidence.review import record_audit
        from .models import ReportSignOff
        if not IsCMSDirector().has_permission(request, self):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row, _ = ReportSignOff.objects.get_or_create(project=project)
        data = request.data
        sig = data.get('signature_image')
        if sig is not None and hasattr(sig, 'content_type') and \
                sig.content_type not in ('image/png', 'image/jpeg'):
            return Response(
                {'detail': 'signature_image: PNG or JPEG images only.'},
                status=status.HTTP_400_BAD_REQUEST)
        for field in self._TEXT_FIELDS:
            if field in data:
                value = data.get(field)
                if value is None:
                    value = ''
                value = str(value).strip()
                max_len = self._FIELD_MAX[field]
                if len(value) > max_len:
                    return Response(
                        {'detail': f'{field}: maximum {max_len} characters.'},
                        status=status.HTTP_400_BAD_REQUEST)
                setattr(row, field, value)
        if hasattr(sig, 'read'):
            row.signature_image.save(
                f'signoff_{project_id}_{sig.name}', sig, save=False)
        elif sig in ('', False, 'remove'):
            row.signature_image.delete(save=False)
            row.signature_image = ''
        row.updated_by = (request.user
                          if getattr(request.user, 'is_authenticated', False)
                          else None)
        row.save()
        record_audit(request.user, 'report_signoff_updated',
                     'ReportSignOff', row.id)
        return Response(self._payload(row))

    def delete(self, request, project_id):
        from apps.evidence.review import record_audit
        from .models import ReportSignOff
        if not IsCMSDirector().has_permission(request, self):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        row = getattr(project, 'report_signoff', None)
        if row is not None:
            row.signature_image.delete(save=False)
            row.delete()
            record_audit(request.user, 'report_signoff_removed',
                         'ReportSignOff', project_id)
        return Response({'signoff_configured': False})

    @staticmethod
    def _payload(row):
        def _url(f):
            try:
                return f.url if f else None
            except Exception:  # noqa: BLE001 — remote storage may raise
                return None
        return {
            'signoff_configured': True,
            'approved_by_name': row.approved_by_name,
            'qualification': row.qualification,
            'coren_registration_no': row.coren_registration_no,
            'firm_name': row.firm_name,
            'signature_image_url': _url(row.signature_image),
            'updated_at': row.updated_at,
        }


# ---------------------------------------------------------------------------
# Report map data (REFINED EXECUTIVE SUMMARY §2.4): the real test-point
# coordinates + project location for the interactive map. Only recorded
# coordinates are returned — a test without a position is simply absent,
# never placed at a fabricated location.
# ---------------------------------------------------------------------------
class ReportMapView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/map/
    GeoJSON-style payload for the interactive location map: project
    centre (recorded latitude/longitude or None), one marker per PUNDIT
    test that carries coordinates, the per-marker strength facts the
    legend colour-codes by, and one polygon per GNSS boundary survey
    (only when real boundary points were recorded).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.digital_eye.models import GnssBoundaryPoint, PUNDITTest
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        features = []
        for t in (PUNDITTest.objects
                  .filter(project=project, latitude__isnull=False,
                          longitude__isnull=False)
                  .exclude(latitude=0, longitude=0)):
            velocity = t.pulse_velocity_ms
            props = {
                'element': t.structural_element or 'element',
                'floor': t.floor or '',
                'grid_location': t.test_location or '',
                'tested_at': t.tested_at,
                'velocity_m_s': velocity,
            }
            # Same f_cu path as every other surface (the project's active
            # calibration curve; honest None outside the calibrated range).
            from apps.digital_eye.strength_curves import apply_active_curve
            v_km_s = (t.velocity_km_s if t.velocity_km_s is not None
                      else (velocity / 1000.0 if velocity else None))
            strength = None
            if v_km_s is not None:
                strength, _snapshot = apply_active_curve(
                    project, v_km_s, rebound_number=t.rebound_number,
                    temperature_c=t.surface_temperature_c)
            props['strength_n_mm2'] = strength
            if strength is not None:
                props['band'] = ('good' if strength >= 25.0 else 'poor')
            elif velocity is not None:
                props['band'] = 'unassessed'
            else:
                props['band'] = 'no_velocity'
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'Point',
                             'coordinates': [t.longitude, t.latitude]},
                'properties': props,
            })
        # Site boundary polygons (spec §2.4): one polygon per GNSS survey
        # that recorded ordered boundary points. Only real surveyed
        # coordinates are used — a project with no boundary survey gets an
        # empty list, never an estimated site extent.
        boundary_polygons = []
        boundary_qs = (GnssBoundaryPoint.objects
                       .filter(survey__project=project)
                       .select_related('survey')
                       .order_by('survey__created_at', 'survey_id',
                                 'sequence'))
        per_survey = {}
        for point in boundary_qs:
            per_survey.setdefault(point.survey_id,
                                  {'survey': point.survey, 'ring': []})
            per_survey[point.survey_id]['ring'].append(
                [point.longitude, point.latitude])
        for entry in per_survey.values():
            ring = entry['ring']
            # A closed GeoJSON ring needs the first point repeated at the
            # end; a polygon needs at least 3 distinct vertices.
            if len(ring) < 3:
                continue
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]
            boundary_polygons.append({
                'survey_reference': entry['survey'].survey_reference,
                'title': entry['survey'].title,
                'polygon': {'type': 'Polygon', 'coordinates': [ring]},
            })
        return Response({
            'project_center': (
                None if project.latitude is None or project.longitude is None
                else [project.longitude, project.latitude]),
            'site_address': project.site_address or '',
            'test_points': {'type': 'FeatureCollection',
                            'features': features},
            'boundary_polygons': boundary_polygons,
            'legend': {
                'good': 'Strength >= 25 N/mm2 (statutory pass)',
                'poor': 'Strength < 25 N/mm2 (requires technical advice)',
                'unassessed': 'Velocity recorded, outside the calibrated '
                              'range — no strength estimate',
                'no_velocity': 'No computable velocity recorded',
            },
        })
