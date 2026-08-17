from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProjectViewSet, ProjectMilestoneViewSet

router = DefaultRouter()
router.register(r'projects', ProjectViewSet, basename='project')
router.register(r'milestones', ProjectMilestoneViewSet, basename='projectmilestone')

urlpatterns = [
    path('', include(router.urls)),
]
