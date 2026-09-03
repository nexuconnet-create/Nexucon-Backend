"""
Client Portal API & Public Transparency Gateway
(implementation plan §5 Week 8 + Workstream D).

Client-scoped endpoints (strict data segregation):
  Every client endpoint resolves the signed-in user's Developer organization
  and filters strictly to that organization's projects. Clients see only
  completed inspection outcomes, their own NCR/corrective-action feed and
  APPROVED documents.

Public Transparency Gateway (unauthenticated, deliberately minimal):
  Only projects that have passed regulatory review (APPROVED / under
  construction / completed) are published, with a whitelist of fields —
  never internal notes, AI internals, staff assignments or raw evidence.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.compliance.models import CorrectiveActionPlan, NonConformanceReport
from apps.documents.models import Document
from apps.inspections.models import Inspection
from apps.projects.models import Project, ProjectMilestone
from apps.stakeholders.models import Developer, StakeholderMessage

# Statuses that may be exposed on the public gateway.
PUBLIC_PROJECT_STATUSES = ('APPROVED', 'ACTIVE', 'COMPLETED')

# Fields published for a project on the transparency gateway.
PUBLIC_PROJECT_FIELDS = (
    'id', 'reference_number', 'name', 'project_type', 'status',
    'developer_organization', 'site_address', 'lga', 'state',
    'start_date', 'estimated_completion', 'permit_number', 'permit_status',
    'number_of_floors', 'site_area', 'gross_floor_area',
)


def client_projects(user):
    """Projects belonging to the signed-in client's developer organization."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()
    developer = Developer.objects.filter(user=user).first()
    if not developer:
        return Project.objects.none()
    return (Project.objects.filter(developer_organization=developer.name)
            | Project.objects.filter(developer_name=developer.name)).distinct()


class ClientPortalMixin:
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not client_projects(request.user).exists():
            self.permission_denied(
                request, message='Client portal access requires a linked developer organization.')


# ==========================================================================
# Client-scoped endpoints (plan §5 Week 8)
# ==========================================================================

class ClientProjectsView(ClientPortalMixin, APIView):
    """GET /api/v1/public-portal/client/projects/ — the client's projects only."""

    def get(self, request):
        projects = []
        for project in client_projects(request.user).order_by('-created_at'):
            projects.append({
                'id': str(project.id),
                'reference_number': project.reference_number,
                'name': project.name,
                'status': project.status,
                'project_type': project.project_type,
                'site_address': project.site_address,
                'start_date': project.start_date,
                'estimated_completion': project.estimated_completion,
                'milestones_total': project.milestones.count(),
                'milestones_completed': project.milestones.filter(is_completed=True).count(),
            })
        return Response({'projects': projects})


class ClientMilestonesView(ClientPortalMixin, APIView):
    """GET /api/v1/public-portal/client/projects/{project_id}/milestones/"""

    def get(self, request, project_id):
        project = client_projects(request.user).filter(pk=project_id).first()
        if not project:
            return Response({'detail': 'Project not found in your client scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        milestones = [{
            'id': str(m.id),
            'title': m.title,
            'target_date': m.target_date,
            'is_completed': m.is_completed,
            'completion_date': m.completion_date,
        } for m in project.milestones.order_by('target_date')]
        return Response({'project': project.reference_number, 'milestones': milestones})


class ClientInspectionsView(ClientPortalMixin, APIView):
    """
    GET /api/v1/public-portal/client/inspections/
    Filtered inspection outcomes: only COMPLETED inspections, outcome level —
    no internal scheduling, staff or evidence detail.
    """

    def get(self, request):
        inspections = (Inspection.objects
                       .filter(project__in=client_projects(request.user),
                               status='COMPLETED')
                       .select_related('project')
                       .order_by('-completed_date'))
        data = [{
            'id': str(i.id),
            'inspection_reference': i.inspection_reference,
            'project': i.project.name,
            'inspection_type': i.inspection_type,
            'outcome': i.outcome,
            'completed_date': i.completed_date,
        } for i in inspections]
        return Response({'inspections': data})


class ClientNCRFeedView(ClientPortalMixin, APIView):
    """GET /api/v1/public-portal/client/ncrs/ — NCR / corrective-action feed."""

    def get(self, request):
        ncrs = (NonConformanceReport.objects
                .filter(project__in=client_projects(request.user))
                .select_related('project')
                .order_by('-date_logged'))
        data = []
        for ncr in ncrs:
            capas = [{
                'capa_reference': c.capa_reference,
                'title': c.title,
                'status': c.status,
                'due_date': c.due_date,
            } for c in ncr.capas.all()]
            data.append({
                'id': str(ncr.id),
                'ncr_reference': ncr.ncr_reference,
                'project': ncr.project.name,
                'title': ncr.title,
                'severity': ncr.severity,
                'status': ncr.status,
                'date_logged': ncr.date_logged,
                'corrective_actions': capas,
            })
        return Response({'ncrs': data})


class ClientDocumentsView(ClientPortalMixin, APIView):
    """GET /api/v1/public-portal/client/documents/ — APPROVED documents only."""

    def get(self, request):
        documents = (Document.objects
                     .filter(project__in=client_projects(request.user),
                             status='APPROVED')
                     .select_related('project')
                     .order_by('-updated_at'))
        data = [{
            'id': str(d.id),
            'document_reference': d.document_reference,
            'project': d.project.name,
            'title': d.title,
            'document_type': d.document_type,
            'status': d.status,
            'current_version': d.current_version,
            'is_digitally_stamped': d.is_digitally_stamped,
            'updated_at': d.updated_at,
        } for d in documents]
        return Response({'documents': data})


