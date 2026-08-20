from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    BIMModelViewSet, BIMModelVersionViewSet, BIMClashViewSet,
    BIMAnnotationViewSet, BIMProgressValidationViewSet, BIMStatsViewSet
)

router = DefaultRouter()
router.register(r'models', BIMModelViewSet, basename='bim-models')
router.register(r'versions', BIMModelVersionViewSet, basename='bim-versions')
router.register(r'clashes', BIMClashViewSet, basename='bim-clashes')
router.register(r'annotations', BIMAnnotationViewSet, basename='bim-annotations')
router.register(r'progress-validation', BIMProgressValidationViewSet, basename='bim-progress-validation')
router.register(r'stats', BIMStatsViewSet, basename='bim-stats')

urlpatterns = [
    path('', include(router.urls)),
]
