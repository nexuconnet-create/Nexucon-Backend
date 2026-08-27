from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription, AgencyProfile, ReportTemplate
)

User = get_user_model()

class AgencyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgencyProfile
        fields = '__all__'

class ReportTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportTemplate
        fields = '__all__'

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
        fields = (
            'id', 'name', 'key_prefix', 'app_type', 'volume_tier', 'status',
            'rate_limit_per_min', 'allowed_ips', 'last_used_at', 'revoked_at',
            'expires_at', 'created_at'
        )

class IntegrationLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationLog
        fields = '__all__'

class UserInvitationSerializer(serializers.ModelSerializer):
    invited_by_email = serializers.ReadOnlyField(source='invited_by.email')

    class Meta:
        model = UserInvitation
        fields = '__all__'

class StaffUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'is_active', 'is_staff', 'is_superuser',
            'role_name', 'department', 'status', 'last_login', 'created_at'
        )

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.email.split('@')[0].capitalize()

    def get_role_name(self, obj):
        if obj.is_superuser:
            return "Government Administrator"
        group = obj.groups.first()
        return group.name if group else "Technical Reviewer"

    def get_department(self, obj):
        return "Physical Planning & Urban Dev"

    def get_status(self, obj):
        return "ACTIVE" if obj.is_active else "SUSPENDED"

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
