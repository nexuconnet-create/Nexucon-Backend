from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()

# Sensory hub & hardware endpoints (HEAD)
router.register(r'devices', views.FieldDeviceViewSet, basename='field-device')
router.register(r'files', views.SensorDataFileViewSet, basename='sensor-file')
router.register(r'gpr-surveys', views.GPRSurveyViewSet, basename='gpr-survey')
router.register(r'gpr-anomalies', views.GPRAnomalyViewSet, basename='gpr-anomaly')
router.register(r'pundit-tests', views.PUNDITTestViewSet, basename='pundit-test')
router.register(r'gnss-surveys', views.GnssSurveyViewSet, basename='gnss-survey')
router.register(r'gnss-benchmarks', views.GnssBenchmarkViewSet, basename='gnss-benchmark')
router.register(r'gnss-boundary-points', views.GnssBoundaryPointViewSet, basename='gnss-boundary-point')
router.register(r'bim-elements', views.BIMElementMappingViewSet, basename='bim-element')
router.register(r'live-streams', views.LiveStreamViewSet, basename='live-stream')
router.register(r'trimble/connections', views.TrimbleConnectionViewSet, basename='trimble-connection')
router.register(r'trimble/projects', views.TrimbleProjectViewSet, basename='trimble-project')

# Scan-to-BIM & AI analytics endpoints (origin/main)
router.register(r'elements', views.BIMStructuralElementViewSet, basename='digital-eye-elements')
router.register(r'gpr', views.GPRScanViewSet, basename='digital-eye-gpr')
router.register(r'pundit', views.PunditTestViewSet, basename='digital-eye-pundit')
router.register(r'findings', views.DigitalEyeFindingViewSet, basename='digital-eye-findings')
router.register(r'ai-analysis', views.AIAnalysisViewSet, basename='digital-eye-ai-analysis')
router.register(r'queue', views.ProcessingQueueJobViewSet, basename='digital-eye-queue')
router.register(r'spatial-map', views.EvidenceSpatialPointViewSet, basename='digital-eye-spatial-map')
router.register(r'reports/devices', views.DeviceReportViewSet, basename='digital-eye-device-reports')

urlpatterns = [
    # Custom endpoints (HEAD)
    path('bim-elements/import-ifc/', views.BIMElementImportView.as_view(),
         name='bim-element-import-ifc'),

    # Custom endpoints (origin/main)
    path('stats/', views.digital_eye_stats, name='digital-eye-stats'),
    path('trimble/status/', views.trimble_status, name='digital-eye-trimble-status'),
    path('trimble/sync/', views.trimble_sync, name='digital-eye-trimble-sync'),
    path('reports/download/pdf/', views.download_pdf_report, name='digital-eye-download-pdf'),

    path('', include(router.urls)),
]
