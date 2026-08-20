from rest_framework import serializers
from .models import Inspection, Checklist, Finding, StopWorkOrder

class ChecklistSerializer(serializers.ModelSerializer):
    class Meta:
        model = Checklist
        fields = '__all__'


class FindingSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    inspection_reference = serializers.CharField(source='inspection.inspection_reference', read_only=True)

    class Meta:
        model = Finding
        fields = '__all__'
        read_only_fields = ('id', 'finding_reference', 'created_at', 'updated_at')


class StopWorkOrderSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)
    inspection_reference = serializers.CharField(source='inspection.inspection_reference', read_only=True, default=None)

    class Meta:
        model = StopWorkOrder
        fields = '__all__'
        read_only_fields = ('id', 'order_number', 'created_at', 'updated_at')


class InspectionSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)
    permit_number = serializers.CharField(source='permit.permit_number', read_only=True, default=None)
    findings = FindingSerializer(many=True, read_only=True)
    findings_count = serializers.SerializerMethodField()
    has_active_swo = serializers.SerializerMethodField()

    class Meta:
        model = Inspection
        fields = '__all__'
        read_only_fields = ('id', 'inspection_reference', 'created_at', 'updated_at')

    def get_findings_count(self, obj):
        return obj.findings.count()

    def get_has_active_swo(self, obj):
        return obj.stop_work_orders.filter(status='ACTIVE').exists()


class CreateInspectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Inspection
        fields = (
            'project', 'permit', 'inspection_type', 'priority',
            'scheduled_date', 'summary_notes', 'checklist_results'
        )
