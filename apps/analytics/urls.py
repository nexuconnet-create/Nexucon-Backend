from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    GeneratedReportViewSet, DepartmentPerformanceViewSet,
    OfficerPerformanceViewSet, ExecutiveAnalyticsViewSet,
    PerformanceAnalyticsViewSet, StructuralRiskViewSet,
    ProgressAnalyticsViewSet, InspectionAnalyticsViewSet,
    ComplianceAnalyticsViewSet, IndustryAnalyticsViewSet,
    FinancialAnalyticsViewSet, AgencyPerformanceViewSet
)

router = DefaultRouter()
router.register(r'reports', GeneratedReportViewSet, basename='analytics-report')
router.register(r'departments', DepartmentPerformanceViewSet, basename='analytics-department')
router.register(r'officers', OfficerPerformanceViewSet, basename='analytics-officer')
router.register(r'performance', PerformanceAnalyticsViewSet, basename='analytics-performance')
router.register(r'risk', StructuralRiskViewSet, basename='analytics-risk')
router.register(r'progress', ProgressAnalyticsViewSet, basename='analytics-progress')
router.register(r'inspections', InspectionAnalyticsViewSet, basename='analytics-inspections')
router.register(r'compliance', ComplianceAnalyticsViewSet, basename='analytics-compliance')
router.register(r'industry', IndustryAnalyticsViewSet, basename='analytics-industry')
router.register(r'financial', FinancialAnalyticsViewSet, basename='analytics-financial')
router.register(r'agency', AgencyPerformanceViewSet, basename='analytics-agency')
router.register(r'overview', ExecutiveAnalyticsViewSet, basename='analytics-overview')

urlpatterns = [
    path('', include(router.urls)),
]
