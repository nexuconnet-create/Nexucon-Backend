from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    BIMStructuralElementViewSet,
    GPRScanViewSet,
    PunditTestViewSet,
    DigitalEyeFindingViewSet,
    AIAnalysisViewSet,
    ProcessingQueueJobViewSet,
    EvidenceSpatialPointViewSet,
    DeviceReportViewSet,
    digital_eye_stats,
    trimble_status,
    trimble_sync,
    download_pdf_report,
)

router = DefaultRouter()
router.register(r'elements', BIMStructuralElementViewSet, basename='digital-eye-elements')
router.register(r'gpr', GPRScanViewSet, basename='digital-eye-gpr')
router.register(r'pundit', PunditTestViewSet, basename='digital-eye-pundit')
router.register(r'findings', DigitalEyeFindingViewSet, basename='digital-eye-findings')
router.register(r'ai-analysis', AIAnalysisViewSet, basename='digital-eye-ai-analysis')
router.register(r'queue', ProcessingQueueJobViewSet, basename='digital-eye-queue')
router.register(r'spatial-map', EvidenceSpatialPointViewSet, basename='digital-eye-spatial-map')
router.register(r'reports/devices', DeviceReportViewSet, basename='digital-eye-device-reports')

urlpatterns = [
    path('stats/', digital_eye_stats, name='digital-eye-stats'),
    path('trimble/status/', trimble_status, name='digital-eye-trimble-status'),
    path('trimble/sync/', trimble_sync, name='digital-eye-trimble-sync'),
    path('reports/download/pdf/', download_pdf_report, name='digital-eye-download-pdf'),
    path('', include(router.urls)),
]
