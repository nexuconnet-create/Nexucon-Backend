from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
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

urlpatterns = [
    # Credential-free BIM GUID import from an uploaded IFC file.
    path('bim-elements/import-ifc/', views.BIMElementImportView.as_view(),
         name='bim-element-import-ifc'),
    path('', include(router.urls)),
]
