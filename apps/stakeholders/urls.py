from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ConsultantViewSet, ContractorViewSet, InspectorViewSet, CertificationViewSet, TrainingRecordViewSet

router = DefaultRouter()
router.register(r'consultants', ConsultantViewSet, basename='consultant')
router.register(r'contractors', ContractorViewSet, basename='contractor')
router.register(r'inspectors', InspectorViewSet, basename='inspector')
router.register(r'certifications', CertificationViewSet, basename='certification')
router.register(r'trainings', TrainingRecordViewSet, basename='training')

urlpatterns = [
    path('', include(router.urls)),
]
