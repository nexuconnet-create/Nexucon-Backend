from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    GeneratedReportViewSet, DepartmentPerformanceViewSet,
    OfficerPerformanceViewSet, RiskAssessmentViewSet,
    ExecutiveAnalyticsViewSet
)

router = DefaultRouter()
router.register(r'reports', GeneratedReportViewSet, basename='analytics-reports')
router.register(r'departments', DepartmentPerformanceViewSet, basename='analytics-departments')
router.register(r'officers', OfficerPerformanceViewSet, basename='analytics-officers')
router.register(r'risk', RiskAssessmentViewSet, basename='analytics-risk')
router.register(r'overview', ExecutiveAnalyticsViewSet, basename='analytics-overview')

urlpatterns = [
    path('', include(router.urls)),
]
