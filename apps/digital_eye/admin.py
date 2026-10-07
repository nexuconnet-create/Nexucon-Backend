from django.contrib import admin
from .models import (
    BIMModelGeometry,
    BIMElementMapping,
    SensorDataFile,
    FieldDevice,
    PUNDITTest,
    GPRSurvey,
    GnssSurvey,
    LiveStream,
)


@admin.register(BIMModelGeometry)
class BIMModelGeometryAdmin(admin.ModelAdmin):
    list_display = ('project', 'source_file', 'element_count', 'translated_from_rvt', 'created_by', 'updated_at')
    list_filter = ('translated_from_rvt', 'updated_at')
    search_fields = ('source_file', 'project__name', 'project__reference_number')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(BIMElementMapping)
class BIMElementMappingAdmin(admin.ModelAdmin):
    list_display = ('element_id', 'element_name', 'element_type', 'level', 'project', 'bim_guid', 'source')
    list_filter = ('element_type', 'source', 'level')
    search_fields = ('element_id', 'element_name', 'bim_guid', 'project__name')


@admin.register(SensorDataFile)
class SensorDataFileAdmin(admin.ModelAdmin):
    list_display = ('file_name', 'project', 'file_type', 'file_size_bytes', 'uploaded_by', 'created_at')
    list_filter = ('file_type', 'created_at')
    search_fields = ('file_name', 'project__name', 'sha256_checksum')
    readonly_fields = ('sha256_checksum', 'created_at')


@admin.register(FieldDevice)
class FieldDeviceAdmin(admin.ModelAdmin):
    list_display = ('device_reference', 'device_id', 'name', 'device_type', 'model', 'status', 'is_active', 'assigned_project')
    list_filter = ('device_type', 'status', 'is_active')
    search_fields = ('device_reference', 'device_id', 'name', 'model', 'assigned_project__name')


@admin.register(PUNDITTest)
class PUNDITTestAdmin(admin.ModelAdmin):
    list_display = ('test_reference', 'project', 'structural_element', 'surface_condition', 'test_date')
    search_fields = ('test_reference', 'structural_element', 'project__name')
