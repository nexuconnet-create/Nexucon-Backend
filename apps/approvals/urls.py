from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ApprovalRequestViewSet, ApprovalDecisionViewSet,
    TechnicalCriteriaViewSet, ApprovalStatsViewSet
)

router = DefaultRouter()
router.register(r'requests', ApprovalRequestViewSet, basename='approvals-requests')
router.register(r'decisions', ApprovalDecisionViewSet, basename='approvals-decisions')
router.register(r'criteria', TechnicalCriteriaViewSet, basename='approvals-criteria')
router.register(r'stats', ApprovalStatsViewSet, basename='approvals-stats')

urlpatterns = [
    path('', include(router.urls)),
]
