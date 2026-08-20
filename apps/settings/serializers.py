from rest_framework import serializers
from .models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription
)

class TersusDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TersusDevice
        fields = '__all__'

class BIMIntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = BIMIntegration
        fields = '__all__'

class DocumentSystemIntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentSystemIntegration
        fields = '__all__'

class GovernmentAPIIntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = GovernmentAPIIntegration
        fields = '__all__'

class APIKeyCredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = APIKeyCredential
        fields = ('id', 'name', 'key_prefix', 'app_type', 'volume_tier', 'status', 'last_used_at', 'created_at')

class IntegrationLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationLog
        fields = '__all__'

class UserInvitationSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserInvitation
        fields = '__all__'

class RolePermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolePermission
        fields = '__all__'

class CustomRoleSerializer(serializers.ModelSerializer):
    permissions = RolePermissionSerializer(many=True, read_only=True)

    class Meta:
        model = CustomRole
        fields = '__all__'

class WorkflowStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStep
        fields = '__all__'

class ApprovalWorkflowSerializer(serializers.ModelSerializer):
    steps = WorkflowStepSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalWorkflow
        fields = '__all__'

class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistItem
        fields = '__all__'

class InspectionTemplateSerializer(serializers.ModelSerializer):
    items = ChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = InspectionTemplate
        fields = '__all__'

class ComplianceStandardSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComplianceStandard
        fields = '__all__'

class StatutoryDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StatutoryDocument
        fields = '__all__'

class NotificationRoutingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationRoutingRule
        fields = '__all__'

class NotificationPreferenceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreferenceCategory
        fields = '__all__'

class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookSubscription
        fields = '__all__'
