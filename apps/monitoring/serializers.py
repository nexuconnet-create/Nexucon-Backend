from rest_framework import serializers
from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification

class DailySiteUpdateSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)

    class Meta:
        model = DailySiteUpdate
        fields = '__all__'
        read_only_fields = ('id', 'update_reference', 'created_at', 'updated_at')


class FieldObservationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)

    class Meta:
        model = FieldObservation
        fields = '__all__'
        read_only_fields = ('id', 'observation_reference', 'created_at', 'updated_at')


class SiteIssueSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)

    class Meta:
        model = SiteIssue
        fields = '__all__'
        read_only_fields = ('id', 'issue_reference', 'created_at', 'updated_at')


class ConstructionMilestoneSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)
    project_status = serializers.CharField(source='project.status', read_only=True)
    gate_evaluation = serializers.SerializerMethodField()

    class Meta:
        model = ConstructionMilestone
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_gate_evaluation(self, obj):
        try:
            from .services import MonitoringService
            return MonitoringService.evaluate_milestone_gates(obj)
        except Exception:
            return {'all_gates_passed': False, 'is_blocked': False, 'gates': [], 'blockers': []}


class SiteVerificationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)

    class Meta:
        model = SiteVerification
        fields = '__all__'
        read_only_fields = ('id', 'verification_reference', 'created_at', 'updated_at')
