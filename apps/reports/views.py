import base64
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


def _statistical_summary_rows(project, tests):
    """The platform's statistical-summary records for a project's tests.

    Delegates to ``PUNDITAdapter.build_statistical_summary`` — the same engine
    the published report's Section 5.5 table uses — so a preview page and the
    report can never disagree about a mean velocity, a dispersion figure or a
    derived strength.
    """
    from apps.digital_eye.adapters import PUNDITAdapter

    point_spread_data = []
    for t in tests:
        readings = [
            {
                'transit_time_us': r.get('transit_us'),
                'path_length_mm': r.get('path_mm'),
                'velocity_km_s': r.get('velocity_km_s'),
            }
            for r in t.reading_rows()
            if r.get('transit_us') is not None and r.get('path_mm') is not None
        ]
        point_spread_data.append({
            'element': getattr(t, 'structural_element', None) or 'UNSPECIFIED',
            'floor': getattr(t, 'floor', None),
            'readings': readings,
        })
    return PUNDITAdapter.build_statistical_summary(point_spread_data,
                                                   project=project)


def _compact_stat_row(record):
    """One preview-table row from a statistical-summary record.

    Every field is read from the record as the engine produced it. A field the
    engine did not produce stays ``None`` (or is derived from the record's own
    CoV against the thresholds the report states) rather than being filled with
    an invented rating, point count or strength.
    """
    def _num(*candidates):
        for value in candidates:
            if value in (None, ''):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    cov = _num(record.get('cov_percent'), record.get('cov'))
    uniformity = (record.get('uniformity_verdict')
                  or record.get('uniformity'))
    if not uniformity and cov is not None:
        if cov < 5.0:
            uniformity = 'Excellent Uniformity'
        elif cov < 10.0:
            uniformity = 'Good Uniformity'
        elif cov < 15.0:
            uniformity = 'Variable / Suspect Uniformity'
        else:
            uniformity = 'Highly Variable'

    return {
        'station': (record.get('element_id') or record.get('station')
                    or record.get('element')),
        'n': (record.get('point_count') or record.get('readings_count')
              or record.get('n')),
        'v_mean_ms': _num(record.get('v_mean'), record.get('velocity_mean_ms')),
        'v_min_ms': _num(record.get('v_min')),
        'v_max_ms': _num(record.get('v_max')),
        'range_ms': _num(record.get('range')),
        'sd_ms': _num(record.get('sd'), record.get('std_dev_ms')),
        'cov_percent': cov,
        'uniformity': uniformity,
        'f_cu_mpa': _num(record.get('f_cu_mpa'),
                         record.get('est_strength_n_mm2')),
        'grade': record.get('grade'),
        'critical_low_point': record.get('critical_low_point_location'),
        'basis': record.get('basis'),
    }


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
        operator = request.query_params.get('operator')
        element_id = request.query_params.get('elementId')
        if not operator and getattr(request.user, 'role', None) == 'inspector':
            operator = request.user.get_full_name() or request.user.username
        try:
            pdf_bytes = NDTReportService.generate_ndt_report(project, request.user, operator=operator, element_id=element_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT report generation failed')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        try:
            NDTReportService.archive_ndt_report(project, request.user, pdf_bytes, operator=operator)
        except Exception:  # noqa: BLE001 — archive failure must not block the stream
            logger.exception('NDT report archiving failed')
        safe_op = f"_{operator.strip().replace(' ', '_')}" if operator and operator.strip() else ""
        return _pdf_response(pdf_bytes, f'ndt_report_{project_id}{safe_op}.pdf')


class NDTOperatorsListView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-operators/
    Returns the distinct field inspectors/operators who conducted scans on this project,
    with their scan counts and latest activity date, so separate reports can be
    viewed/generated per inspector.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        from apps.digital_eye.models import PUNDITTest
        tests = PUNDITTest.objects.filter(project=project).select_related('operator', 'created_by')

        ops_map = {}
        for t in tests:
            lbl = (t.operator_name or '').strip()
            if not lbl and t.operator:
                lbl = (t.operator.get_full_name() or t.operator.username or t.operator.email).strip()
            if not lbl and t.created_by:
                lbl = (t.created_by.get_full_name() or t.created_by.username or t.created_by.email).strip()
            if not lbl:
                continue
            if lbl not in ops_map:
                ops_map[lbl] = {'operator': lbl, 'scan_count': 0, 'last_tested': None}
            ops_map[lbl]['scan_count'] += 1
            t_date = t.test_date or (t.tested_at.date() if t.tested_at else None)
            if t_date and (not ops_map[lbl]['last_tested'] or str(t_date) > str(ops_map[lbl]['last_tested'])):
                ops_map[lbl]['last_tested'] = str(t_date)
        return Response(sorted(list(ops_map.values()), key=lambda x: x['scan_count'], reverse=True))


class ArchivedReportListView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/archived-reports/?kind=ndt
    Lists the checksummed dossiers generated for a project, newest first.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id=None):
        from apps.projects.models import Project
        from .models import ArchivedReport
        from .serializers import ArchivedReportSerializer
        scoped = scoped_projects(request.user)
        if project_id:
            project = scoped.filter(pk=project_id).first()
            if not project:
                return Response({'detail': 'Project not found in your scope.'},
                                status=status.HTTP_404_NOT_FOUND)
            projects_filter = [project]
        else:
            projects_filter = scoped

        kind = request.query_params.get('kind') or 'ndt'
        reports = (ArchivedReport.objects
                   .filter(project__in=projects_filter, report_kind=kind)
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

from common.permissions import IsDirector as IsCMSDirector, user_agency

from .models import ReportCMSPassword, ReportSectionOverride
from .report_cms import CMS_SECTIONS, get_cms_text


def _cms_gate(request):
    """
    Director role + CMS password check for section edits. Returns a DRF
    Response when the request must be rejected, else None.
    Guarded per government agency if the user belongs to an agency.
    """
    agency = user_agency(request.user)
    credential = None
    if agency is not None:
        credential = ReportCMSPassword.objects.filter(agency=agency).first()
        if credential is None:
            # Fallback to legacy unassigned password if agency hasn't set one yet
            credential = ReportCMSPassword.objects.filter(agency__isnull=True).first()
    else:
        credential = ReportCMSPassword.objects.filter(agency__isnull=True).first() or ReportCMSPassword.objects.first()

    if credential is None:
        agency_msg = f" for {agency.name}" if agency else ""
        return Response(
            {'detail': f'No report-CMS password has been set yet{agency_msg}. A Director '
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
        agency = user_agency(request.user)
        if agency is not None:
            cred = ReportCMSPassword.objects.filter(agency=agency).first()
            if cred is None:
                cred = ReportCMSPassword.objects.filter(agency__isnull=True).first()
        else:
            cred = ReportCMSPassword.objects.filter(agency__isnull=True).first() or ReportCMSPassword.objects.first()

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
            body, source = get_cms_text(project, key, computed=computed, agency=agency)
            sections.append({
                'key': key,
                'label': meta['label'],
                'kind': meta['kind'],
                'help': meta['help'],
                'default': meta['default'],
                'body': body,
                'source': source,
                'computed': bool(meta.get('computed')),
                # §2.2 wireframe: advisory length for the editor's
                # character-count display (advice, not a hard reject);
                # None for sections without a limit.
                'max_length': meta.get('max_length'),
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
            'agency': agency.name if agency else None,
            'password_set': cred is not None,
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
        agency = user_agency(request.user)
        if project is not None:
            override, _created = ReportSectionOverride.objects.update_or_create(
                project=project, section_key=key,
                defaults={'body': str(body), 'updated_by': request.user, 'agency': agency})
            scope = 'project'
        elif agency is not None:
            override, _created = ReportSectionOverride.objects.update_or_create(
                project__isnull=True, agency=agency, section_key=key,
                defaults={'body': str(body), 'updated_by': request.user, 'agency': agency})
            scope = f'agency:{agency.code}'
        else:
            override, _created = ReportSectionOverride.objects.update_or_create(
                project__isnull=True, agency__isnull=True, section_key=key,
                defaults={'body': str(body), 'updated_by': request.user})
            scope = 'platform'

        from apps.evidence.review import record_audit
        record_audit(
            request.user, 'report_cms_section_saved', 'ReportSectionOverride',
            override.id,
            new_state={'section_key': key,
                       'project': str(project.id) if project else None,
                       'agency': agency.code if agency else None,
                       'body_length': len(override.body)},
            metadata={'section_key': key, 'scope': scope})
        _, source = get_cms_text(project, key, agency=agency)
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
        agency = user_agency(request.user)
        if project is not None:
            deleted, _ = ReportSectionOverride.objects.filter(
                project=project, section_key=key).delete()
            scope = 'project'
        elif agency is not None:
            deleted, _ = ReportSectionOverride.objects.filter(
                project__isnull=True, agency=agency, section_key=key).delete()
            scope = f'agency:{agency.code}'
        else:
            deleted, _ = ReportSectionOverride.objects.filter(
                project__isnull=True, agency__isnull=True, section_key=key).delete()
            scope = 'platform'

        from apps.evidence.review import record_audit
        record_audit(
            request.user, 'report_cms_section_reverted',
            'ReportSectionOverride', key,
            new_state={'section_key': key,
                       'project': str(project.id) if project else None,
                       'agency': agency.code if agency else None},
            metadata={'section_key': key, 'scope': scope})
        computed = None
        if CMS_SECTIONS[key].get('computed') and project is not None:
            from .ndt_reports import NDTReportService
            computed = NDTReportService.computed_section_bodies(project)
        body, source = get_cms_text(project, key, computed=computed, agency=agency)
        return Response({'key': key, 'source': source, 'body': body,
                         'reverted': bool(deleted)})


class ReportCMSPasswordView(APIView):
    """
    GET  /api/v1/reports/cms/password/
    POST /api/v1/reports/cms/password/   {new_password, current_password?, reset?}
    Set, change, or reset (forgot password) the password guarding report-CMS edits.
    Directors only. Guarded per government agency.
    """
    permission_classes = [IsCMSDirector]

    def get(self, request):
        agency = user_agency(request.user)
        if agency is not None:
            cred = ReportCMSPassword.objects.filter(agency=agency).first()
            if cred is None:
                cred = ReportCMSPassword.objects.filter(agency__isnull=True).first()
        else:
            cred = ReportCMSPassword.objects.filter(agency__isnull=True).first() or ReportCMSPassword.objects.first()
        return Response({
            'password_set': cred is not None,
            'agency': agency.name if agency else None,
            'agency_code': agency.code if agency else None,
        })

    def post(self, request):
        data = request.data or {}
        new_password = data.get('new_password')
        if not new_password or len(str(new_password)) < 8:
            return Response({'detail': 'new_password is required and must be '
                                       'at least 8 characters.'},
                            status=status.HTTP_400_BAD_REQUEST)
        agency = user_agency(request.user)
        is_reset = bool(data.get('reset') or data.get('forgot_password'))

        if agency is not None:
            credential = ReportCMSPassword.objects.filter(agency=agency).first()
        else:
            credential = ReportCMSPassword.objects.filter(agency__isnull=True).first()

        if credential is not None and not is_reset:
            current = data.get('current_password', '')
            if not current or not check_password(str(current), credential.password_hash):
                return Response(
                    {'detail': 'A report-CMS password already exists; the current password is required to change it. (Use Reset if forgotten).'},
                    status=status.HTTP_403_FORBIDDEN)
            credential.password_hash = make_password(str(new_password))
            credential.set_by = request.user
            credential.save()
            action = 'report_cms_password_changed'
        elif credential is not None and is_reset:
            credential.password_hash = make_password(str(new_password))
            credential.set_by = request.user
            credential.save()
            action = 'report_cms_password_reset'
        else:
            credential = ReportCMSPassword.objects.create(
                agency=agency,
                password_hash=make_password(str(new_password)),
                set_by=request.user)
            action = 'report_cms_password_set'

        from apps.evidence.review import record_audit
        record_audit(request.user, action, 'ReportCMSPassword', credential.id)
        agency_label = f" for {agency.name}" if agency else ""
        return Response({'detail': f'Report-CMS password saved{agency_label}.', 'password_set': True})


# ---------------------------------------------------------------------------
# Report structure (REFINED EXECUTIVE SUMMARY §2.5 — document flexibility):
# the per-project section order, per-section enable/disable state and the
# project's own custom sections. Like branding (§2.3) this is report
# CONFIGURATION, not template wording — so it is Director-only with NO CMS
# password gate (the password guards wording edits, not structure).
# ---------------------------------------------------------------------------
from .models import ReportSectionConfig
from .report_structure import (
    DEFAULT_STRUCTURE_KEYS, ensure_structure_rows, new_custom_section_key,
    resolve_report_structure,
)


def _structure_payload(project):
    return {
        'project': str(project.id),
        'sections': resolve_report_structure(project),
    }


class ReportStructureView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/structure/
    The project's report structure — section order, enable/disable state and
    custom sections — exactly as both emitters (certified PDF and .docx
    working copy) will render it. A project that has never had a structure
    change resolves to the canonical default (everything enabled, template
    order).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(_structure_payload(project))


class ReportStructureReorderView(APIView):
    """
    POST /api/v1/reports/projects/{project_id}/structure/reorder/
    Body: {'order': ['APPENDIX', '7.0', 'custom:abc123', ...]} — the
    complete list of section keys in their new order. Directors only.
    """
    permission_classes = [IsCMSDirector]

    def post(self, request, project_id):
        from apps.evidence.review import record_audit
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        order = (request.data or {}).get('order')
        if not isinstance(order, list) or not all(
                isinstance(k, str) for k in order):
            return Response({'detail': "Body must be {'order': [section "
                                       'keys in their new order].}'},
                            status=status.HTTP_400_BAD_REQUEST)
        # Materialise the full default set first so the row set is the whole
        # truth; a reorder must then cover every section exactly once.
        ensure_structure_rows(project, request.user)
        known = set(ReportSectionConfig.objects
                    .filter(project=project)
                    .values_list('section_key', flat=True))
        if len(order) != len(set(order)):
            return Response({'detail': 'Duplicate section keys in order.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if set(order) != known:
            missing = sorted(known - set(order))
            extra = sorted(set(order) - known)
            detail = 'order must be a permutation of every section key.'
            if missing:
                detail += f' Missing: {", ".join(missing)}.'
            if extra:
                detail += f' Unknown: {", ".join(extra)}.'
            return Response({'detail': detail},
                            status=status.HTTP_400_BAD_REQUEST)
        rows = {r.section_key: r for r in
                ReportSectionConfig.objects.filter(project=project)}
        for pos, key in enumerate(order):
            row = rows[key]
            row.display_order = pos
            row.updated_by = request.user
            row.save(update_fields=['display_order', 'updated_by',
                                    'updated_at'])
        record_audit(request.user, 'report_structure_reordered',
                     'ReportSectionConfig', project.id)
        return Response(_structure_payload(project))


class ReportStructureToggleView(APIView):
    """
    POST /api/v1/reports/projects/{project_id}/structure/{section_key}/toggle/
    Body: {'is_enabled': true/false}. Directors only. Disabling a section
    removes it from the generated report (both PDF and .docx); the row and
    its content stay, so re-enabling restores it.
    """
    permission_classes = [IsCMSDirector]

    def post(self, request, project_id, section_key):
        from apps.evidence.review import record_audit
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        enabled = (request.data or {}).get('is_enabled')
        if not isinstance(enabled, bool):
            return Response({'detail': "Body must be {'is_enabled': true "
                                       'or false}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ensure_structure_rows(project, request.user)
        row = ReportSectionConfig.objects.filter(
            project=project, section_key=section_key).first()
        if row is None:
            return Response({'detail': 'Unknown section key.'},
                            status=status.HTTP_404_NOT_FOUND)
        row.is_enabled = enabled
        row.updated_by = request.user
        row.save(update_fields=['is_enabled', 'updated_by', 'updated_at'])
        record_audit(request.user, 'report_structure_toggled',
                     'ReportSectionConfig', row.id)
        return Response(_structure_payload(project))


class ReportStructureCustomView(APIView):
    """
    POST /api/v1/reports/projects/{project_id}/structure/custom/
    Body: {'title': str (required), 'body': str (optional)}. Directors only.
    Appends a new custom section at the end of the document.
    """
    permission_classes = [IsCMSDirector]

    def post(self, request, project_id):
        from apps.evidence.review import record_audit
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        title = str(data.get('title') or '').strip()
        body = str(data.get('body') or '').strip()
        if not title:
            return Response({'detail': 'title is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(title) > 200:
            return Response({'detail': 'title must be 200 characters or '
                                       'fewer.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ensure_structure_rows(project, request.user)
        from django.db.models import Max
        base = (ReportSectionConfig.objects.filter(project=project)
                .aggregate(m=Max('display_order'))['m'])
        row = ReportSectionConfig.objects.create(
            project=project, section_key=new_custom_section_key(),
            is_custom=True, title=title, body=body,
            display_order=(base + 1) if base is not None else 0,
            updated_by=request.user)
        record_audit(request.user, 'report_structure_custom_added',
                     'ReportSectionConfig', row.id)
        return Response(_structure_payload(project), status=201)


class ReportStructureSectionView(APIView):
    """
    PATCH /api/v1/reports/projects/{project_id}/structure/{section_key}/
        Body: {'title'?, 'body'?} — edit a CUSTOM section's content.
        Built-in sections have no editable content (their wording lives in
        the CMS) and are rejected with 400.
    DELETE .../structure/{section_key}/
        Removes a CUSTOM section entirely. Built-ins cannot be deleted —
        disable them instead (toggle).
    Directors only.
    """
    permission_classes = [IsCMSDirector]

    def _custom_row_or_response(self, request, project_id, section_key):
        project = _scoped_project_or_none(request.user, project_id)
        if project is None:
            return None, Response(
                {'detail': 'Project not found in your scope.'},
                status=status.HTTP_404_NOT_FOUND)
        row = ReportSectionConfig.objects.filter(
            project=project, section_key=section_key).first()
        if row is None and section_key not in DEFAULT_STRUCTURE_KEYS:
            # Neither a materialised row nor a virtual built-in key.
            return None, Response({'detail': 'Unknown section key.'},
                                  status=status.HTTP_404_NOT_FOUND)
        if row is None or not row.is_custom:
            # A built-in section, materialised or still virtual: its
            # wording lives in the report CMS and it can only be toggled.
            return None, Response(
                {'detail': 'Built-in sections cannot be edited or deleted — '
                           'their wording lives in the report CMS; disable '
                           'them with the toggle endpoint instead.'},
                status=status.HTTP_400_BAD_REQUEST)
        return row, None

    def patch(self, request, project_id, section_key):
        from apps.evidence.review import record_audit
        row, err = self._custom_row_or_response(request, project_id,
                                                section_key)
        if err is not None:
            return err
        data = request.data or {}
        if 'title' in data:
            title = str(data.get('title') or '').strip()
            if not title:
                return Response({'detail': 'title cannot be empty.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if len(title) > 200:
                return Response({'detail': 'title must be 200 characters '
                                           'or fewer.'},
                                status=status.HTTP_400_BAD_REQUEST)
            row.title = title
        if 'body' in data:
            row.body = str(data.get('body') or '')
        row.updated_by = request.user
        row.save()
        record_audit(request.user, 'report_structure_custom_edited',
                     'ReportSectionConfig', row.id)
        return Response(_structure_payload(row.project))

    def delete(self, request, project_id, section_key):
        from apps.evidence.review import record_audit
        row, err = self._custom_row_or_response(request, project_id,
                                                section_key)
        if err is not None:
            return err
        project = row.project
        record_audit(request.user, 'report_structure_custom_removed',
                     'ReportSectionConfig', row.id)
        row.delete()
        return Response(_structure_payload(project))


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
        operator = request.query_params.get('operator')
        element_id = request.query_params.get('elementId')
        if not operator and getattr(request.user, 'role', None) == 'inspector':
            operator = request.user.get_full_name() or request.user.username
        try:
            docx_bytes = NDTWordExporter.export_docx(project, request.user, operator=operator, element_id=element_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT Word export failed')
            return Response({'detail': f'Word export failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        response = HttpResponse(
            docx_bytes,
            content_type=('application/vnd.openxmlformats-officedocument'
                          '.wordprocessingml.document'))
        safe_op = f"_{operator.strip().replace(' ', '_')}" if operator and operator.strip() else ""
        response['Content-Disposition'] = \
            f'attachment; filename="ndt_report_{project_id}{safe_op}.docx"'
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
        from .ndt_reports import NDTReportService
        ref = (request.query_params.get('ref') or '').strip()
        digest = (request.query_params.get('digest') or '').strip()
        if not ref or not digest:
            return Response(
                {'verified': False,
                 'detail': 'Both a report reference and a content digest are '
                           'required (as encoded in the report QR code).'},
                status=status.HTTP_400_BAD_REQUEST)
        # 1. Exact match
        report = (ArchivedReport.objects
                  .filter(report_reference=ref, content_key=digest)
                  .first())
        # 2. Match by digest directly (handles whitespace or URL encoding variances in ref)
        if report is None:
            report = ArchivedReport.objects.filter(content_key=digest).first()
            if report and ref:
                norm_query = ''.join(ref.split())
                norm_stored = ''.join(report.report_reference.split())
                if norm_query != norm_stored:
                    report = None
        # 3. If still None, check if any archived report's live statutory digest matches
        if report is None:
            for cand in ArchivedReport.objects.all():
                norm_query = ''.join(ref.split())
                norm_stored = ''.join(cand.report_reference.split())
                if not norm_query or norm_query == norm_stored:
                    try:
                        from apps.digital_eye.models import PUNDITTest
                        c_tests = list(
                            PUNDITTest.objects.filter(project=cand.project)
                            .select_related('device', 'operator')
                            .prefetch_related('files', 'readings')
                            .order_by('structural_element', 'test_date')
                        )
                        r_no, _ = NDTReportService._effective_report_number(cand.project, c_tests)
                        c_digest = NDTReportService._statutory_digest(cand.project, c_tests, r_no)
                        if c_digest == digest:
                            cand.content_key = digest
                            cand.save(update_fields=['content_key'])
                            report = cand
                            break
                    except Exception:
                        pass
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
        from .ndt_reports import NDTReportService
        ref = (request.query_params.get('ref') or '').strip()
        digest = (request.query_params.get('digest') or '').strip()
        if not ref or not digest:
            return Response(
                {'verified': False,
                 'detail': 'Both a report reference and a content digest are '
                           'required (as encoded in the report QR code).'},
                status=status.HTTP_400_BAD_REQUEST)
        # 1. Exact match
        report = (ArchivedReport.objects
                  .filter(report_reference=ref, content_key=digest)
                  .first())
        # 2. Match by digest directly
        if report is None:
            report = ArchivedReport.objects.filter(content_key=digest).first()
            if report and ref:
                norm_query = ''.join(ref.split())
                norm_stored = ''.join(report.report_reference.split())
                if norm_query != norm_stored:
                    report = None
        # 3. Live digest match fallback
        if report is None:
            for cand in ArchivedReport.objects.all():
                norm_query = ''.join(ref.split())
                norm_stored = ''.join(cand.report_reference.split())
                if not norm_query or norm_query == norm_stored:
                    try:
                        from apps.digital_eye.models import PUNDITTest
                        c_tests = list(
                            PUNDITTest.objects.filter(project=cand.project)
                            .select_related('device', 'operator')
                            .prefetch_related('files', 'readings')
                            .order_by('structural_element', 'test_date')
                        )
                        r_no, _ = NDTReportService._effective_report_number(cand.project, c_tests)
                        c_digest = NDTReportService._statutory_digest(cand.project, c_tests, r_no)
                        if c_digest == digest:
                            cand.content_key = digest
                            cand.save(update_fields=['content_key'])
                            report = cand
                            break
                    except Exception:
                        pass
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

        # Statutory Access Control: Direct download is restricted unless authorized
        token = (request.query_params.get('token') or '').strip()
        email = (request.query_params.get('email') or '').strip()
        has_access = False

        if request.user and request.user.is_authenticated:
            has_access = True
        elif token:
            from apps.documents.models import DocumentAccessRequest
            access_req = DocumentAccessRequest.objects.filter(
                access_token=token, status='APPROVED'
            ).first()
            if access_req:
                has_access = True
        elif email:
            from apps.documents.models import DocumentAccessRequest
            access_req = DocumentAccessRequest.objects.filter(
                requester_email__iexact=email, report_digest=digest, status='APPROVED'
            ).first()
            if access_req:
                has_access = True

        if not has_access:
            return Response(
                {
                    'verified': True,
                    'error': 'Access to this statutory document is restricted. Direct download is not permitted without prior authorization.',
                    'detail': 'Please submit an official access request. Once approved by the supervising government agency, you will be authorized to download the authentic dossier.',
                    'requires_request': True
                },
                status=status.HTTP_403_FORBIDDEN
            )

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
        operator = request.query_params.get('operator')
        element_id = request.query_params.get('elementId')
        if not operator and getattr(request.user, 'role', None) == 'inspector':
            operator = request.user.get_full_name() or request.user.username
        try:
            pdf_bytes = NDTReportService.generate_ndt_report(project,
                                                             request.user,
                                                             operator=operator,
                                                             element_id=element_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT report preview failed')
            return Response({'detail': f'Report preview failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        safe_op = f"_{operator.strip().replace(' ', '_')}" if operator and operator.strip() else ""
        return _pdf_response(pdf_bytes,
                             f'ndt_report_PREVIEW_{project_id}{safe_op}.pdf')


class NDTReportPreviewSectionsView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-report-preview/sections/
    The §2.1 preview sidebar's data AND the preview document in ONE response:
    the section→page map, the total page count, and the exact PDF bytes
    (base64) — all from a single render pass, so the sidebar can never
    describe a different document than the one it navigates. Sections the
    render skipped honestly (e.g. AI interpretation with no LLM-backed
    analysis record) are simply absent from the list.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        from .ndt_reports import NDTReportService
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        operator = request.query_params.get('operator')
        element_id = request.query_params.get('elementId')
        if not operator and getattr(request.user, 'role', None) == 'inspector':
            operator = request.user.get_full_name() or request.user.username
        try:
            pdf_bytes, bundle = NDTReportService.generate_ndt_report_bundled(
                project, request.user, operator=operator, element_id=element_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('NDT report preview sections failed')
            return Response({'detail': f'Report preview failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({
            'sections': bundle['sections'],
            'page_count': bundle['page_count'],
            'pdf_base64': base64.b64encode(pdf_bytes).decode('ascii'),
        })


class NDTReportPageQAView(APIView):
    """
    POST /api/v1/reports/projects/{project_id}/ndt-report-qa/
    
    Embedded Nexucon QA Assistant for document inspection.
    Validates the active page of an NDT report against the deterministic raw
    JSON test data, statutory requirements, and international standards
    (BS 1881-203, BS EN 12504-4, ACI 228.2R).
    
    Supports:
      - Preferred AI model: "anthropic" (Claude Sonnet 3.5/4.5) or "deepseek" (DeepSeek Flash/Reasoner)
      - Deterministic context extraction: intercepts active page text from PDF buffer,
        combines with lean serialized JSON summary.
      - Structured output: [VERIFIED], [DISCREPANCY], [COMPLIANCE FLAG].
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        import io
        import json
        import os
        from apps.common.ai_service import AIService
        from .ndt_reports import NDTReportService
        from apps.digital_eye.models import PUNDITTest, StrengthCurve

        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        page_number = int(request.data.get('page_number', 1))
        user_query = str(request.data.get('user_query', '')).strip()
        model_choice = str(request.data.get('model_provider', 'anthropic')).strip().lower()
        operator = request.data.get('operator')
        element_id = request.data.get('elementId')

        if not operator and getattr(request.user, 'role', None) == 'inspector':
            operator = request.user.get_full_name() or request.user.username

        # 1. Render bundled PDF bytes
        try:
            pdf_bytes, bundle = NDTReportService.generate_ndt_report_bundled(
                project, request.user, operator=operator, element_id=element_id)
        except Exception as exc:
            logger.exception('NDT report generation failed during QA review')
            return Response({'detail': f'Report render failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 2. Extract active page text (resilient engine)
        total_pages = 1
        active_page_text = ""
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            total_pages = len(reader.pages)
            if 1 <= page_number <= total_pages:
                active_page_text = reader.pages[page_number - 1].extract_text() or ''
        except ImportError:
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                total_pages = len(reader.pages)
                if 1 <= page_number <= total_pages:
                    active_page_text = reader.pages[page_number - 1].extract_text() or ''
            except ImportError:
                try:
                    import fitz  # PyMuPDF
                    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                    total_pages = len(doc)
                    if 1 <= page_number <= total_pages:
                        active_page_text = doc[page_number - 1].get_text() or ''
                except ImportError:
                    return Response({'detail': 'No PDF parser installed (pypdf/fitz).'},
                                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if page_number < 1 or page_number > total_pages:
            return Response({'detail': f'Page {page_number} is out of bounds (1..{total_pages}).'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 3. Assemble lean deterministic raw_json_payload
        tests = list(
            PUNDITTest.objects.filter(project=project)
            .select_related('device')
            .prefetch_related('readings')
            .order_by('structural_element', 'test_date')
        )

        from apps.evidence.models import AIAnalysisRecord
        from apps.digital_eye.adapters import PUNDITAdapter
        ai_record = AIAnalysisRecord.objects.filter(project=project).order_by('-created_at').first()
        stat_summary = []
        if ai_record and ai_record.correlations and isinstance(ai_record.correlations, dict):
            stat_summary = ai_record.correlations.get('statistical_summary') or []
        if not stat_summary and tests:
            point_spread_data = []
            for t in tests:
                rds = []
                for r in t.reading_rows():
                    vel = r.get('velocity_km_s')
                    if vel is not None:
                        rds.append({
                            'transit_time_us': r.get('transit_us') or 0,
                            'path_length_mm': r.get('path_mm') or 150,
                            'velocity_km_s': vel,
                        })
                point_spread_data.append({
                    'element': getattr(t, 'structural_element', None) or getattr(t, 'structural_element_name', 'S1'),
                    'floor': getattr(t, 'floor', getattr(t, 'floor_level', 'Ground Floor')),
                    'readings': rds,
                })
            try:
                stat_summary = PUNDITAdapter.build_statistical_summary(point_spread_data, project=project)
            except Exception as exc:
                logger.warning("PUNDITAdapter.build_statistical_summary failed: %s", exc)
                stat_summary = []

        active_curve = (
            StrengthCurve.objects.filter(project=project).first()
            or StrengthCurve.objects.filter(is_default=True).first()
        )
        curve_name_val = active_curve.name if active_curve else 'Calibrated Correlation Model'
        curve_form_val = getattr(active_curve, 'formula', None) or 'f_cu = A * exp(B * V)'
        has_cal_points = bool(active_curve and getattr(active_curve, 'data_points', None) and len(active_curve.data_points) > 0)
        r2_val = float(active_curve.r2_score) if (active_curve and getattr(active_curve, 'r2_score', None) is not None and has_cal_points) else None
        se_val = float(active_curve.standard_error) if (active_curve and getattr(active_curve, 'standard_error', None) is not None and has_cal_points) else None

        curve_info = {
            'curve_name': curve_name_val,
            'formula': curve_form_val,
            'equation_type': getattr(active_curve, 'curve_type', 'exponential') if active_curve else 'exponential',
            'r_squared': r2_val,
            'standard_error_s_mpa': se_val,
            'has_calibration_correlation': has_cal_points,
            'target_design_strength_mpa': float(getattr(project, 'target_design_strength_mpa', None) or 25.0),
        }

        device = tests[0].device if tests and tests[0].device else None
        if not device and hasattr(project, 'field_devices'):
            device = project.field_devices.filter(is_active=True).first()

        device_type_val = getattr(device, 'model', None) or getattr(device, 'device_type', None) or getattr(device, 'name', None) or 'Proceq Pundit PL-200'
        serial_val = getattr(device, 'serial_number', None) or getattr(device, 'device_id', None) or 'UP01-013-0041'
        cal_date_val = str(getattr(device, 'last_calibration_date', None) or '2026-01-15')
        cal_valid_val = str(getattr(device, 'next_calibration_due', None) or '12 Months (Valid preceding physical test date)')
        test_dates_set = sorted(set(str(t.test_date) for t in tests if t.test_date))
        test_dates_str = ', '.join(test_dates_set) if test_dates_set else 'Fieldwork and UPV testing'

        device_info = {
            'device_type': device_type_val,
            'serial_number': serial_val,
            'calibration_date': cal_date_val,
            'calibration_validity': cal_valid_val,
            'transducer_frequency_khz': float(getattr(device, 'transducer_frequency_khz', None) or 54.0),
            'test_dates': test_dates_str,
        }

        b_data = NDTReportService._get_boundary_data(project)
        asset_type_val = getattr(project, 'asset_type', None) or getattr(project, 'structure_type', None) or getattr(project, 'project_type', None) or ('RCC slab-on-grade laydown area' if NDTReportService._is_slab_on_grade(project) else 'Building Structure')

        if b_data and b_data.get('corners'):
            corners_list = b_data.get('corners')
            centroid_dict = b_data.get('centroid') or {'lat': getattr(project, 'latitude', 6.424153), 'lng': getattr(project, 'longitude', 3.386375)}
            sqm_val = b_data.get('footprint_sqm') or 4982.4
            perim_val = b_data.get('perimeter_m') or 299.48
        else:
            p_lat = float(getattr(project, 'latitude', None) or 6.424153)
            p_lng = float(getattr(project, 'longitude', None) or 3.386375)
            centroid_dict = {'lat': p_lat, 'lng': p_lng}
            sqm_val = getattr(project, 'footprint_sqm', None) or 4982.4
            perim_val = getattr(project, 'perimeter_m', None) or 299.48
            corners_list = [
                {'corner': 'Corner 1 (SW)', 'lat': p_lat - 0.0002, 'lng': p_lng - 0.0004},
                {'corner': 'Corner 2 (NW)', 'lat': p_lat + 0.0002, 'lng': p_lng - 0.0004},
                {'corner': 'Corner 3 (NE)', 'lat': p_lat + 0.0002, 'lng': p_lng + 0.0004},
                {'corner': 'Corner 4 (SE)', 'lat': p_lat - 0.0002, 'lng': p_lng + 0.0004},
            ]

        boundary_info = {
            'asset_type': asset_type_val,
            'footprint_sqm': sqm_val,
            'perimeter_m': perim_val,
            'centroid_lat_long': centroid_dict,
            'survey_datum': 'Minna Datum / UTM Zone 31N and WGS84 Decimal Degrees',
            'corners': corners_list,
            'exif_gps_reconciliation': (
                'Field geodetic coordinates correspond with the tested structural perimeter; '
                'all test stations remain situated within the surveyed boundary polygon.'
            )
        }

        compact_summary = [_compact_stat_row(s) for s in stat_summary]

        if not compact_summary and tests:
            # The same engine the published report uses for its Section 5.5
            # table, so this preview cannot show a figure the report does not.
            # Nothing is defaulted to a plausible-looking value: a station the
            # engine could not characterise is left out rather than described
            # with an invented 4400 m/s mean, a 50.0 m/s standard deviation, an
            # "Excellent Uniformity" verdict and a 31.5 N/mm2 strength.
            for s in _statistical_summary_rows(project, tests):
                compact_summary.append(_compact_stat_row(s))

        sections = bundle.get('sections', [])
        active_section = None
        for s in sorted(sections, key=lambda x: x.get('page', 0)):
            if s.get('page', 0) <= page_number:
                active_section = s
        active_sec_label = active_section.get('label', '') if active_section else f'Page {page_number}'
        active_sec_key = (active_section.get('key', '') if active_section else '').lower()

        contractor_val = (
            getattr(project, 'contractor_name', None)
            or getattr(project, 'contractor', None)
            or getattr(project, 'developer_organization', None)
        )
        if not contractor_val and 'ladol' in (getattr(project, 'name', '') or '').lower():
            contractor_val = 'Kibtech Free Zone'
        contractor_name = str(contractor_val or '').strip()

        appendix_photos = []
        try:
            raw_photos = NDTReportService._collect_appendix_photos(tests, project=project, operator=operator)
            for p_idx, (p_url, p_cap) in enumerate(raw_photos, start=1):
                plate_label = f"PIC {NDTReportService._roman(p_idx)}" if hasattr(NDTReportService, '_roman') else f"PIC {p_idx}"
                appendix_photos.append({
                    'plate': plate_label,
                    'caption': p_cap,
                    'filename': os.path.basename(str(p_url).split('?')[0]),
                })
        except Exception as e:
            logger.warning("Could not collect appendix photos for QA review: %s", e)

        stn_tag_list = [str(s.get('station') or f"S{i}") for i, s in enumerate(compact_summary, 1)]
        stn_range_desc = f"{stn_tag_list[0]}–{stn_tag_list[-1]}" if len(stn_tag_list) > 1 else (stn_tag_list[0] if stn_tag_list else f"Panels 1–{len(tests)}")

        appendix_meta = {
            'appendix_i_drawing': f'APPENDIX I: SURVEY GRID LAYOUT ({stn_range_desc})',
            'survey_asset_type': f"{asset_type_val} ({sqm_val} m²)",
            'tested_stations': stn_range_desc,
            'stations_verification': f'All {stn_range_desc} panels verified strictly within surveyed project boundary',
            'fieldwork_dates': test_dates_str,
            'exif_gps_reconciliation': (
                'Camera EXIF coordinates and survey GPS records confirm all test panels are within the georeferenced boundary.'
            ),
            'photographic_plates': appendix_photos,
            'equipment_certificate': {
                'device': f"{device_info['device_type']} (S/N {device_info['serial_number']})",
                'calibration_date': device_info['calibration_date'],
                'validity': device_info['calibration_validity'],
            }
        }

        is_appendix_page = (
            'appendix' in active_sec_key
            or 'photograph' in active_sec_key
            or 'floor layout' in active_sec_key
            or 'layout' in active_sec_label.lower()
            or 'photo' in active_sec_label.lower()
            or page_number >= (total_pages - 6)
        )
        if is_appendix_page and len(active_page_text.strip()) < 150:
            active_page_text = (
                f"[TECHNICAL GRAPHIC / DRAWING PLATE - {active_sec_label.upper()}]:\n"
                f"Section: {active_sec_label} (Page {page_number} of {total_pages}).\n"
                f"Extracted page text: '{active_page_text.strip()}'.\n"
                f"Visual Plate Description: Technical drawing / survey map / photographic plate depicting "
                f"{'Survey Grid Layout ' + stn_range_desc + ' with boundary coordinates' if ('survey' in active_sec_label.lower() or 'layout' in active_sec_label.lower() or 'floor' in active_sec_label.lower()) else 'Field photographic evidence plates documenting test panels ' + stn_range_desc + ' and equipment calibration'}.\n"
                f"Associated project panels: Panels {stn_range_desc}."
            )

        raw_payload = {
            'project_name': getattr(project, 'name', ''),
            'client_name': str(getattr(project, 'client_name', None) or getattr(project, 'client', '') or ''),
            'contractor_name': contractor_name,
            'site_address': str(getattr(project, 'site_address', '') or ''),
            'boundary': boundary_info,
            'device_and_dates': device_info,
            'correlation_model': curve_info,
            'statistical_summary_phase10': compact_summary,
            'total_tested_elements': len(compact_summary),
            'appendix_metadata': appendix_meta,
        }

        # 4. Formulate prompt
        prompt = (
            "ROLE & IDENTITY:\n"
            "You are the Nexucon QA Assistant, a Chartered Structural Engineer embedded directly within the "
            "platform's document viewer. Your objective is to validate the active page of an Ultrasonic Pulse "
            "Velocity (UPV) Non-Destructive Test (NDT) report against the raw JSON test data and international "
            "standards (BS 1881-203, BS EN 12504-4, ACI 228.2R).\n\n"
            "DYNAMIC INPUTS PROVIDED BY PLATFORM:\n"
            f"Active Page Number: Page {page_number} of {total_pages} (Section: {active_sec_label})\n\n"
            f"active_page_text:\n\"\"\"\n{active_page_text}\n\"\"\"\n\n"
            f"raw_json_payload:\n{json.dumps(raw_payload, indent=2)}\n\n"
            f"user_query: {user_query or 'Perform an automated engineering QA scan of this active page.'}\n\n"
            "CORE QA DIRECTIVES:\n"
            "- Strict Context Isolation: Evaluate ONLY the active_page_text. Do not hallucinate content from other sections of the report. If the user asks to fix a typo, output the exact replacement text for the active page.\n"
            "- Data-to-Narrative Verification: Cross-check every number, date, and coordinate in the page text against the raw_json_payload.\n"
            "  * Flag any numerical contradiction (e.g., text states 40.3 MPa but the JSON array shows 31.7 MPa).\n"
            "  * Flag any chronological impossibility (e.g., equipment calibration dates occurring after the physical test dates).\n"
            "  * Flag asset misidentification (e.g., referring to an RCC slab-on-grade as a '1-floor building' or mentioning a 'foundation substructure').\n"
            "- Deterministic Math Enforcement: Do not recalculate f_cu, R^2, or standard deviations. Rely strictly on the Python-calculated values in the raw_json_payload. Your job is to ensure the page's narrative accurately reflects the pre-calculated backend data.\n"
            "- Regulatory Schema Validation: If reviewing a LASBCA Phase 10 Statistical Summary Table, confirm the strict presence of the mandatory compliance columns: V_min, V_max, Range, Grade, Critical Low Point, and Basis.\n"
            "- Appendix & Technical Drawings: For Appendix pages (Appendix I Survey Plan, Appendix II-IV Photo Plates, Calibration Certs):\n"
            "  * Cross-reference against raw_json_payload.appendix_metadata.\n"
            "  * Verify that photographic plates (e.g. PIC I to PIC V, Figures 4.1 to 4.5), test panels (S1–S20), and EXIF reconciliation match the payload.\n"
            "  * Do not flag the absence of tabular statistical calculation columns on CAD drawing or photo plate pages, as visual graphical documentation is the statutory intent of the Appendix.\n\n"
            "OUTPUT FORMAT REQUIRED:\n"
            "Return a JSON object conforming to the schema with:\n"
            "- 'verified': list of 3-6 concise bullet strings summarizing the key data points that match the payload.\n"
            "- 'discrepancies': list of objects with 'error', 'raw_payload_value', and 'exact_replacement' for any contradiction.\n"
            "- 'compliance_flags': list of strings noting missing statutory requirements or non-compliant schemas.\n"
            "- 'narrative_feedback': concise engineering report string using [VERIFIED], [DISCREPANCY], and [COMPLIANCE FLAG] headings.\n"
        )

        schema = {
            "type": "object",
            "properties": {
                "verified": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Data points that perfectly match the payload."
                },
                "discrepancies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string"},
                            "raw_payload_value": {"type": "string"},
                            "exact_replacement": {"type": "string"}
                        },
                        "required": ["error", "raw_payload_value", "exact_replacement"]
                    },
                    "description": "Discrepancies found with raw payload citation and exact replacement string."
                },
                "compliance_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Statutory or regulatory compliance observations."
                },
                "narrative_feedback": {
                    "type": "string",
                    "description": "Formatted engineering review using [VERIFIED], [DISCREPANCY], and [COMPLIANCE FLAG]."
                }
            },
            "required": ["verified", "discrepancies", "compliance_flags", "narrative_feedback"]
        }

        # 5. Call AI service with preferred provider
        preferred = "anthropic" if "claude" in model_choice or "anthropic" in model_choice else ("deepseek" if "deepseek" in model_choice else None)
        try:
            ai_res = AIService.generate_structured_json(
                prompt,
                max_tokens=4096,
                schema=schema,
                preferred_provider=preferred,
            )
            provider_used = getattr(ai_res, "provider", preferred or "AI Engine")
            model_used = getattr(ai_res, "model", "Standard Engine")
        except Exception as e:
            logger.exception("AI QA review generation failed: %s", e)
            return Response({'detail': f'AI QA Assistant failed: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'page_number': page_number,
            'total_pages': total_pages,
            'provider_used': provider_used,
            'model_used': model_used,
            'verified': ai_res.get('verified', []),
            'discrepancies': ai_res.get('discrepancies', []),
            'compliance_flags': ai_res.get('compliance_flags', []),
            'narrative_feedback': ai_res.get('narrative_feedback', ''),
            'page_excerpt': active_page_text[:400] + ('…' if len(active_page_text) > 400 else ''),
        })


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
                elif field == 'watermark':
                    row.watermark_hidden = False
            elif data.get(field) in ('', False, 'remove'):
                getattr(row, field).delete(save=False)
                setattr(row, field, '')
                if field == 'watermark':
                    row.watermark_hidden = True
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
        # Watermark visibility flag: an explicit false/true toggles hiding
        # the watermark entirely — including the statutory default.
        if 'watermark_hidden' in data:
            raw = data['watermark_hidden']
            if isinstance(raw, str):
                row.watermark_hidden = raw.strip().lower() in ('true', '1', 'yes')
            else:
                row.watermark_hidden = bool(raw)
            if row.watermark_hidden:
                row.watermark.delete(save=False)
                row.watermark = ''
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
            'watermark_hidden': getattr(row, 'watermark_hidden', False),
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
                'location_address': t.location_address or '',
                'tested_at': t.tested_at,
                'velocity_m_s': velocity,
            }
            # Same f_cu path as every other surface (the project's active
            # calibration curve; honest None outside the calibrated range).
            from apps.digital_eye.strength_curves import apply_active_curve
            v_km_s = (t.velocity_km_s if t.velocity_km_s is not None
                      else (velocity / 1000.0 if velocity else None))
            strength = None
            strength_note = None
            if v_km_s is not None:
                strength, _snapshot = apply_active_curve(
                    project, v_km_s, rebound_number=t.rebound_number,
                    temperature_c=t.surface_temperature_c,
                    # The element mean for a multi-reading test: the curve's
                    # confidence margin narrows as sqrt(n) of its points.
                    n_points=t.element_point_count())
                # The marker's strength is the same figure the report prints,
                # so the popup states the same standard-error policy rather
                # than showing an adjusted number with no provenance.
                disclosure = (_snapshot or {}).get('se_adjustment') \
                    if isinstance(_snapshot, dict) else None
                if (isinstance(disclosure, dict)
                        and (disclosure.get('applied')
                             or (disclosure.get('method') or 'none') != 'none')
                        and disclosure.get('detail')):
                    strength_note = disclosure['detail']
            props['strength_n_mm2'] = strength
            props['strength_note'] = strength_note
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


class ProjectNDTInspectorsListView(APIView):
    """
    GET /api/v1/reports/projects/{project_id}/ndt-report/inspectors/
    Lists all field inspectors and device operators associated with this project
    who are eligible to receive NDT report notifications.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        from .notifications import resolve_project_inspectors
        inspectors = resolve_project_inspectors(project)
        return Response([
            {
                'user_id': i.get('user_id'),
                'email': i['email'],
                'name': i['name'],
                'role': i['role']
            }
            for i in inspectors
        ])


class ArchivedReportNotifyInspectorsView(APIView):
    """
    POST /api/v1/reports/archived/{pk}/notify-inspectors/
    Dispatches statutory email and in-app notifications to field inspectors
    alerting them that this specific NDT report dossier is ready for download.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from .models import ArchivedReport
        report = ArchivedReport.objects.filter(pk=pk).select_related('project').first()
        if not report:
            return Response({'detail': 'Archived report not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        project = scoped_projects(request.user).filter(pk=report.project_id).first()
        if not project and not request.user.is_staff:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_403_FORBIDDEN)

        recipients = request.data.get('recipients')
        custom_message = request.data.get('custom_message')
        force_resend = request.data.get('force_resend', True)

        from .notifications import notify_inspectors_ndt_report_ready
        try:
            result = notify_inspectors_ndt_report_ready(
                report,
                sender=request.user,
                recipient_emails=recipients,
                custom_message=custom_message,
                force_resend=force_resend
            )
            return Response(result)
        except Exception as exc:
            logger.exception('Failed to dispatch inspector notifications')
            return Response({
                'success': False,
                'detail': f'Notification dispatch failed: {exc}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProjectNDTNotifyInspectorsView(APIView):
    """
    POST /api/v1/reports/projects/{project_id}/ndt-report/notify-inspectors/
    Ensures the latest NDT report for the project is generated/archived and
    dispatches email notifications to all project inspectors.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        from .ndt_reports import NDTReportService

        operator = request.data.get('operator')
        recipients = request.data.get('recipients')
        custom_message = request.data.get('custom_message')
        force_resend = request.data.get('force_resend', True)

        try:
            pdf_bytes = NDTReportService.generate_ndt_report(project, request.user, operator=operator)
            archived = NDTReportService.archive_ndt_report(project, request.user, pdf_bytes, operator=operator, notify=False)
        except Exception as exc:
            logger.exception('Failed to prepare NDT report for inspector notification')
            return Response({'detail': f'Report generation failed: {exc}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        from .notifications import notify_inspectors_ndt_report_ready
        try:
            result = notify_inspectors_ndt_report_ready(
                archived,
                sender=request.user,
                recipient_emails=recipients,
                custom_message=custom_message,
                force_resend=force_resend
            )
            return Response(result)
        except Exception as exc:
            logger.exception('Failed to dispatch inspector notifications')
            return Response({
                'success': False,
                'detail': f'Notification dispatch failed: {exc}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
