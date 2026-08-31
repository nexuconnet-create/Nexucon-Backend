from django.contrib import admin

# Register your models here.
from .models import ScanSession, ScanMetadata, ProcessingTask, Defect, ThermalAnomaly, HardwareAlert, BIMAlignmentResult, ScanFile, ProgressValidationResult, ScanPlan, Scanner, GnssTelemetry, ComplianceCertificate, ComplianceCheck

@admin.register(ScanSession)
class ScanSessionAdmin(admin.ModelAdmin):
    pass

@admin.register(ScanMetadata)
class ScanMetadataAdmin(admin.ModelAdmin):
    pass

@admin.register(ProcessingTask)
class ProcessingTaskAdmin(admin.ModelAdmin):
    pass

@admin.register(Defect)
class DefectAdmin(admin.ModelAdmin):
    pass

@admin.register(ThermalAnomaly)
class ThermalAnomalyAdmin(admin.ModelAdmin):
    pass

@admin.register(HardwareAlert)
class HardwareAlertAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMAlignmentResult)
class BIMAlignmentResultAdmin(admin.ModelAdmin):
    pass

@admin.register(ScanFile)
class ScanFileAdmin(admin.ModelAdmin):
    pass

@admin.register(ProgressValidationResult)
class ProgressValidationResultAdmin(admin.ModelAdmin):
    pass

@admin.register(ScanPlan)
class ScanPlanAdmin(admin.ModelAdmin):
    pass

@admin.register(Scanner)
class ScannerAdmin(admin.ModelAdmin):
    pass

@admin.register(GnssTelemetry)
class GnssTelemetryAdmin(admin.ModelAdmin):
    pass

@admin.register(ComplianceCertificate)
class ComplianceCertificateAdmin(admin.ModelAdmin):
    pass

@admin.register(ComplianceCheck)
class ComplianceCheckAdmin(admin.ModelAdmin):
    pass

