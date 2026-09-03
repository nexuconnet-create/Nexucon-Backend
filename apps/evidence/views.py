"""
Evidence Intelligence API views.

All list endpoints are scoped through common.permissions.scoped_projects()
(HQ -> District -> Project isolation). AI findings are decision-support only;
every mutation is a human action recorded in the audit ledger.
"""
import logging

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import (
    IsDirector, scoped_projects, user_is_state_hq, user_district,
)
from .correlation import CorrelationEngine
from .intelligence import HQIntelligenceService, ProjectIntelligenceService
from .models import AIAnalysisRecord, CorrelationFinding, EvidenceRecord
from .review import HumanReviewService, ReviewError, record_audit
from .serializers import (
    AIAnalysisRecordSerializer, CorrelationFindingSerializer, EvidenceRecordSerializer,
)

logger = logging.getLogger(__name__)


class ScopedEvidenceMixin:
    """Filters querysets through the requesting user's project scope."""

    def get_queryset(self):
        qs = super().get_queryset()
        allowed = scoped_projects(self.request.user)
        return qs.filter(project__in=allowed)


class EvidenceRecordViewSet(ScopedEvidenceMixin, viewsets.ReadOnlyModelViewSet):
    """
    Centralized Evidence Registry — read endpoint. Records are created by the
    ingestion pipeline, never hand-posted.
    """
    serializer_class = EvidenceRecordSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'source_type', 'structural_element_id', 'bim_guid']
    search_fields = ['evidence_reference', 'structural_element_id', 'bim_guid']
    ordering_fields = ['created_at', 'captured_at']

    def get_queryset(self):
        qs = EvidenceRecord.objects.select_related('project').all()
        allowed = scoped_projects(self.request.user)
        return qs.filter(project__in=allowed)


class AIAnalysisRecordViewSet(ScopedEvidenceMixin, viewsets.ReadOnlyModelViewSet):
    """Persistent AI Analysis Records (risk_level, observations, correlations,
    recommendations, requires_human_review)."""
    serializer_class = AIAnalysisRecordSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'analysis_type', 'risk_level', 'requires_human_review']

    def get_queryset(self):
        qs = AIAnalysisRecord.objects.select_related('project').prefetch_related('evidence')
        allowed = scoped_projects(self.request.user)
        return qs.filter(project__in=allowed)


import uuid

