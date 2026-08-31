from django.contrib import admin
from .models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria

@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    pass

@admin.register(ApprovalDecision)
class ApprovalDecisionAdmin(admin.ModelAdmin):
    pass

@admin.register(TechnicalReviewCriteria)
class TechnicalReviewCriteriaAdmin(admin.ModelAdmin):
    pass

