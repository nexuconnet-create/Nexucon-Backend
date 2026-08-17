from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet, VersionViewSet, ApprovalViewSet

router = DefaultRouter()
router.register(r'versions', VersionViewSet, basename='version')
router.register(r'approvals', ApprovalViewSet, basename='approval')
router.register(r'', DocumentViewSet, basename='document')

urlpatterns = [
    path('', include(router.urls)),
]
