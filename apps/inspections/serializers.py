from rest_framework import serializers
from .models import Inspection, Checklist, Finding, StopWorkOrder
from apps.permits.models import Permit

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
    scheduled_date = serializers.DateTimeField(required=False, allow_null=True)
    permit = serializers.PrimaryKeyRelatedField(queryset=Permit.objects.all(), required=False, allow_null=True)

    class Meta:
        model = Inspection
        fields = (
            'project', 'permit', 'inspection_type', 'priority',
            'scheduled_date', 'summary_notes', 'checklist_results'
        )

    def validate_scheduled_date(self, value):
        if not value or value == '':
            return None
        return value


from .models import Issue, IssueComment, NonConformanceReport, CorrectiveAction
from django.contrib.auth import get_user_model
User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name']


class IssueCommentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = IssueComment
        fields = ['id', 'issue', 'user', 'text', 'created_at']
        read_only_fields = ['id', 'created_at', 'issue']


class IssueSerializer(serializers.ModelSerializer):
    assignee = UserSerializer(read_only=True)
    assignee_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source='assignee', write_only=True, required=False, allow_null=True
    )
    created_by = UserSerializer(read_only=True)
    comments = IssueCommentSerializer(many=True, read_only=True)

    class Meta:
        model = Issue
        fields = [
            'id', 'title', 'description', 'status', 'priority', 
            'project', 'session', 'assignee', 'assignee_id', 'created_by', 
            'created_at', 'updated_at', 'comments'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']


class CorrectiveActionSerializer(serializers.ModelSerializer):
    assignee = UserSerializer(read_only=True)
    assignee_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source='assignee', write_only=True, required=False, allow_null=True
    )

    class Meta:
        from .models import CorrectiveAction
        model = CorrectiveAction
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'ncr']


class NonConformanceReportSerializer(serializers.ModelSerializer):
    corrective_actions = CorrectiveActionSerializer(many=True, read_only=True)

    class Meta:
        from .models import NonConformanceReport
        model = NonConformanceReport
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'ncr_number']
