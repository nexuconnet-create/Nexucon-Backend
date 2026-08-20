from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DeveloperViewSet, ContractorViewSet, ConsultantViewSet,
    InspectorViewSet, LicensedProfessionalViewSet, ProjectStakeholderTeamViewSet,
    BlacklistRecordViewSet, StakeholderMeetingViewSet, StakeholderMessageViewSet,
    CertificationViewSet, TrainingRecordViewSet, StakeholderStatsViewSet
)

router = DefaultRouter()
router.register(r'developers', DeveloperViewSet, basename='developer')
router.register(r'contractors', ContractorViewSet, basename='contractor')
router.register(r'consultants', ConsultantViewSet, basename='consultant')
router.register(r'inspectors', InspectorViewSet, basename='inspector')
router.register(r'professionals', LicensedProfessionalViewSet, basename='professional')
router.register(r'teams', ProjectStakeholderTeamViewSet, basename='team')
router.register(r'blacklist', BlacklistRecordViewSet, basename='blacklist')
router.register(r'meetings', StakeholderMeetingViewSet, basename='meeting')
router.register(r'messages', StakeholderMessageViewSet, basename='message')
router.register(r'certifications', CertificationViewSet, basename='certification')
router.register(r'trainings', TrainingRecordViewSet, basename='training')
router.register(r'stats', StakeholderStatsViewSet, basename='stakeholder-stats')

urlpatterns = [
    path('', include(router.urls)),
]
