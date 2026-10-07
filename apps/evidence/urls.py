from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'records', views.EvidenceRecordViewSet, basename='evidence-record')
router.register(r'analyses', views.AIAnalysisRecordViewSet, basename='ai-analysis')
router.register(r'findings', views.CorrelationFindingViewSet, basename='correlation-finding')

urlpatterns = [
    # File evidence — declared before the router include. The converters differ
    # (`uuid` here, `str` inside the router) so a literal could not be swallowed
    # by these, but the ordering is kept explicit because the identical mistake
    # is already recorded as a fixed bug in `scans/urls.py` and
    # `inspections/urls.py`, and a regression test asserts `/records/` still
    # resolves.
    path('upload/', views.EvidenceFileUploadView.as_view(), name='evidence-upload'),
    path('inspection/<uuid:inspection_id>/', views.EvidenceByInspectionView.as_view(),
         name='evidence-by-inspection'),
    path('<uuid:pk>/verify/', views.EvidenceVerifyView.as_view(), name='evidence-verify'),
    path('<uuid:pk>/', views.EvidenceRecordDetailView.as_view(), name='evidence-detail'),

    # Project-Level AI Intelligence
    path('intelligence/projects/<uuid:project_id>/',
         views.ProjectIntelligenceView.as_view(), name='project-intelligence'),
    path('intelligence/projects/<uuid:project_id>/correlate/',
         views.ProjectCorrelationView.as_view(), name='project-correlate'),

    # HQ Command AI (Week 9)
    path('hq/overview/', views.HQOverviewView.as_view(), name='hq-overview'),
    path('hq/districts/', views.HQDistrictMatrixView.as_view(), name='hq-district-matrix'),
    path('hq/executive-briefing/', views.HQExecutiveBriefingView.as_view(), name='hq-executive-briefing'),
    path('hq/inspector-analytics/', views.HQInspectorAnalyticsView.as_view(), name='hq-inspector-analytics'),

    # Field Evidence Photo Upload & Attestation
    path('upload/', views.EvidenceUploadView.as_view(), name='evidence-upload'),
    path('<uuid:pk>/verify/', views.EvidenceVerifyView.as_view(), name='evidence-verify-uuid'),
    path('<str:pk>/verify/', views.EvidenceVerifyView.as_view(), name='evidence-verify-ref'),

    path('', include(router.urls)),
]
