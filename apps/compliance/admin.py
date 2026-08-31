from django.contrib import admin
from .models import NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement, ComplianceReview, ComplianceCertificate

@admin.register(NonConformanceReport)
class NonConformanceReportAdmin(admin.ModelAdmin):
    pass

@admin.register(CorrectiveActionPlan)
class CorrectiveActionPlanAdmin(admin.ModelAdmin):
    pass

@admin.register(RegulatoryRequirement)
class RegulatoryRequirementAdmin(admin.ModelAdmin):
    pass

@admin.register(ComplianceReview)
class ComplianceReviewAdmin(admin.ModelAdmin):
    pass

@admin.register(ComplianceCertificate)
class ComplianceCertificateAdmin(admin.ModelAdmin):
    pass

