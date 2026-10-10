from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProjectViewSet, ProjectMilestoneViewSet

router = DefaultRouter()
router.register(r'milestones', ProjectMilestoneViewSet, basename='projectmilestone')


from .views import BIMModelViewSet, DashboardStatsView
router.register(r'bim-models', BIMModelViewSet, basename='all-bim-models')
router.register(r'(?P<project_pk>[^/.]+)/bim-models', BIMModelViewSet, basename='project-bim-models')
router.register(r'', ProjectViewSet, basename='project')

from rest_framework.permissions import IsAuthenticated

urlpatterns = [
    path('dashboard-stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('assignable/', ProjectViewSet.as_view({'get': 'assignable'}, permission_classes=[IsAuthenticated]), name='project-assignable'),

    path('', include(router.urls)),
]