class CorrelationFindingViewSet(ScopedEvidenceMixin, viewsets.ModelViewSet):
    """
    Cross-source correlation findings + Human-in-the-Loop review actions
    (plan §5 Week 5):
      POST /findings/                        create manual technical finding
      POST /findings/{id}/review/            accept | reject | modify
      POST /findings/{id}/supplementary/     request supplementary evidence / live stream
      POST /findings/{id}/trigger-inspection/
      POST /findings/{id}/issue-ncr/
      POST /findings/{id}/director-signoff/
    """
    serializer_class = CorrelationFindingSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['project', 'status', 'risk_level', 'structural_element_id']
    search_fields = ['finding_reference', 'title', 'structural_element_id']

    def get_queryset(self):
        qs = CorrelationFinding.objects.select_related(
            'project', 'reviewed_by', 'linked_ncr', 'linked_inspection', 'analysis',
        ).prefetch_related('evidence', 'revisions')
        allowed = scoped_projects(self.request.user)
        return qs.filter(project__in=allowed)

    def create(self, request, *args, **kwargs):
        project_id = request.data.get('project')
        if not project_id:
            return Response({'detail': 'Project is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Allow lookup by UUID or project name
        project = None
        try:
            project = scoped_projects(request.user).filter(pk=uuid.UUID(str(project_id))).first()
        except (ValueError, TypeError):
            project = scoped_projects(request.user).filter(name__icontains=str(project_id)).first()

        if not project:
            project = scoped_projects(request.user).first()

        if not project:
            return Response({'detail': 'Project not found in your scope.'}, status=status.HTTP_404_NOT_FOUND)

        title = request.data.get('title', '').strip()
        description = request.data.get('description', '').strip()
        if not title:
            return Response({'detail': 'Title is required.'}, status=status.HTTP_400_BAD_REQUEST)

        severity = str(request.data.get('severity') or 'HIGH').lower()
        risk_level_map = {'critical': 'critical', 'high': 'high', 'medium': 'medium', 'low': 'low'}
        risk_level = risk_level_map.get(severity, 'high')

        severity_scores = {'critical': 0.95, 'high': 0.78, 'medium': 0.50, 'low': 0.25}
        risk_score = severity_scores.get(risk_level, 0.78)

        structural_element_id = request.data.get('structural_element_name') or request.data.get('structural_element_id') or ''
        bim_guid = request.data.get('structural_element_guid') or ''
        taxonomy = request.data.get('taxonomy') or 'REBAR_SPACING_DEFICIENCY'
        depth_mm = request.data.get('depth_mm')
        deviation_mm = request.data.get('deviation_mm')

        extra_details = []
        if depth_mm is not None and depth_mm != '':
            extra_details.append(f"Depth: {depth_mm} mm")
        if deviation_mm is not None and deviation_mm != '':
            extra_details.append(f"Variance: {deviation_mm} mm")
        if taxonomy:
            extra_details.append(f"Taxonomy: {taxonomy}")

        full_desc = description
        if extra_details:
            full_desc = f"{description}\n\nTechnical Parameters: {', '.join(extra_details)}"

        user_label = request.user.get_full_name() or request.user.email
        reasoning = (
            f"Technical finding logged on-site by {user_label} for structural element "
            f"'{structural_element_id or 'General Structure'}'. Severity assessed as {risk_level.upper()} "
            f"(risk score {risk_score:.2f})."
        )

        # Ingest as EvidenceRecord
        evidence = EvidenceRecord.objects.create(
            project=project,
            source_type='other',
            source_model='digital_eye.ManualFinding',
            source_id=str(uuid.uuid4()),
            structural_element_id=structural_element_id,
            bim_guid=bim_guid,
            confidence=risk_score,
            payload={
                'title': title,
                'description': description,
                'taxonomy': taxonomy,
                'severity': risk_level,
                'depth_mm': depth_mm,
                'deviation_mm': deviation_mm,
                'logged_by': user_label,
            },
            ingested_by=request.user,
        )
        evidence.evidence_hash = evidence.compute_hash()
        evidence.save(update_fields=['evidence_hash', 'updated_at'])

        analysis = AIAnalysisRecord.objects.create(
            project=project,
            analysis_type='correlation',
            risk_level=risk_level,
            risk_score=risk_score,
            observations=[title, description],
            correlations=[{'evidence_reference': evidence.evidence_reference, 'source_type': 'manual_finding'}],
            recommendations=[{
                'recommendation': f"Conduct structural evaluation for {title} on element {structural_element_id or 'structure'}.",
                'priority': 'Urgent' if risk_level in ('critical', 'high') else 'Routine',
            }],
            reasoning_log=reasoning,
            requires_human_review=True,
            model_provider='engineer',
            model_version='manual-inspection-v1',
        )
        analysis.evidence.set([evidence])

        finding = CorrelationFinding.objects.create(
            project=project,
            structural_element_id=structural_element_id,
            bim_guid=bim_guid,
            group_key=taxonomy or f"element:{structural_element_id}",
            title=title,
            description=full_desc,
            risk_level=risk_level,
            risk_score=risk_score,
            reasoning=reasoning,
            analysis=analysis,
            status='pending_review',
        )
        finding.evidence.set([evidence])

        CorrelationEngine._add_revision(
            finding, 'created', changed_by=request.user,
            notes=f"Technical finding logged by {user_label}.",
        )

        serializer = self.get_serializer(finding)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def review(self, request, pk=None):
        """Accept / Reject / Modify an AI finding (human decision)."""
        finding = self.get_object()
        decision = request.data.get('decision')
        notes = request.data.get('notes', '')
        modifications = request.data.get('modifications')
        try:
            finding, digest = HumanReviewService.review(
                finding, request.user, decision, notes=notes, modifications=modifications,
            )
        except ReviewError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            CorrelationFindingSerializer(finding, context=self.get_serializer_context()).data
            | {'decision_hash': digest},
        )

    @action(detail=True, methods=['post'])
    def supplementary(self, request, pk=None):
        """Request supplementary evidence or a live stream for a finding."""
        finding = self.get_object()
        request_type = request.data.get('request_type', 'supplementary')
        notes = request.data.get('notes', '')
        try:
            finding, revision = HumanReviewService.request_supplementary_evidence(
                finding, request.user, request_type=request_type, notes=notes,
            )
        except ReviewError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'requested', 'finding': finding.finding_reference,
                         'revision': revision.revision_number})

    @action(detail=True, methods=['post'], url_path='trigger-inspection')
    def trigger_inspection(self, request, pk=None):
        """Trigger an immediate statutory inspection from a finding."""
        finding = self.get_object()
        try:
            inspection = HumanReviewService.trigger_inspection(
                finding, request.user,
                inspection_type=request.data.get('inspection_type', 'Structural Review'),
                priority=request.data.get('priority', 'High'),
                notes=request.data.get('notes', ''),
            )
        except ReviewError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {'inspection_id': str(inspection.id),
             'inspection_reference': inspection.inspection_reference,
             'status': inspection.status},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='trigger_inspection', url_name='trigger-inspection-legacy')
    def trigger_inspection_legacy(self, request, pk=None):
        return self.trigger_inspection(request, pk=pk)

    @action(detail=True, methods=['post'], url_path='issue-ncr')
    def issue_ncr(self, request, pk=None):
        """Issue a formal NCR (with optional corrective action) from a finding."""
        finding = self.get_object()
        from datetime import datetime as _dt
        due = request.data.get('corrective_due_date')
        due_date = None
        if due:
            try:
                due_date = _dt.fromisoformat(str(due)).date()
            except ValueError:
                return Response({'detail': 'corrective_due_date must be ISO date (YYYY-MM-DD).'},
                                status=status.HTTP_400_BAD_REQUEST)
        try:
            ncr, capa = HumanReviewService.issue_ncr(
                finding, request.user,
                title=request.data.get('title'),
                description=request.data.get('description'),
                severity=request.data.get('severity'),
                category=request.data.get('category', 'Structural'),
                corrective_action=request.data.get('corrective_action', ''),
                corrective_due_date=due_date,
            )
        except ReviewError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        data = {'ncr_id': str(ncr.id), 'ncr_reference': ncr.ncr_reference,
                'severity': ncr.severity, 'status': ncr.status}
        if capa:
            data['corrective_action'] = {
                'capa_id': str(capa.id), 'capa_reference': capa.capa_reference,
                'status': capa.status, 'due_date': capa.due_date,
            }
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='issue_ncr', url_name='issue-ncr-legacy')
    def issue_ncr_legacy(self, request, pk=None):
        return self.issue_ncr(request, pk=pk)

    @action(detail=True, methods=['post'], url_path='director-signoff', permission_classes=[IsAuthenticated, IsDirector])
    def director_signoff(self, request, pk=None):
        """Formal Director review & sign-off into the official record."""
        finding = self.get_object()
        try:
            revision, digest = HumanReviewService.director_signoff(
                finding, request.user,
                decision=request.data.get('decision', 'approve'),
                notes=request.data.get('notes', ''),
            )
        except ReviewError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'revision': revision.revision_number, 'signoff_hash': digest,
                          'signed_at': revision.recorded_at.isoformat()})

    @action(detail=True, methods=['post'], url_path='director_signoff', permission_classes=[IsAuthenticated, IsDirector], url_name='director-signoff-legacy')
    def director_signoff_legacy(self, request, pk=None):
        return self.director_signoff(request, pk=pk)


