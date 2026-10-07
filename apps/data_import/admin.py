from django.contrib import admin

from .models import ImportBatch, ImportRecord


class ImportRecordInline(admin.TabularInline):
    model = ImportRecord
    extra = 0
    can_delete = False
    fields = ['row_number', 'row_end', 'record_type', 'validation_status',
              'error_message', 'target_model', 'target_id']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        # Records are written by the validate and commit passes. One added here
        # would sit in the batch's record list without having been parsed from
        # the file the batch's hash attests.
        return False


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ['batch_reference', 'file_name', 'import_type', 'record_type',
                    'inspector_name', 'import_status', 'record_count',
                    'valid_record_count', 'invalid_record_count', 'created_at']
    list_filter = ['import_status', 'import_type', 'record_type']
    search_fields = ['batch_reference', 'file_name', 'inspector__email',
                     'sha256_hash']
    readonly_fields = ['id', 'batch_reference', 'inspector', 'inspector_name',
                       'project', 'inspection', 'import_type', 'record_type',
                       'file_name', 'file_size_bytes', 'file_url',
                       'storage_name', 'sha256_hash', 'import_status',
                       'validation_errors', 'errors_truncated', 'record_count',
                       'valid_record_count', 'invalid_record_count',
                       'skipped_row_count', 'validated_at', 'imported_at',
                       'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    inlines = [ImportRecordInline]

    def has_add_permission(self, request):
        # A batch is created by the upload endpoint, which computes the hash
        # over the bytes it actually received. A batch typed in here would
        # carry a hash nobody computed.
        return False
