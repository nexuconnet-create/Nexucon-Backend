from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import InspectionViewSet, ChecklistViewSet, FindingViewSet, StopWorkOrderViewSet

router = DefaultRouter()
router.register(r'stop-work-orders', StopWorkOrderViewSet, basename='stop-work-order')
router.register(r'checklists', ChecklistViewSet, basename='checklist')
router.register(r'findings', FindingViewSet, basename='finding')


from .views import IssueViewSet, NonConformanceReportViewSet, CorrectiveActionViewSet
router.register(r'issues', IssueViewSet, basename='issue')
router.register(r'ncrs', NonConformanceReportViewSet, basename='ncr')
router.register(r'corrective-actions', CorrectiveActionViewSet, basename='corrective-action')

# The bare-prefix InspectionViewSet must be registered LAST: its detail route
# ^(?P<pk>[^/.]+)/$ otherwise shadows the sibling prefixes above (issues/,
# ncrs/, ...) and makes those endpoints unreachable.
router.register(r'', InspectionViewSet, basename='inspection')

from . import execution_views

urlpatterns = [
    # Inspection Execution API (plan §5 Week 7) — must precede the router's
    # detail routes so /execution/... paths are matched here.
    path('<uuid:inspection_id>/execution/', execution_views.InspectionExecutionView.as_view(),
         name='inspection-execution'),
    path('<uuid:inspection_id>/execution/checkin/', execution_views.InspectionCheckinView.as_view(),
         name='inspection-checkin'),
    path('<uuid:inspection_id>/execution/submit/', execution_views.InspectionSubmitView.as_view(),
         name='inspection-submit'),
    path('<uuid:inspection_id>/execution/sign-off/', execution_views.InspectionSignOffView.as_view(),
         name='inspection-signoff'),
    path('<uuid:inspection_id>/execution/verify/', execution_views.InspectionVerifyView.as_view(),
         name='inspection-verify'),
    path('', include(router.urls)),
]
