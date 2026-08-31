from django.contrib import admin
from .models import Checklist, Inspection, Finding, StopWorkOrder, Issue, IssueComment, NonConformanceReport, CorrectiveAction

@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    pass

@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    pass

@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    pass

@admin.register(StopWorkOrder)
class StopWorkOrderAdmin(admin.ModelAdmin):
    pass

@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    pass

@admin.register(IssueComment)
class IssueCommentAdmin(admin.ModelAdmin):
    pass

@admin.register(NonConformanceReport)
class NonConformanceReportAdmin(admin.ModelAdmin):
    pass

@admin.register(CorrectiveAction)
class CorrectiveActionAdmin(admin.ModelAdmin):
    pass

