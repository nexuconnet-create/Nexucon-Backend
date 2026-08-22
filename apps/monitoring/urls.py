from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DailySiteUpdateViewSet, FieldObservationViewSet,
    SiteIssueViewSet, ConstructionMilestoneViewSet,
    SiteVerificationViewSet, MonitoringStatsViewSet,
    SiteProgressViewSet
)

router = DefaultRouter()
router.register(r'updates', DailySiteUpdateViewSet, basename='site-update')
router.register(r'observations', FieldObservationViewSet, basename='field-observation')
router.register(r'issues', SiteIssueViewSet, basename='site-issue')
router.register(r'milestones', ConstructionMilestoneViewSet, basename='construction-milestone')
router.register(r'verifications', SiteVerificationViewSet, basename='site-verification')
router.register(r'stats', MonitoringStatsViewSet, basename='monitoring-stats')
router.register(r'progress', SiteProgressViewSet, basename='site-progress')

urlpatterns = [
    path('', include(router.urls)),
]
