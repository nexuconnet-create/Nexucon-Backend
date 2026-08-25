from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from django.utils import timezone
import datetime
import uuid
from .models import (
    Document, Version, Approval, DocumentReview, 
    DocumentAccess, DocumentAudit, DocumentTemplate, DocumentFolder
)
from .serializers import (
    DocumentSerializer, VersionSerializer, ApprovalSerializer,
    DocumentReviewSerializer, DocumentAccessSerializer, DocumentAuditSerializer,
    DocumentTemplateSerializer, DocumentFolderSerializer
)
from .services import DocumentService


def is_valid_uuid(val):
    if not val or str(val).lower() in ('undefined', 'null', 'none', ''):
        return False
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.all().select_related('project', 'linked_bim_model', 'linked_inspection', 'linked_compliance_case')
    serializer_class = DocumentSerializer
    parser_classes = (MultiPartParser, FormParser, JSONParser)
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        try:
            file_obj = request.FILES.get('file')
            data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            
            project_val = data.get('project_id') or data.get('project')
            if isinstance(project_val, list) and project_val:
                project_val = project_val[0]
            data['project'] = project_val
            data['project_id'] = project_val

            doc = DocumentService.upload_document(data, request.user, file_obj=file_obj)
            serializer = self.get_serializer(doc)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        folder = self.request.query_params.get('folder')
        discipline = self.request.query_params.get('discipline')
        doc_type = self.request.query_params.get('type') or self.request.query_params.get('document_type')
        status_val = self.request.query_params.get('status')
        starred = self.request.query_params.get('starred')
        search = self.request.query_params.get('search')
        bim_model_id = self.request.query_params.get('bim_model')
        inspection_id = self.request.query_params.get('inspection')

        if is_valid_uuid(project_id):
            qs = qs.filter(project_id=project_id)
        if folder and str(folder).lower() not in ('undefined', 'null', 'none', ''):
            qs = qs.filter(folder__iexact=folder)
        if discipline and str(discipline).lower() not in ('undefined', 'null', 'none', '', 'all'):
            qs = qs.filter(discipline__iexact=discipline)
        if doc_type and str(doc_type).lower() not in ('undefined', 'null', 'none', '', 'all'):
            if doc_type.upper() in ('DRAWING', 'SUBMITTED_DRAWING'):
                qs = qs.filter(document_type__in=['DRAWING', 'SUBMITTED_DRAWING'])
            elif doc_type.upper() in ('REPORT', 'TECHNICAL_REPORT'):
                qs = qs.filter(document_type__in=['REPORT', 'TECHNICAL_REPORT'])
            elif doc_type.upper() in ('COMPLIANCE', 'COMPLIANCE_DOCUMENT', 'COMPLIANCE_CERTIFICATE'):
                qs = qs.filter(document_type__in=['COMPLIANCE', 'COMPLIANCE_DOCUMENT', 'COMPLIANCE_CERTIFICATE'])
            elif doc_type.upper() == 'INSPECTION_REPORT':
                qs = qs.filter(document_type='INSPECTION_REPORT')
            else:
                qs = qs.filter(document_type__iexact=doc_type)
        if status_val and str(status_val).lower() not in ('undefined', 'null', 'none', '', 'all'):
            qs = qs.filter(status__iexact=status_val)
        if starred is not None and str(starred).lower() not in ('undefined', 'null', 'none', ''):
            qs = qs.filter(is_starred=str(starred).lower() in ('true', '1'))
        if is_valid_uuid(bim_model_id):
            qs = qs.filter(linked_bim_model_id=bim_model_id)
        if is_valid_uuid(inspection_id):
            qs = qs.filter(linked_inspection_id=inspection_id)
        if search and str(search).lower() not in ('undefined', 'null', 'none', ''):
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(document_reference__icontains=search) |
                Q(uploader_name__icontains=search) |
                Q(discipline__icontains=search) |
                Q(folder__icontains=search)
            )
        return qs

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
        review_obj = DocumentService.review_and_decide(document, status_val, comments, request.user)
        return Response(DocumentReviewSerializer(review_obj).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='create-version')
    def create_version(self, request, pk=None):
        document = self.get_object()
        file_obj = request.FILES.get('file')
        version = DocumentService.create_version(document, request.data, request.user, file_obj=file_obj)
        return Response(VersionSerializer(version).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='download')
    def download(self, request, pk=None):
        document = self.get_object()
        DocumentService.log_audit(
            user=request.user,
            action="DOCUMENT_DOWNLOADED",
            resource_id=document.id,
            new_state={"title": document.title, "file_url": document.file_url},
            document=document
        )
        return Response({
            "download_url": document.file_url,
            "title": document.title,
            "file_size": document.file_size,
            "file_format": document.file_format,
            "signature_hash": document.signature_hash
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='link-bim')
    def link_bim(self, request, pk=None):
        document = self.get_object()
        bim_id = request.data.get('bim_model_id') or request.data.get('linked_bim_model')
        doc = DocumentService.link_to_bim_model(document, bim_id, request.user)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='link-inspection')
    def link_inspection(self, request, pk=None):
        document = self.get_object()
        insp_id = request.data.get('inspection_id') or request.data.get('linked_inspection')
        doc = DocumentService.link_to_inspection(document, insp_id, request.user)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='link-compliance')
    def link_compliance(self, request, pk=None):
        document = self.get_object()
        ncr_id = request.data.get('compliance_case_id') or request.data.get('linked_compliance_case')
        doc = DocumentService.link_to_compliance_case(document, ncr_id, request.user)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='drawings')
    def drawings(self, request):
        qs = self.get_queryset().filter(document_type__in=['DRAWING', 'SUBMITTED_DRAWING'])
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
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        document_id = self.request.query_params.get('document')
        if is_valid_uuid(document_id):
            qs = qs.filter(document_id=document_id)
        return qs

    @action(detail=False, methods=['post'], url_path='compare')
    def compare(self, request):
        v_a = request.data.get('version_a')
        v_b = request.data.get('version_b')
        if not v_a or not v_b:
            return Response({"error": "version_a and version_b are required."}, status=status.HTTP_400_BAD_REQUEST)
        diff = DocumentService.compare_versions(v_a, v_b)
        return Response(diff, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='download')
    def download(self, request, pk=None):
        version = self.get_object()
        return Response({
            "download_url": version.file_url or version.document.file_url,
            "version_label": version.version_label,
            "file_size": version.file_size,
            "signature_hash": version.signature_hash
        }, status=status.HTTP_200_OK)


