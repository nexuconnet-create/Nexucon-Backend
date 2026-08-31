from django.contrib import admin
from .models import GeneratedReport, DepartmentPerformanceMetric, OfficerPerformanceRecord, RiskAssessmentAlert

@admin.register(GeneratedReport)
class GeneratedReportAdmin(admin.ModelAdmin):
    pass

@admin.register(DepartmentPerformanceMetric)
class DepartmentPerformanceMetricAdmin(admin.ModelAdmin):
    pass

@admin.register(OfficerPerformanceRecord)
class OfficerPerformanceRecordAdmin(admin.ModelAdmin):
    pass

@admin.register(RiskAssessmentAlert)
class RiskAssessmentAlertAdmin(admin.ModelAdmin):
    pass

