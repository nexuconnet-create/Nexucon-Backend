"""
Import API serializers.

Request shapes are validated here and nowhere else; everything that decides
whether a *row* is acceptable belongs to the registry and the serializers the
registry names. Keeping the two apart is why an error on row 412 of a CSV
names row 412 rather than the request body.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import ImportBatch, ImportRecord
from .registry import KNOWN_RECORD_TYPES


class UploadRequestSerializer(serializers.Serializer):
    """`POST import/upload/` — multipart.

    ``import_type`` is optional and is treated as a *claim*: the bytes are what
    decide, and a claim that contradicts them is refused naming both. A caller
    that leaves it out is not asked to guess, and a caller that states it gets
    told when the file it picked is not the file it described.
    """

    file = serializers.FileField()
    project = serializers.UUIDField()
    inspection = serializers.UUIDField(required=False, allow_null=True)
    import_type = serializers.ChoiceField(
        choices=['CSV', 'JSON', 'PDF'], required=False, allow_blank=True)
    record_type = serializers.ChoiceField(
        choices=list(KNOWN_RECORD_TYPES), required=False, allow_blank=True)


class ImportRecordSerializer(serializers.ModelSerializer):
    row_label = serializers.CharField(read_only=True)

    class Meta:
        model = ImportRecord
        fields = [
            'id', 'row_number', 'row_end', 'row_label', 'record_type',
            'raw_data', 'record_data', 'validation_status', 'error_message',
            'target_model', 'target_id', 'created_at',
        ]
        read_only_fields = fields


class ImportBatchSerializer(serializers.ModelSerializer):
    inspector_name = serializers.CharField(read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    inspection_reference = serializers.CharField(
        source='inspection.inspection_reference', read_only=True, default=None)
    can_commit = serializers.BooleanField(read_only=True)
    error_count = serializers.SerializerMethodField()
    deduplicated = serializers.SerializerMethodField()

    class Meta:
        model = ImportBatch
        fields = [
            'id', 'batch_reference', 'inspector_name', 'project', 'project_name',
            'inspection', 'inspection_reference', 'import_type', 'record_type',
            'file_name', 'file_size_bytes', 'file_url', 'sha256_hash',
            'import_status', 'record_count', 'valid_record_count',
            'invalid_record_count', 'skipped_row_count', 'error_count',
            'errors_truncated', 'can_commit', 'deduplicated',
            'validated_at', 'imported_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField())
    def get_error_count(self, obj):
        return len(obj.validation_errors or [])

    @extend_schema_field(serializers.BooleanField())
    def get_deduplicated(self, obj):
        # Set by the upload view on the response only. Defaults to False so the
        # field is always present and a client never has to distinguish
        # "absent" from "not a duplicate".
        return bool(self.context.get('deduplicated', False))


class ImportBatchStatusSerializer(ImportBatchSerializer):
    """The status endpoint's shape: the batch plus its (capped) error list."""

    errors = serializers.SerializerMethodField()

    class Meta(ImportBatchSerializer.Meta):
        fields = ImportBatchSerializer.Meta.fields + ['errors']
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_errors(self, obj):
        cap = self.context.get('error_limit') or 200
        return list(obj.validation_errors or [])[:cap]
