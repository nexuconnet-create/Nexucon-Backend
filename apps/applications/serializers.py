from rest_framework import serializers
from .models import Application

class ApplicationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    applicant_name = serializers.CharField(source='applicant.get_full_name', read_only=True)
    
    class Meta:
        model = Application
        fields = '__all__'
