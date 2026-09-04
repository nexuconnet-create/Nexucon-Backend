from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'records', views.EvidenceRecordViewSet, basename='evidence-record')
router.register(r'analyses', views.AIAnalysisRecordViewSet, basename='ai-analysis')
router.register(r'findings', views.CorrelationFindingViewSet, basename='correlation-finding')

urlpatterns = [
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

    path('', include(router.urls)),
]
