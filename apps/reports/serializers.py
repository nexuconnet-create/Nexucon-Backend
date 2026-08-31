from rest_framework import serializers
from .models import QualityReport, ReportTemplate

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
