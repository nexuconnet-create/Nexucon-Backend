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

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(name__icontains=search_param) |
                Q(reference_number__icontains=search_param) |
                Q(site_location__icontains=search_param) |
                Q(developer_name__icontains=search_param)
            )

        return queryset

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