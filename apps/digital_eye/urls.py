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
router.register(r'nexucon-link/curves', views.StrengthCurveViewSet, basename='strength-curve')
# Ground-truth core results (path-to-95% Layer 3): lab-crushed cores whose
# strength pairs with the in-situ UPV test at the same location.
router.register(r'nexucon-link/core-samples', views.CoreSampleViewSet, basename='core-sample')
router.register(r'gnss-surveys', views.GnssSurveyViewSet, basename='gnss-survey')
router.register(r'gnss-benchmarks', views.GnssBenchmarkViewSet, basename='gnss-benchmark')
router.register(r'gnss-boundary-points', views.GnssBoundaryPointViewSet, basename='gnss-boundary-point')
router.register(r'bim-elements', views.BIMElementMappingViewSet, basename='bim-element')
router.register(r'live-streams', views.LiveStreamViewSet, basename='live-stream')
router.register(r'trimble/connections', views.TrimbleConnectionViewSet, basename='trimble-connection')
router.register(r'trimble/projects', views.TrimbleProjectViewSet, basename='trimble-project')

# Scan-to-BIM & AI analytics read endpoints. Read-only mirrors of the data
# owned by the scoped viewsets above — the authoritative write paths are
# 'pundit-tests', 'gpr-surveys' and 'bim-elements'.
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
    path('bim-elements/geometry/', views.BIMModelGeometryView.as_view(),
         name='bim-element-geometry'),
    # Engineer review of a PUNDIT AI analysis (client principle 5).
    path('pundit-analysis-review/<uuid:analysis_id>/',
         views.PunditAnalysisReviewView.as_view(),
         name='pundit-analysis-review'),
    # Nexucon Link platform system settings (wireframe "System Settings"):
    # the default curve type, standard and display units.
    path('nexucon-link/settings/', views.NexuconLinkSettingsView.as_view(),
         name='nexucon-link-settings'),

    # Removed: 'stats/', 'trimble/status/', 'trimble/sync/' and
    # 'reports/download/pdf/' returned fabricated counters, a fake CONNECTED
    # Trimble connection, invented sync totals and a stub PDF respectively.
    # See the note in views.py. Trimble state lives at 'trimble/connections/';
    # real dossiers stream from the reports app.

    path('', include(router.urls)),
]
