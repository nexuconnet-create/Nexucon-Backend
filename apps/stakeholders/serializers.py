from rest_framework import serializers
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    Certification, TrainingRecord
)

class DeveloperSerializer(serializers.ModelSerializer):
    class Meta:
        model = Developer
        fields = '__all__'

class ContractorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contractor
        fields = '__all__'

class ConsultantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consultant
        fields = '__all__'

class InspectorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Inspector
        fields = '__all__'

class LicensedProfessionalSerializer(serializers.ModelSerializer):
    class Meta:
        model = LicensedProfessional
        fields = '__all__'

class ProjectStakeholderTeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectStakeholderTeam
        fields = '__all__'

class BlacklistRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlacklistRecord
        fields = '__all__'

class StakeholderMeetingSerializer(serializers.ModelSerializer):
    class Meta:
        model = StakeholderMeeting
        fields = '__all__'
        read_only_fields = ('id', 'meeting_reference', 'room_id', 'created_at')

class StakeholderMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = StakeholderMessage
        fields = '__all__'
        read_only_fields = ('id', 'created_at')

class CertificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Certification
        fields = '__all__'

class TrainingRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingRecord
        fields = '__all__'
