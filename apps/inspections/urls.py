from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import InspectionViewSet, ChecklistViewSet, FindingViewSet, StopWorkOrderViewSet

router = DefaultRouter()
router.register(r'stop-work-orders', StopWorkOrderViewSet, basename='stop-work-order')
router.register(r'checklists', ChecklistViewSet, basename='checklist')
router.register(r'findings', FindingViewSet, basename='finding')
router.register(r'', InspectionViewSet, basename='inspection')


from .views import IssueViewSet, NonConformanceReportViewSet, CorrectiveActionViewSet
router.register(r'issues', IssueViewSet, basename='issue')
router.register(r'ncrs', NonConformanceReportViewSet, basename='ncr')
router.register(r'corrective-actions', CorrectiveActionViewSet, basename='corrective-action')

urlpatterns = [
    path('', include(router.urls)),
]
