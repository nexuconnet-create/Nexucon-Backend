from django.contrib import admin
from .models import TersusDevice, BIMIntegration, DocumentSystemIntegration, GovernmentAPIIntegration, APIKeyCredential, IntegrationLog, UserInvitation, CustomRole, RolePermission, ApprovalWorkflow, WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard, StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory, WebhookSubscription

@admin.register(TersusDevice)
class TersusDeviceAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMIntegration)
class BIMIntegrationAdmin(admin.ModelAdmin):
    pass

@admin.register(DocumentSystemIntegration)
class DocumentSystemIntegrationAdmin(admin.ModelAdmin):
    pass

@admin.register(GovernmentAPIIntegration)
class GovernmentAPIIntegrationAdmin(admin.ModelAdmin):
    pass

@admin.register(APIKeyCredential)
class APIKeyCredentialAdmin(admin.ModelAdmin):
    pass

@admin.register(IntegrationLog)
class IntegrationLogAdmin(admin.ModelAdmin):
    pass

@admin.register(UserInvitation)
class UserInvitationAdmin(admin.ModelAdmin):
    pass

@admin.register(CustomRole)
class CustomRoleAdmin(admin.ModelAdmin):
    pass

@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    pass

@admin.register(ApprovalWorkflow)
class ApprovalWorkflowAdmin(admin.ModelAdmin):
    pass

@admin.register(WorkflowStep)
class WorkflowStepAdmin(admin.ModelAdmin):
    pass

@admin.register(InspectionTemplate)
class InspectionTemplateAdmin(admin.ModelAdmin):
    pass

@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    pass

@admin.register(ComplianceStandard)
class ComplianceStandardAdmin(admin.ModelAdmin):
    pass

@admin.register(StatutoryDocument)
class StatutoryDocumentAdmin(admin.ModelAdmin):
    pass

@admin.register(NotificationRoutingRule)
class NotificationRoutingRuleAdmin(admin.ModelAdmin):
    pass

@admin.register(NotificationPreferenceCategory)
class NotificationPreferenceCategoryAdmin(admin.ModelAdmin):
    pass

@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    pass

