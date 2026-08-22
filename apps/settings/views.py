from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription
)
from .serializers import (
    TersusDeviceSerializer, BIMIntegrationSerializer, DocumentSystemIntegrationSerializer,
    GovernmentAPIIntegrationSerializer, APIKeyCredentialSerializer, IntegrationLogSerializer,
    UserInvitationSerializer, CustomRoleSerializer, RolePermissionSerializer,
    ApprovalWorkflowSerializer, WorkflowStepSerializer, InspectionTemplateSerializer,
    ChecklistItemSerializer, ComplianceStandardSerializer, StatutoryDocumentSerializer,
    NotificationRoutingRuleSerializer, NotificationPreferenceCategorySerializer,
    WebhookSubscriptionSerializer
)
from .services import IntegrationService, SettingsService

# ==========================================
# 1. INTEGRATION VIEWSETS
# ==========================================

class TersusDeviceViewSet(viewsets.ModelViewSet):
    queryset = TersusDevice.objects.all().order_by('-created_at')
    serializer_class = TersusDeviceSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(device_id__icontains=search) | Q(device_type__icontains=search))
        return qs

    @action(detail=True, methods=['post'], url_path='force-sync')
    def force_sync(self, request, pk=None):
        device = self.get_object()
        updated = IntegrationService.force_sync_device(device.device_id, request.user)
        return Response(TersusDeviceSerializer(updated).data, status=status.HTTP_200_OK)


class BIMIntegrationViewSet(viewsets.ModelViewSet):
    queryset = BIMIntegration.objects.all().order_by('provider')
    serializer_class = BIMIntegrationSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        return super().get_queryset()

    @action(detail=True, methods=['post'], url_path='sync')
    def sync_platform(self, request, pk=None):
        bim = IntegrationService.sync_bim_platform(pk, request.user)
        return Response(BIMIntegrationSerializer(bim).data, status=status.HTTP_200_OK)


class DocumentSystemIntegrationViewSet(viewsets.ModelViewSet):
    queryset = DocumentSystemIntegration.objects.all().order_by('name')
    serializer_class = DocumentSystemIntegrationSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        return super().get_queryset()

    @action(detail=True, methods=['post'], url_path='sync')
    def sync_system(self, request, pk=None):
        dms = IntegrationService.sync_document_system(pk, request.user)
        return Response(DocumentSystemIntegrationSerializer(dms).data, status=status.HTTP_200_OK)


class GovernmentAPIIntegrationViewSet(viewsets.ModelViewSet):
    queryset = GovernmentAPIIntegration.objects.all().order_by('name')
    serializer_class = GovernmentAPIIntegrationSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        return super().get_queryset()

    @action(detail=True, methods=['post'], url_path='test-connection')
    def test_connection(self, request, pk=None):
        gov = self.get_object()
        res = IntegrationService.verify_government_api(gov.api_key_identifier, request.user)
        return Response(GovernmentAPIIntegrationSerializer(res).data, status=status.HTTP_200_OK)


class APIKeyCredentialViewSet(viewsets.ModelViewSet):
    queryset = APIKeyCredential.objects.all().order_by('-created_at')
    serializer_class = APIKeyCredentialSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        return super().get_queryset()

    def create(self, request, *args, **kwargs):
        name = request.data.get('name')
        if not name:
            return Response({"error": "Application name is required"}, status=status.HTTP_400_BAD_REQUEST)
        app_type = request.data.get('app_type', 'OAuth 2.0 App')
        volume_tier = request.data.get('volume_tier', 'High (450k/day)')
        res = IntegrationService.generate_api_key(name, app_type, volume_tier, request.user)
        return Response(res, status=status.HTTP_201_CREATED)


class IntegrationLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = IntegrationLog.objects.all().order_by('-created_at')
    serializer_class = IntegrationLogSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        IntegrationService.seed_initial_integrations()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        service = self.request.query_params.get('service')
        status_param = self.request.query_params.get('status')

        if search:
            qs = qs.filter(Q(log_reference__icontains=search) | Q(service_name__icontains=search) | Q(event_name__icontains=search))
        if service:
            qs = qs.filter(service_name__icontains=service)
        if status_param:
            qs = qs.filter(status__iexact=status_param)
        return qs


class IntegrationStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        stats = IntegrationService.get_integration_stats()
        return Response(stats, status=status.HTTP_200_OK)


# ==========================================
# 2. SETTINGS VIEWSETS
# ==========================================

class StaffUserViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        search = request.query_params.get('search')
        department = request.query_params.get('department')
        role = request.query_params.get('role')
        users = SettingsService.get_staff_users(search=search, department=department, role=role)
        return Response(users, status=status.HTTP_200_OK)

    def create(self, request):
        email = request.data.get('email')
        name = request.data.get('name', '')
        role = request.data.get('role', 'Reviewer')
        department = request.data.get('department', 'Urban Planning')

        if not email or not name:
            return Response({"error": "Name and email are required"}, status=status.HTTP_400_BAD_REQUEST)

        inv = SettingsService.invite_user(email, name, role, department, request.user)
        return Response(UserInvitationSerializer(inv).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        u = SettingsService.toggle_user_status(pk, request.user)
        return Response({"id": str(u.id), "is_active": u.is_active, "message": f"User status toggled to {u.is_active}"}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny], url_path='accept-invite')
    def accept_invite(self, request):
        email = request.data.get('email')
        token = request.data.get('token')
        password = request.data.get('password')
        full_name = request.data.get('name') or request.data.get('full_name')

        if not email:
            return Response({"error": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)

        result = SettingsService.accept_invitation(
            email=email,
            token=token,
            password=password,
            full_name=full_name
        )

        if not result.get('success'):
            return Response({"error": result.get('message')}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class CustomRoleViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        roles = SettingsService.get_roles()
        return Response(CustomRoleSerializer(roles, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        name = request.data.get('name')
        description = request.data.get('description', '')
        if not name:
            return Response({"error": "Role name is required"}, status=status.HTTP_400_BAD_REQUEST)
        role = SettingsService.create_custom_role(name, description, request.user)
        return Response(CustomRoleSerializer(role).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get', 'post'], url_path='matrix')
    def matrix(self, request):
        if request.method == 'GET':
            data = SettingsService.get_roles_matrix()
            return Response(data, status=status.HTTP_200_OK)
        else:
            # Batch update permission matrix
            updates = request.data.get('updates', []) # list of {role_name, module, permission_name, is_granted}
            for up in updates:
                SettingsService.update_role_permission(
                    role_name=up['role_name'],
                    module=up['module'],
                    permission_name=up['permission_name'],
                    is_granted=up['is_granted'],
                    actor=request.user
                )
            return Response({"status": "success", "message": "Permissions updated successfully"}, status=status.HTTP_200_OK)


class ApprovalWorkflowViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        wfs = SettingsService.get_workflows()
        return Response(ApprovalWorkflowSerializer(wfs, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        name = request.data.get('name')
        description = request.data.get('description', '')
        steps = request.data.get('steps', [])
        if not name:
            return Response({"error": "Workflow name is required"}, status=status.HTTP_400_BAD_REQUEST)
        wf = SettingsService.create_workflow(name, steps, description, request.user)
        return Response(ApprovalWorkflowSerializer(wf).data, status=status.HTTP_201_CREATED)


class InspectionTemplateViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        tpls = SettingsService.get_templates()
        return Response(InspectionTemplateSerializer(tpls, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        name = request.data.get('name')
        department = request.data.get('department', 'Structural')
        items = request.data.get('items', [])
        if not name:
            return Response({"error": "Template name is required"}, status=status.HTTP_400_BAD_REQUEST)
        tpl = SettingsService.create_template(name, department, items, request.user)
        return Response(InspectionTemplateSerializer(tpl).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        SettingsService.delete_template(pk, request.user)
        return Response({"status": "deleted"}, status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='items')
    def add_item(self, request, pk=None):
        title = request.data.get('title')
        field_type = request.data.get('field_type', 'Pass/Fail Toggle')
        is_required = request.data.get('is_required', True)
        if not title:
            return Response({"error": "Item title is required"}, status=status.HTTP_400_BAD_REQUEST)
        it = SettingsService.add_checklist_item(pk, title, field_type, is_required)
        return Response(ChecklistItemSerializer(it).data, status=status.HTTP_201_CREATED)


class ComplianceStandardViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        standards = SettingsService.get_standards()
        return Response(ComplianceStandardSerializer(standards, many=True).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='update-thresholds')
    def update_thresholds(self, request):
        thresholds = request.data.get('thresholds', {})
        updated = SettingsService.update_standards(thresholds, request.user)
        return Response(ComplianceStandardSerializer(updated, many=True).data, status=status.HTTP_200_OK)


class StatutoryDocumentViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        docs = SettingsService.get_statutory_documents()
        return Response(StatutoryDocumentSerializer(docs, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        code = request.data.get('code')
        name = request.data.get('name')
        connected_features = request.data.get('connected_features', [])
        document_url = request.data.get('document_url')

        if not code or not name:
            return Response({"error": "Code and name are required"}, status=status.HTTP_400_BAD_REQUEST)
        doc = SettingsService.add_statutory_document(code, name, connected_features, document_url, request.user)
        return Response(StatutoryDocumentSerializer(doc).data, status=status.HTTP_201_CREATED)


class NotificationPreferenceViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        prefs = SettingsService.get_notification_preferences()
        return Response(prefs, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='update-preference')
    def update_preference(self, request):
        category = request.data.get('category')
        event_label = request.data.get('event_label')
        channel = request.data.get('channel')
        enabled = request.data.get('enabled')

        pref = SettingsService.update_notification_preference(category, event_label, channel, enabled, request.user)
        return Response(NotificationPreferenceCategorySerializer(pref).data, status=status.HTTP_200_OK)


class NotificationRoutingRuleViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        rules = SettingsService.get_routing_rules()
        return Response(NotificationRoutingRuleSerializer(rules, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        trigger_event = request.data.get('trigger_event')
        primary_recipient = request.data.get('primary_recipient')
        sla_timeline = request.data.get('sla_timeline', 'Within 15 mins')
        escalation_target = request.data.get('escalation_target')

        if not trigger_event or not primary_recipient or not escalation_target:
            return Response({"error": "Trigger event, primary recipient, and escalation target are required"}, status=status.HTTP_400_BAD_REQUEST)

        rule = SettingsService.add_routing_rule(trigger_event, primary_recipient, sla_timeline, escalation_target, request.user)
        return Response(NotificationRoutingRuleSerializer(rule).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        SettingsService.delete_routing_rule(pk, request.user)
        return Response({"status": "deleted"}, status=status.HTTP_204_NO_CONTENT)


class WebhookSubscriptionViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        hooks = SettingsService.get_webhooks()
        return Response(WebhookSubscriptionSerializer(hooks, many=True).data, status=status.HTTP_200_OK)

    def create(self, request):
        name = request.data.get('name')
        target_url = request.data.get('target_url')
        events = request.data.get('events', [])

        if not name or not target_url:
            return Response({"error": "Name and Target URL are required"}, status=status.HTTP_400_BAD_REQUEST)

        hook = SettingsService.create_webhook(name, target_url, events, request.user)
        return Response(WebhookSubscriptionSerializer(hook).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        SettingsService.delete_webhook(pk, request.user)
        return Response({"status": "deleted"}, status=status.HTTP_204_NO_CONTENT)
