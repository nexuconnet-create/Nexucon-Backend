from rest_framework import serializers
from .models import (
    GeneratedReport, DepartmentPerformanceMetric,
    OfficerPerformanceRecord, RiskAssessmentAlert
)

class GeneratedReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneratedReport
        fields = '__all__'
        read_only_fields = ('id', 'report_reference', 'status', 'file_url', 'file_size', 'generated_by_name', 'created_at')


class DepartmentPerformanceMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepartmentPerformanceMetric
        fields = '__all__'
        read_only_fields = ('id', 'updated_at')


class OfficerPerformanceRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = OfficerPerformanceRecord
        fields = '__all__'
        read_only_fields = ('id',)


class RiskAssessmentAlertSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = RiskAssessmentAlert
        fields = '__all__'
        read_only_fields = ('id', 'created_at')
