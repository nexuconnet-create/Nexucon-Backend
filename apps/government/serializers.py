from rest_framework import serializers
from .models import Agency, Profile, Role

class AgencyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agency
        fields = (
            'id', 'name', 'code', 'description',
            'country', 'state_region', 'city', 'department_name',
            'primary_role', 'jurisdiction_level', 'project_scale_focus', 'collaboration_preference',
            'short_name', 'official_email', 'main_phone', 'physical_address', 'timezone', 'measurement_system'
        )
