from rest_framework import serializers
from .models import Application

class ApplicationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)
    applicant_name = serializers.SerializerMethodField()
    applicant_email = serializers.EmailField(source='applicant.email', read_only=True)
    permit_number = serializers.CharField(source='permit.permit_number', read_only=True, default=None)
    
    class Meta:
        model = Application
        fields = '__all__'
        read_only_fields = ('id', 'application_reference', 'created_at', 'updated_at')

    def get_applicant_name(self, obj):
        if obj.applicant:
            name = obj.applicant.get_full_name()
            return name if name and name.strip() else obj.applicant.email
        return "Unknown Applicant"

class ApplicationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Application
        fields = (
            'project', 'title', 'application_type', 'jurisdiction',
            'priority', 'fee_amount', 'review_deadline', 'required_action',
            'review_items', 'attached_documents'
        )
