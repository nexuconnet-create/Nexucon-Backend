from rest_framework import serializers
from .models import (
    NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement,
    ComplianceReview, ComplianceCertificate, EscalationRule
)

class CorrectiveActionPlanSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    ncr_reference = serializers.CharField(source='ncr.ncr_reference', read_only=True)

    class Meta:
        model = CorrectiveActionPlan
        fields = '__all__'
        read_only_fields = ('id', 'capa_reference', 'created_at')


class NonConformanceReportSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    capas = CorrectiveActionPlanSerializer(many=True, read_only=True)
    days_open = serializers.ReadOnlyField()
    linked_capa_ref = serializers.SerializerMethodField()
    escalation_action_text = serializers.SerializerMethodField()

    class Meta:
        model = NonConformanceReport
        fields = '__all__'
        read_only_fields = ('id', 'ncr_reference', 'date_logged', 'created_at', 'updated_at')

    def get_linked_capa_ref(self, obj):
        first_capa = obj.capas.first()
        return first_capa.capa_reference if first_capa else 'None'

    def get_escalation_action_text(self, obj):
        if obj.status == 'Closed':
            return 'Resolved'
        actions = {
            1: "Reminder Sent (Auto)",
            2: "Warning Letter (Auto)",
            3: "Escalate to Sr. Officer",
            4: "Escalate to Director",
            5: "Initiate Legal Proceedings"
        }
        return actions.get(obj.escalation_level, 'Standard Monitoring')


class RegulatoryRequirementSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = RegulatoryRequirement
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class ComplianceReviewSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = ComplianceReview
        fields = '__all__'
        read_only_fields = ('id', 'review_reference', 'created_at')


class EscalationRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = EscalationRule
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class ComplianceCertificateSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = ComplianceCertificate
        fields = '__all__'
        read_only_fields = ('id', 'certificate_reference', 'created_at')
