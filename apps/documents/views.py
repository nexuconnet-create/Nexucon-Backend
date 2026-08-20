from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from django.utils import timezone
import datetime
from .models import Document, Version, Approval, DocumentTemplate, DocumentFolder
from .serializers import (
    DocumentSerializer, VersionSerializer, ApprovalSerializer,
    DocumentTemplateSerializer, DocumentFolderSerializer
)
from .services import DocumentService

class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.all().select_related('project')
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        folder = self.request.query_params.get('folder')
        discipline = self.request.query_params.get('discipline')
        doc_type = self.request.query_params.get('type') or self.request.query_params.get('document_type')
        status_val = self.request.query_params.get('status')
        starred = self.request.query_params.get('starred')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if folder:
            qs = qs.filter(folder__iexact=folder)
        if discipline and discipline.lower() != 'all':
            qs = qs.filter(discipline__iexact=discipline)
        if doc_type:
            qs = qs.filter(document_type__iexact=doc_type)
        if status_val:
            qs = qs.filter(status__iexact=status_val)
        if starred is not None:
            qs = qs.filter(is_starred=starred.lower() in ('true', '1'))
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(document_reference__icontains=search) |
                Q(uploader_name__icontains=search) |
                Q(discipline__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        doc = DocumentService.upload_document(self.request.data, self.request.user)
        serializer.instance = doc

    @action(detail=True, methods=['post'], url_path='star')
    def toggle_star(self, request, pk=None):
        document = self.get_object()
        updated = DocumentService.toggle_star(document)
        return Response(DocumentSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='stamp')
    def apply_stamp(self, request, pk=None):
        document = self.get_object()
        comments = request.data.get('comments')
        approval = DocumentService.apply_digital_signature_stamp(document, request.user, comments)
        return Response(ApprovalSerializer(approval).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='review')
    def review(self, request, pk=None):
        document = self.get_object()
        status_val = request.data.get('status', 'APPROVED')
        comments = request.data.get('comments', '')
        approval = DocumentService.review_and_decide(document, status_val, comments, request.user)
        return Response(ApprovalSerializer(approval).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='create-version')
    def create_version(self, request, pk=None):
        document = self.get_object()
        version = DocumentService.create_version(document, request.data, request.user)
        return Response(VersionSerializer(version).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='drawings')
    def drawings(self, request):
        qs = self.get_queryset().filter(document_type='DRAWING')
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='approvals-vault')
    def approvals_vault(self, request):
        qs = self.get_queryset().filter(is_digitally_stamped=True)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)


class VersionViewSet(viewsets.ModelViewSet):
    queryset = Version.objects.all().select_related('document')
    serializer_class = VersionSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        document_id = self.request.query_params.get('document')
        if document_id:
            qs = qs.filter(document_id=document_id)
        return qs


class ApprovalViewSet(viewsets.ModelViewSet):
    queryset = Approval.objects.all().select_related('document', 'version')
    serializer_class = ApprovalSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        status_val = self.request.query_params.get('status')
        if status_val:
            qs = qs.filter(status__iexact=status_val)
        return qs


class DocumentTemplateViewSet(viewsets.ModelViewSet):
    queryset = DocumentTemplate.objects.all()
    serializer_class = DocumentTemplateSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category__iexact=category)
        return qs

    def perform_create(self, serializer):
        template = DocumentService.create_template(serializer.validated_data, self.request.user)
        serializer.instance = template


class DocumentFolderViewSet(viewsets.ModelViewSet):
    queryset = DocumentFolder.objects.all()
    serializer_class = DocumentFolderSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs


class DocumentStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        today = timezone.now().date()
        total_docs = Document.objects.count()
        drawings_count = Document.objects.filter(document_type='DRAWING').count()
        approved_count = Document.objects.filter(status='APPROVED').count()
        stamped_count = Document.objects.filter(is_digitally_stamped=True).count()
        
        expired_count = Document.objects.filter(expiry_date__lt=today).count()
        expiring_soon = Document.objects.filter(expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=30)).count()

        folders_count = DocumentFolder.objects.count()

        data = {
            "total_documents": total_docs,
            "folders_count": folders_count,
            "drawings_count": drawings_count,
            "approved_count": approved_count,
            "stamped_approvals_count": stamped_count,
            "expired_count": expired_count,
            "expiring_soon_count": expiring_soon,
            "templates_count": DocumentTemplate.objects.count()
        }
        return Response(data, status=status.HTTP_200_OK)
