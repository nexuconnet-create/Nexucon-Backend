from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    TersusDeviceViewSet, BIMIntegrationViewSet, DocumentSystemIntegrationViewSet,
    GovernmentAPIIntegrationViewSet, APIKeyCredentialViewSet, IntegrationLogViewSet,
    IntegrationStatsViewSet, StaffUserViewSet, CustomRoleViewSet,
    ApprovalWorkflowViewSet, InspectionTemplateViewSet, ComplianceStandardViewSet,
    StatutoryDocumentViewSet, NotificationPreferenceViewSet,
    NotificationRoutingRuleViewSet, WebhookSubscriptionViewSet
)

router = DefaultRouter()

# Integrations
router.register(r'tersus', TersusDeviceViewSet, basename='tersus')
router.register(r'bim', BIMIntegrationViewSet, basename='bim-integration')
router.register(r'documents', DocumentSystemIntegrationViewSet, basename='dms-integration')
router.register(r'government', GovernmentAPIIntegrationViewSet, basename='gov-integration')
router.register(r'api-keys', APIKeyCredentialViewSet, basename='api-key')
router.register(r'logs', IntegrationLogViewSet, basename='integration-log')
router.register(r'stats', IntegrationStatsViewSet, basename='integration-stats')

# Settings
router.register(r'users', StaffUserViewSet, basename='staff-user')
router.register(r'roles', CustomRoleViewSet, basename='custom-role')
router.register(r'workflows', ApprovalWorkflowViewSet, basename='approval-workflow')
router.register(r'templates', InspectionTemplateViewSet, basename='inspection-template')
router.register(r'standards', ComplianceStandardViewSet, basename='compliance-standard')
router.register(r'statutes', StatutoryDocumentViewSet, basename='statutory-document')
router.register(r'notifications', NotificationPreferenceViewSet, basename='notification-preference')
router.register(r'routing-rules', NotificationRoutingRuleViewSet, basename='routing-rule')
router.register(r'webhooks', WebhookSubscriptionViewSet, basename='webhook-subscription')

urlpatterns = [
    path('', include(router.urls)),
]
