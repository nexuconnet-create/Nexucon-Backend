from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from django.db.models import Q
from .models import Project, ProjectMilestone, ProjectDocument
from .serializers import ProjectSerializer, ProjectMilestoneSerializer, ProjectDocumentSerializer
from apps.applications.models import Application


class ProjectMilestoneViewSet(viewsets.ModelViewSet):
    queryset = ProjectMilestone.objects.all().order_by('target_date')
    serializer_class = ProjectMilestoneSerializer


from .models import BIMModel
from .serializers import BIMModelSerializer
from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiResponse
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from django.views.decorators.cache import cache_page

from rest_framework.permissions import IsAuthenticated

@method_decorator(ratelimit(key='ip', rate='60/m', block=True), name='dispatch')
@method_decorator(cache_page(60 * 15), name='list')
@method_decorator(cache_page(60 * 15), name='retrieve')
class ProjectViewSet(viewsets.ModelViewSet):
    """CRUD API for Project model"""
    queryset = Project.objects.prefetch_related('scans', 'bim_models').all().order_by('-created_at')
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    @extend_schema(responses={200: ProjectSerializer(many=True)})
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = Project.objects.prefetch_related('scans', 'bim_models').all().order_by('-created_at')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        # Cold storage (8 Sep meeting): projects inactive for months drop
        # out of the default browse LIST; every record stays reachable
        # directly — retrieve/update and the detail actions fetch by pk and
        # must never 404 on a cold-stored project.
        if self.action == 'list':
            storage_param = self.request.query_params.get('storage')
            if storage_param == 'cold':
                queryset = queryset.filter(cold_storage=True)
            elif (self.request.query_params.get('include_cold', '').lower()
                    not in ('true', '1')):
                queryset = queryset.filter(cold_storage=False)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(name__icontains=search_param) |
                Q(reference_number__icontains=search_param) |
                Q(site_location__icontains=search_param) |
                Q(developer_name__icontains=search_param)
            )

        # Constrain mock seed projects: Only SiteIQ@nexucon.net and official inspectors
        # have visibility into the initial regulatory mock projects during the testing period.
        user = getattr(self.request, 'user', None)
        if user and user.is_authenticated:
            user_email = (user.email or '').strip().lower()
            is_siteiq = (user_email == 'siteiq@nexucon.net')
            has_gov = hasattr(user, 'government_profile') and user.government_profile
            is_inspector = bool(has_gov and user.government_profile.role and 'inspector' in user.government_profile.role.name.lower())

            if not is_siteiq and not is_inspector and not user.is_superuser:
                mock_names = [
                    'Eko Atlantic Marina Towers',
                    'Victoria Island Financial Center',
                    'Lekki Free Trade Zone Warehouse Complex',
                    'Ikoyi Imperial Heights Luxury Condominiums'
                ]
                queryset = queryset.filter(
                    Q(developer_email__iexact=user.email) |
                    Q(client_contact__icontains=user.email)
                ).exclude(name__in=mock_names)

        return queryset

    @action(detail=False, methods=['get'], url_path='assignable', permission_classes=[IsAuthenticated])
    def assignable(self, request):
        """
        Returns active projects available for assignment to inspections, findings, or field evidence,
        strictly scoped to the authenticated user's permissions, assigned projects, or district jurisdiction.
        """
        from common.permissions import scoped_projects
        user = request.user

        # 1. Scope projects strictly based on organizational role, district, and assignments
        qs = scoped_projects(user)

        # 2. For external non-government users (contractors, developers, client contacts),
        # combine with projects where they are listed as developer or contact
        if not user.is_superuser and not (hasattr(user, 'government_profile') and user.government_profile):
            qs = (qs | self.get_queryset()).distinct()

        # 3. Only ACTIVE or APPROVED projects that are currently under construction
        # Exclude cold storage, draft, planning, suspended, completed, or abandoned projects.
        projects = qs.filter(
            cold_storage=False,
            status__in=['ACTIVE', 'APPROVED']
        ).order_by('name')

        data = [
            {
                'id': str(p.id),
                'name': p.name,
                'reference_number': p.reference_number,
                'permit_number': p.permit_number,
                'status': p.status,
                'lga': p.lga,
                'state': p.state,
                'site_address': p.site_address,
                'developer_organization': p.developer_organization,
                'latitude': float(p.latitude) if p.latitude is not None else None,
                'longitude': float(p.longitude) if p.longitude is not None else None,
            }
            for p in projects
        ]
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='inspectors', permission_classes=[IsAuthenticated])
    def inspectors(self, request, pk=None):
        """
        Returns all inspectors assigned or invited to this project,
        enabling direct inspector-to-inspector communication.
        """
        try:
            project = Project.objects.filter(pk=pk).first()
        except Exception:
            project = None

        if not project:
            return Response({"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND)

        from django.contrib.auth import get_user_model
        from apps.inspections.models import Inspection
        from apps.settings.models import UserInvitation
        from apps.stakeholders.models import Inspector as StakeholderInspector

        User = get_user_model()
        results = []
        seen_emails = set()

        # 1. Inspectors with assigned inspections on this project
        inspections = Inspection.objects.filter(project=project).select_related('inspector')
        for insp in inspections:
            u = insp.inspector
            if u and u.email and u.email.lower() not in seen_emails:
                seen_emails.add(u.email.lower())
                name = f"{u.first_name} {u.last_name}".strip() or insp.inspector_name or u.email.split('@')[0]
                badge = f"LAG-INS-{str(u.id).replace('-', '')[:4].upper()}"
                role = "Accredited Field Inspector"
                agency = "LASBCA"
                
                st_ins = StakeholderInspector.objects.filter(user=u).first()
                if st_ins:
                    badge = st_ins.inspector_id or badge
                    role = st_ins.role_title or role
                
                gov_prof = getattr(u, 'government_profile', None)
                if gov_prof:
                    if gov_prof.role:
                        role = gov_prof.role.name
                    if gov_prof.agency:
                        agency = gov_prof.agency.name

                results.append({
                    "id": str(u.id),
                    "name": name,
                    "email": u.email,
                    "badge_number": badge,
                    "role": role,
                    "agency": agency,
                    "status": "Assigned to Site",
                    "is_current_user": (request.user.id == u.id)
                })

        # 2. Inspectors with invitations referencing this project
        proj_identifiers = [str(project.id), project.reference_number, project.name]
        invitations = UserInvitation.objects.filter(
            Q(role__icontains='inspector') | Q(department__icontains='inspection')
        )
        for inv in invitations:
            assigned = inv.assigned_projects or []
            matches = any(str(p_id) in assigned for p_id in proj_identifiers)
            if matches or (inv.district and project.district and inv.district_id == project.district_id):
                if inv.email.lower() not in seen_emails:
                    seen_emails.add(inv.email.lower())
                    results.append({
                        "id": f"inv-{inv.id}",
                        "name": inv.name or inv.email.split('@')[0],
                        "email": inv.email,
                        "badge_number": inv.invite_code or f"INV-{str(inv.id)[:4].upper()}",
                        "role": inv.role or "Invited Inspector",
                        "agency": inv.agency.name if inv.agency else "LASBCA",
                        "status": "Invited to Project",
                        "is_current_user": (request.user.email and request.user.email.lower() == inv.email.lower())
                    })

        # 3. Project assigned_inspector field if specified
        if project.assigned_inspector and project.assigned_inspector.strip():
            assigned_str = project.assigned_inspector.strip()
            if assigned_str.lower() not in seen_emails:
                seen_emails.add(assigned_str.lower())
                results.append({
                    "id": f"assign-{str(project.id)[:8]}",
                    "name": assigned_str,
                    "email": f"{assigned_str.lower().replace(' ', '.')}@lasbca.gov.ng" if '@' not in assigned_str else assigned_str,
                    "badge_number": f"LAG-INS-{str(project.id).replace('-', '')[:4].upper()}",
                    "role": "Lead Project Inspector",
                    "agency": "LASBCA",
                    "status": "Lead Inspector",
                    "is_current_user": False
                })

        # 4. If fewer than 2 inspectors found for this project, include accredited inspectors from the roster
        # so inspectors can always message peer inspectors on this project site.
        if len(results) < 2:
            all_inspectors = User.objects.filter(
                Q(email__icontains='inspector') |
                Q(government_profile__role__name__icontains='inspector')
            ).exclude(id=request.user.id).distinct()
            for u in all_inspectors[:3]:
                if u.email.lower() not in seen_emails:
                    seen_emails.add(u.email.lower())
                    name = f"{u.first_name} {u.last_name}".strip() or u.email.split('@')[0].capitalize()
                    badge = f"LAG-INS-{str(u.id).replace('-', '')[:4].upper()}"
                    role = "Accredited Field Inspector"
                    st_ins = StakeholderInspector.objects.filter(user=u).first()
                    if st_ins:
                        badge = st_ins.inspector_id or badge
                        role = st_ins.role_title or role
                    results.append({
                        "id": str(u.id),
                        "name": name,
                        "email": u.email,
                        "badge_number": badge,
                        "role": role,
                        "agency": "LASBCA Field Operations",
                        "status": "Available on Project",
                        "is_current_user": False
                    })

        return Response(results, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def restore_from_cold_storage(self, request, pk=None):
        """Bring a cold-stored project back into the hot working set.
        Director-level and audited — cold storage never deletes anything,
        this only flips the browse flag."""
        from common.permissions import IsDirector
        if not IsDirector().has_permission(request, self):
            return Response({'detail': 'Director-level role required.'},
                            status=status.HTTP_403_FORBIDDEN)
        # get_object() would 404: the default queryset EXCLUDES cold
        # projects — fetch it directly (a malformed id must 404, not 500).
        try:
            project = Project.objects.filter(pk=pk).first()
        except Exception:
            project = None
        if project is None:
            return Response({'detail': 'Project not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if not project.cold_storage:
            return Response({'detail': 'Project is not in cold storage.'},
                            status=status.HTTP_400_BAD_REQUEST)
        project.cold_storage = False
        project.cold_stored_at = None
        project.save(update_fields=['cold_storage', 'cold_stored_at',
                                    'updated_at'])
        try:
            from apps.audit.models import AuditEvent
            from common.permissions import user_role_name
            AuditEvent.objects.create(
                user=request.user if request.user.is_authenticated else None,
                user_name=(request.user.get_full_name() or request.user.email
                           if request.user.is_authenticated else 'System'),
                user_role=user_role_name(request.user)
                if request.user.is_authenticated else 'System',
                action='projects.project.restore_from_cold_storage',
                resource_type='Project', resource_id=str(project.id),
                metadata={'name': project.name})
        except Exception:
            pass
        from django.core.cache import cache
        cache.clear()
        return Response({'id': str(project.id), 'name': project.name,
                         'cold_storage': False})

    @extend_schema(request=ProjectSerializer, responses={201: ProjectSerializer})
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        project = serializer.save(status='PLANNING')
        # Automatically create an Application for this new project to appear in the review queue
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        applicant_user = self.request.user if (self.request.user and self.request.user.is_authenticated) else UserModel.objects.first()
        if applicant_user:
            Application.objects.create(
                project=project,
                applicant=applicant_user,
                application_type='General Construction Permit',
                status='SUBMITTED'
            )
        from django.core.cache import cache
        cache.clear()

    @action(detail=True, methods=['post'], url_path='upload-document')
    def upload_document(self, request, pk=None):
        project = self.get_object()
        file = request.FILES.get('file')
        document_type = request.data.get('document_type')
        name = request.data.get('name', document_type)
        
        if not file or not document_type:
            return Response({"error": "file and document_type are required."}, status=status.HTTP_400_BAD_REQUEST)
            
        doc = ProjectDocument.objects.create(
            project=project,
            file=file,
            document_type=document_type,
            name=name
        )
        serializer = ProjectDocumentSerializer(doc)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: ProjectSerializer})
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=ProjectSerializer, responses={200: ProjectSerializer})
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        serializer.save()
        from django.core.cache import cache
        cache.clear()

    @extend_schema(responses={204: OpenApiResponse(description="Deleted successfully")})
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        instance.delete()
        from django.core.cache import cache
        cache.clear()

@method_decorator(ratelimit(key='ip', rate='30/m', block=True), name='dispatch')
@method_decorator(cache_page(60 * 15), name='list')
@method_decorator(cache_page(60 * 15), name='retrieve')
class BIMModelViewSet(viewsets.ModelViewSet):
    """CRUD API for BIM models linked to a project."""
    serializer_class = BIMModelSerializer

    def get_queryset(self):
        project_pk = self.kwargs.get('project_pk')
        if project_pk:
            return BIMModel.objects.select_related('project').filter(project_id=project_pk).order_by('-created_at')
        return BIMModel.objects.select_related('project').all().order_by('-created_at')

    def perform_create(self, serializer):
        project_pk = self.kwargs.get('project_pk')
        file_obj = self.request.FILES.get('file')
        
        name = "Unnamed Model"
        file_format = "other"
        
        if file_obj:
            name = file_obj.name
            ext = name.split('.')[-1].lower() if '.' in name else ''
            if ext in ['ifc', 'rvt', 'nwd']:
                file_format = ext
                
        if project_pk:
            serializer.save(project_id=project_pk, name=name, file_format=file_format)
        else:
            serializer.save(name=name, file_format=file_format)

from rest_framework.views import APIView
from rest_framework import serializers


from rest_framework.views import APIView
from rest_framework import serializers

class DashboardStatsView(APIView):
    """Returns aggregated stats for the QC dashboard."""
    @extend_schema(
        responses={
            200: inline_serializer(
                name='DashboardStatsResponse',
                fields={
                    'total_scans': serializers.IntegerField(),
                    'active_issues': serializers.IntegerField(),
                    'avg_progress': serializers.FloatField(),
                }
            )
        },
        summary="Retrieve aggregate statistics for the dashboard.",
        tags=["Dashboard"]
    )
    @method_decorator(ratelimit(key='ip', rate='60/m', block=True))
    @method_decorator(cache_page(60 * 5))
    def get(self, request):
        from apps.scans.models import ScanSession, ProgressValidationResult, ThermalAnomaly, Defect
        from apps.inspections.models import Issue
        from django.db.models import Avg
        from django.utils import timezone
        import datetime

        now = timezone.now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        today = now.date()

        total_scans = ScanSession.objects.filter(created_at__gte=start_of_month).count()
        active_issues = Issue.objects.exclude(status__in=['resolved', 'closed']).count()
        
        avg_prog = ProgressValidationResult.objects.aggregate(Avg('progress_score'))['progress_score__avg']
        avg_progress = round((avg_prog or 0.0) * 100, 1)

        # Additional stats for Digital Eye Dashboard
        scans_today = ScanSession.objects.filter(created_at__date=today).count()
        in_processing = ScanSession.objects.filter(status='processing').count()
        
        # Calculate AI Anomalies (Defects + Thermal)
        ai_anomalies = Defect.objects.count() + ThermalAnomaly.objects.count()
        
        # Estimate active scanners from distinct scanner_ids across all sessions
        active_scanners = ScanSession.objects.values('scanner_id').distinct().count()
        if active_scanners == 0:
            # Fallback for MVP if there are sessions but scanner_id is null/empty
            active_scanners = 1 if total_scans > 0 else 0

        return Response({
            'total_scans': total_scans,
            'active_issues': active_issues,
            'avg_progress': avg_progress,
            'scans_today': scans_today,
            'in_processing': in_processing,
            'ai_anomalies': ai_anomalies,
            'active_scanners': active_scanners
        })