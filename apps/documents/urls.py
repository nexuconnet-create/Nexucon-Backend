from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DocumentViewSet, VersionViewSet, ApprovalViewSet,
    DocumentTemplateViewSet, DocumentFolderViewSet, DocumentStatsViewSet
)

router = DefaultRouter()
router.register(r'documents', DocumentViewSet, basename='documents')
router.register(r'versions', VersionViewSet, basename='document-versions')
router.register(r'approvals', ApprovalViewSet, basename='document-approvals')
router.register(r'templates', DocumentTemplateViewSet, basename='document-templates')
router.register(r'folders', DocumentFolderViewSet, basename='document-folders')
router.register(r'stats', DocumentStatsViewSet, basename='document-stats')

urlpatterns = [
    path('', include(router.urls)),
]
