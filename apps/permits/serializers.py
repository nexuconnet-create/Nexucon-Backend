from rest_framework import serializers
import datetime
from .models import Permit

class PermitSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    project_location = serializers.CharField(source='project.lga', read_only=True)
    application_reference = serializers.CharField(source='application.application_reference', read_only=True)
    application_type = serializers.CharField(source='application.application_type', read_only=True)
    applicant_name = serializers.SerializerMethodField()
    days_until_expiry = serializers.SerializerMethodField()
    is_expiring_soon = serializers.SerializerMethodField()

    class Meta:
        model = Permit
        fields = '__all__'
        read_only_fields = ('id', 'permit_number', 'created_at', 'updated_at')

    def get_applicant_name(self, obj):
        if obj.application and obj.application.applicant:
            name = obj.application.applicant.get_full_name()
            return name if name and name.strip() else obj.application.applicant.email
        return "Unknown Applicant"

    def get_days_until_expiry(self, obj):
        if obj.expiry_date:
            delta = obj.expiry_date - datetime.date.today()
            return delta.days
        return None

    def get_is_expiring_soon(self, obj):
        if obj.expiry_date and obj.status == 'ACTIVE':
            days = (obj.expiry_date - datetime.date.today()).days
            return 0 <= days <= 30
        return False
