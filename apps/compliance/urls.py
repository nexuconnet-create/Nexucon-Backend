from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    NonConformanceReportViewSet, CorrectiveActionPlanViewSet,
    RegulatoryRequirementViewSet, ComplianceReviewViewSet,
    ComplianceCertificateViewSet, ComplianceStatsViewSet,
    EscalationRuleViewSet
)

router = DefaultRouter()
router.register(r'ncrs', NonConformanceReportViewSet, basename='compliance-ncrs')
router.register(r'capas', CorrectiveActionPlanViewSet, basename='compliance-capas')
router.register(r'requirements', RegulatoryRequirementViewSet, basename='compliance-requirements')
router.register(r'reviews', ComplianceReviewViewSet, basename='compliance-reviews')
router.register(r'certificates', ComplianceCertificateViewSet, basename='compliance-certificates')
router.register(r'escalation-rules', EscalationRuleViewSet, basename='compliance-escalation-rules')
router.register(r'stats', ComplianceStatsViewSet, basename='compliance-stats')

urlpatterns = [
    path('', include(router.urls)),
]