class ApprovalViewSet(viewsets.ModelViewSet):
    queryset = Approval.objects.all().select_related('document', 'version')
    serializer_class = ApprovalSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        status_val = self.request.query_params.get('status')
        project_id = self.request.query_params.get('project')
        if status_val and str(status_val).lower() not in ('undefined', 'null', 'none', '', 'all'):
            qs = qs.filter(status__iexact=status_val)
        if is_valid_uuid(project_id):
            qs = qs.filter(document__project_id=project_id)
        return qs

    @action(detail=True, methods=['get'], url_path='verify')
    def verify(self, request, pk=None):
        approval = self.get_object()
        return Response({
            "approval_reference": approval.approval_reference,
            "status": approval.status,
            "approved_by": approval.approved_by_name,
            "signature_hash": approval.signature_hash,
            "reviewed_at": approval.reviewed_at,
            "is_valid": bool(approval.signature_hash),
            "document_title": approval.document.title if approval.document else 'Project Document'
        }, status=status.HTTP_200_OK)


class DocumentReviewViewSet(viewsets.ModelViewSet):
    queryset = DocumentReview.objects.all().select_related('document', 'version', 'reviewer')
    serializer_class = DocumentReviewSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        doc_id = self.request.query_params.get('document')
        if is_valid_uuid(doc_id):
            qs = qs.filter(document_id=doc_id)
        return qs


class DocumentFolderViewSet(viewsets.ModelViewSet):
    queryset = DocumentFolder.objects.all().select_related('project')
    serializer_class = DocumentFolderSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        if is_valid_uuid(project_id):
            qs = qs.filter(project_id=project_id)
        return qs


class DocumentTemplateViewSet(viewsets.ModelViewSet):
    queryset = DocumentTemplate.objects.all()
    serializer_class = DocumentTemplateSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        if category and str(category).lower() not in ('undefined', 'null', 'none', '', 'all'):
            qs = qs.filter(category__iexact=category)
        return qs

    def perform_create(self, serializer):
        template = DocumentService.create_template(serializer.validated_data, self.request.user)
        serializer.instance = template


class DocumentStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    def list(self, request):
        return self._compute_stats(request)

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        return self._compute_stats(request)

    def _compute_stats(self, request):
        project_id = request.query_params.get('project')
        qs = Document.objects.all()
        folder_qs = DocumentFolder.objects.all()
        if is_valid_uuid(project_id):
            qs = qs.filter(project_id=project_id)
            folder_qs = folder_qs.filter(project_id=project_id)

        total_docs = qs.count()
        drawings_count = qs.filter(document_type__in=['DRAWING', 'SUBMITTED_DRAWING']).count()
        reports_count = qs.filter(document_type__in=['REPORT', 'TECHNICAL_REPORT']).count()
        compliance_count = qs.filter(document_type__in=['COMPLIANCE', 'COMPLIANCE_DOCUMENT', 'COMPLIANCE_CERTIFICATE']).count()
        inspection_count = qs.filter(document_type='INSPECTION_REPORT').count()
        approved_count = qs.filter(status='APPROVED').count()
        pending_count = qs.filter(status__in=['PENDING_REVIEW', 'UNDER_REVIEW']).count()
        stamped_count = qs.filter(is_digitally_stamped=True).count()
        folders_count = folder_qs.count()
        templates_count = DocumentTemplate.objects.count()

        today = timezone.now().date()
        expired_count = qs.filter(expiry_date__isnull=False, expiry_date__lt=today).count()
        expiring_soon_count = qs.filter(
            expiry_date__isnull=False,
            expiry_date__gte=today,
            expiry_date__lte=today + datetime.timedelta(days=30)
        ).count()

        return Response({
            "total_documents": total_docs,
            "drawings_count": drawings_count,
            "reports_count": reports_count,
            "compliance_count": compliance_count,
            "inspection_reports_count": inspection_count,
            "approved_count": approved_count,
            "pending_count": pending_count,
            "stamped_count": stamped_count,
            "stamped_approvals_count": stamped_count,
            "folders_count": folders_count,
            "templates_count": templates_count,
            "expired_count": expired_count,
            "expiring_soon_count": expiring_soon_count,
            "storage_bucket": "nexucondocument",
            "storage_provider": "Cloudflare R2"
        }, status=status.HTTP_200_OK)
