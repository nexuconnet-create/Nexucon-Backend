from django.contrib import admin

# Register your models here.
from .models import ReportTemplate, QualityReport

@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    pass

@admin.register(QualityReport)
class QualityReportAdmin(admin.ModelAdmin):
    pass