class ClientDocumentDownloadView(ClientPortalMixin, APIView):
    """
    GET /api/v1/public-portal/client/documents/{document_id}/download/
    Approved-document download gateway: only APPROVED documents of the
    client's own projects can be fetched.
    """

    def get(self, request, document_id):
        document = (Document.objects
                    .filter(pk=document_id,
                            project__in=client_projects(request.user),
                            status='APPROVED')
                    .select_related('project')
                    .first())
        if not document:
            return Response(
                {'detail': 'Document not found or not approved for release.'},
                status=status.HTTP_404_NOT_FOUND)
        if not document.file_url:
            return Response({'detail': 'No file is attached to this document record.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            'document': document.document_reference,
            'title': document.title,
            'version': document.current_version,
            'url': document.file_url,
            'digitally_stamped': document.is_digitally_stamped,
            'stamp_reference': document.stamp_reference,
        })


class ClientMessagesView(ClientPortalMixin, APIView):
    """
    Real-time client messaging (plan §5 W8) over the existing
    StakeholderMessage channel store, scoped to the client's projects.
    GET  /client/messages/?project={id}
    POST /client/messages/  {project, message_text, is_urgent?}
    """

    def get(self, request):
        projects = client_projects(request.user)
        project_id = request.query_params.get('project')
        if project_id:
            projects = projects.filter(pk=project_id)
            if not projects.exists():
                return Response({'detail': 'Project not found in your client scope.'},
                                status=status.HTTP_404_NOT_FOUND)
        messages = (StakeholderMessage.objects
                    .filter(project_name__in=[p.name for p in projects])
                    .order_by('created_at')[:500])
        data = [{
            'id': str(m.id),
            'channel_name': m.channel_name,
            'project_name': m.project_name,
            'sender_name': m.sender_name,
            'sender_role': m.sender_role,
            'message_text': m.message_text,
            'is_urgent': m.is_urgent,
            'created_at': m.created_at,
        } for m in messages]
        return Response({'messages': data})

    def post(self, request):
        project = client_projects(request.user).filter(
            pk=request.data.get('project')).first()
        if not project:
            return Response({'detail': 'A valid project in your client scope is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        text = (request.data.get('message_text') or '').strip()
        if not text:
            return Response({'detail': 'message_text is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        developer = Developer.objects.filter(user=request.user).first()
        message = StakeholderMessage.objects.create(
            sender=request.user,
            sender_name=(request.user.get_full_name() or request.user.email),
            sender_role=f"Client — {developer.name if developer else 'Developer'}",
            channel_name=f"project::{project.reference_number}",
            project_name=project.name,
            message_text=text[:10000],
            is_urgent=bool(request.data.get('is_urgent', False)),
        )
        return Response({
            'id': str(message.id),
            'channel_name': message.channel_name,
            'created_at': message.created_at,
        }, status=status.HTTP_201_CREATED)


# ==========================================================================
# Public Transparency Gateway (Workstream D) — approved data only
# ==========================================================================

class PublicProjectsView(APIView):
    """GET /api/v1/public-portal/transparency/projects/ — published projects."""

    permission_classes = [AllowAny]

    def get(self, request):
        projects = (Project.objects.filter(status__in=PUBLIC_PROJECT_STATUSES)
                    .order_by('name'))
        data = []
        for project in projects:
            row = {'id': str(project.id)}
            for field in PUBLIC_PROJECT_FIELDS:
                if field == 'id':
                    continue
                row[field] = getattr(project, field, None)
            data.append(row)
        return Response({'projects': data, 'count': len(data)})


class PublicProjectDetailView(APIView):
    """
    GET /api/v1/public-portal/transparency/projects/{project_id}/
    Public summary: milestones and completed inspection outcomes only.
    """

    permission_classes = [AllowAny]

    def get(self, request, project_id):
        project = get_object_or_404(
            Project.objects.filter(status__in=PUBLIC_PROJECT_STATUSES), pk=project_id)
        detail = {field: getattr(project, field, None)
                  for field in PUBLIC_PROJECT_FIELDS if field != 'id'}
        detail['milestones'] = [{
            'title': m.title,
            'target_date': m.target_date,
            'is_completed': m.is_completed,
        } for m in project.milestones.order_by('target_date')]
        detail['inspection_outcomes'] = [{
            'inspection_reference': i.inspection_reference,
            'inspection_type': i.inspection_type,
            'outcome': i.outcome,
            'completed_date': i.completed_date,
        } for i in project.inspections.filter(status='COMPLETED')
                                       .order_by('-completed_date')]
        return Response(detail)


class PublicViolationReportView(APIView):
    """
    POST /api/v1/public-portal/transparency/violation-reports/
    Citizen reporting of suspected violations (Workstream D).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        from .models import ViolationReport
        address = (request.data.get('address') or '').strip()
        description = (request.data.get('description') or '').strip()
        if not address or not description:
            return Response({'detail': 'address and description are required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        report = ViolationReport.objects.create(
            reporter_name=(request.data.get('reporter_name') or '').strip()[:150] or None,
            reporter_contact=(request.data.get('reporter_contact') or '').strip()[:150] or None,
            address=address[:255],
            description=description,
            evidence_url=request.data.get('evidence_url') or None,
        )
        return Response({
            'id': str(report.id),
            'status': report.status,
            'reported_at': report.reported_at,
        }, status=status.HTTP_201_CREATED)