class ProjectIntelligenceView(APIView):
    """
    Project-Level AI Intelligence (plan §5 Week 6): Overall / Structural /
    Compliance risk scores, observations and recommended actions.
    GET /api/v1/evidence/intelligence/projects/{project_id}/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from apps.projects.models import Project
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        summary = ProjectIntelligenceService.aggregate(project)
        return Response(summary)


class ProjectCorrelationView(APIView):
    """
    Run (or re-run) the Cross-Source Correlation Engine for a project
    (plan §5 Week 4). Triggers re-ingestion first so the registry is current.
    POST /api/v1/evidence/intelligence/projects/{project_id}/correlate/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        from apps.projects.models import Project
        project = scoped_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        findings = CorrelationEngine.run(project, ingested_by=request.user)
        record_audit(
            request.user, 'evidence.correlation.run', 'Project', project.id,
            metadata={'findings': len(findings)},
        )
        return Response({
            'project_id': str(project.id),
            'findings_created_or_updated': len(findings),
            'findings': CorrelationFindingSerializer(
                findings, many=True, context={'request': request},
            ).data,
        })


class HQOverviewView(APIView):
    """
    State HQ Cross-District AI Intelligence (plan §5 Week 9).
    GET /api/v1/evidence/hq/overview/  — state-wide (HQ role required)
    GET /api/v1/evidence/hq/overview/?district={id}
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not user_is_state_hq(request.user):
            # District staff see only their own district's overview.
            district = user_district(request.user)
            if district is None:
                return Response(
                    {'detail': 'State HQ role or district assignment required.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(HQIntelligenceService.overview(district=district))
        from apps.government.models import District
        district = None
        district_id = request.query_params.get('district')
        if district_id:
            district = District.objects.filter(pk=district_id).first()
            if not district:
                return Response({'detail': 'District not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(HQIntelligenceService.overview(district=district))


class HQDistrictMatrixView(APIView):
    """Automated District Risk Heatmap queries (plan §5 Week 9). HQ only."""
    permission_classes = [IsAuthenticated, IsDirector]

    def get(self, request):
        return Response(HQIntelligenceService.district_matrix())


class HQExecutiveBriefingView(APIView):
    """AI-Assisted Executive Briefing generator (plan §5 Week 9). HQ only."""
    permission_classes = [IsAuthenticated, IsDirector]

    def get(self, request):
        from apps.government.models import District
        district = None
        district_id = request.query_params.get('district')
        if district_id:
            district = District.objects.filter(pk=district_id).first()
        return Response(HQIntelligenceService.executive_briefing(district=district))


class HQInspectorAnalyticsView(APIView):
    """Inspector performance & turnaround analytics (plan §5 Week 9). HQ only."""
    permission_classes = [IsAuthenticated, IsDirector]

    def get(self, request):
        return Response(HQIntelligenceService.inspector_analytics())
