from rest_framework import serializers
from .models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria

class TechnicalReviewCriteriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = TechnicalReviewCriteria
        fields = '__all__'
        read_only_fields = ('id',)


class ApprovalDecisionSerializer(serializers.ModelSerializer):
    request_reference = serializers.CharField(source='approval_request.request_reference', read_only=True)
    request_title = serializers.CharField(source='approval_request.title', read_only=True)
    project_name = serializers.CharField(source='approval_request.project.name', read_only=True)

    class Meta:
        model = ApprovalDecision
        fields = '__all__'
        read_only_fields = ('id', 'decision_reference', 'timestamp')


class ApprovalRequestSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    decisions = ApprovalDecisionSerializer(many=True, read_only=True)
    criteria = TechnicalReviewCriteriaSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = '__all__'
        read_only_fields = ('id', 'request_reference', 'doa_level_required', 'created_at', 'updated_at')
