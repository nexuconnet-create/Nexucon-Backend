from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, Version, Approval
from .serializers import DocumentSerializer, VersionSerializer, ApprovalSerializer

class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.all().order_by('-created_at')
    serializer_class = DocumentSerializer
    
    @action(detail=True, methods=['get'], url_path='download-url/(?P<version_id>[^/.]+)')
    def download_url(self, request, pk=None, version_id=None):
        try:
            document = self.get_object()
            version = document.versions.get(id=version_id)
            
            if not version.file:
                return Response({'success': False, 'message': 'No file associated with this version.'}, status=status.HTTP_404_NOT_FOUND)
            
            # django-storages S3 backend generates signed URLs automatically when calling .url 
            # if AWS_QUERYSTRING_AUTH is True (default). If local, it just returns the local path.
            url = version.file.url
            
            return Response({
                'success': True, 
                'message': 'Signed URL generated',
                'data': {'url': url}
            })
        except Version.DoesNotExist:
            return Response({'success': False, 'message': 'Version not found.'}, status=status.HTTP_404_NOT_FOUND)

class VersionViewSet(viewsets.ModelViewSet):
    queryset = Version.objects.all().order_by('-uploaded_at')
    serializer_class = VersionSerializer

class ApprovalViewSet(viewsets.ModelViewSet):
    queryset = Approval.objects.all().order_by('-reviewed_at')
    serializer_class = ApprovalSerializer
