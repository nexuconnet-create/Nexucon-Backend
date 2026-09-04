from rest_framework import serializers
from .models import ArchivedReport, QualityReport, ReportTemplate

class QualityReportSerializer(serializers.ModelSerializer):
    project_name = serializers.SerializerMethodField()
    session_id = serializers.SerializerMethodField()

    class Meta:
        model = QualityReport
        fields = ['id', 'scan', 'session_id', 'project_id', 'project_name', 'generated_at', 'status', 'report_type', 'summary', 'recommendations', 'report_url', 'defect_count', 'anomaly_count', 'mean_deviation', 'overall_ai_confidence']

    def get_project_name(self, obj):
        project = getattr(obj.scan, 'project', None)
        return project.name if project else None

    def get_session_id(self, obj):
        return str(obj.scan_id) if obj.scan_id else None


class ReportTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportTemplate
        fields = ['id', 'name', 'description', 'report_type', 'sort_order', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class ArchivedReportSerializer(serializers.ModelSerializer):
    """A generated statutory dossier archived exactly as produced (checksummed
    PDF bytes + real counts/verdicts from the tests it was rendered from)."""
    project_name = serializers.CharField(source='project.name', read_only=True)
    generated_by_name = serializers.SerializerMethodField()
    report_kind_display = serializers.CharField(source='get_report_kind_display', read_only=True)
    compliance_status_display = serializers.CharField(
        source='get_compliance_status_display', read_only=True)

    class Meta:
        model = ArchivedReport
        fields = [
            'id', 'project', 'project_name', 'report_kind', 'report_kind_display',
            'report_reference', 'title', 'file_size_bytes', 'sha256_checksum',
            'test_count', 'assessed_count', 'passed_count',
            'compliance_status', 'compliance_status_display',
            'generated_by', 'generated_by_name', 'created_at',
        ]

    def get_generated_by_name(self, obj):
        if not obj.generated_by:
            return ''
        full = getattr(obj.generated_by, 'get_full_name', None)
        if full and callable(full):
            return obj.generated_by.get_full_name() or obj.generated_by.username
        return getattr(obj.generated_by, 'username', '')
