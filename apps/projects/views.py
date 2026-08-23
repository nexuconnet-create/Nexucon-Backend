from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly
from django.db.models import Q
from .models import Project, ProjectMilestone, ProjectDocument
from .serializers import ProjectSerializer, ProjectMilestoneSerializer, ProjectDocumentSerializer
from apps.applications.models import Application

class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all().order_by('-created_at')
    serializer_class = ProjectSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = Project.objects.all().order_by('-created_at')
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

    def perform_create(self, serializer):
        # Default new projects to PLANNING status, simulating they are pending review
        project = serializer.save(status='PLANNING')
        # Automatically create an Application for this new project to appear in the review queue
        Application.objects.create(
            project=project,
            applicant=self.request.user,
            application_type='General Construction Permit',
            status='SUBMITTED'
        )

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

class ProjectMilestoneViewSet(viewsets.ModelViewSet):
    queryset = ProjectMilestone.objects.all().order_by('target_date')
    serializer_class = ProjectMilestoneSerializer
