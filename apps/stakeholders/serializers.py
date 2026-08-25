from rest_framework import serializers
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    Certification, TrainingRecord, MessageTranslation, MeetingActionItem
)

class DeveloperSerializer(serializers.ModelSerializer):
    class Meta:
        model = Developer
        fields = '__all__'
        read_only_fields = ('id', 'developer_id', 'created_at', 'updated_at')


class ContractorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contractor
        fields = '__all__'
        read_only_fields = ('id', 'contractor_id', 'created_at', 'updated_at')


class ConsultantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consultant
        fields = '__all__'
        read_only_fields = ('id', 'consultant_id', 'created_at', 'updated_at')


class InspectorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Inspector
        fields = '__all__'
        read_only_fields = ('id', 'inspector_id', 'created_at', 'updated_at')


class LicensedProfessionalSerializer(serializers.ModelSerializer):
    class Meta:
        model = LicensedProfessional
        fields = '__all__'
        read_only_fields = ('id', 'license_id', 'created_at')


class ProjectStakeholderTeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectStakeholderTeam
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class BlacklistRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlacklistRecord
        fields = '__all__'
        read_only_fields = ('id', 'blacklisted_at')


class MeetingActionItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeetingActionItem
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class StakeholderMeetingSerializer(serializers.ModelSerializer):
    action_items = MeetingActionItemSerializer(many=True, read_only=True)

    class Meta:
        model = StakeholderMeeting
        fields = '__all__'
        read_only_fields = ('id', 'meeting_reference', 'room_id', 'created_at')


class MessageTranslationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageTranslation
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class StakeholderMessageSerializer(serializers.ModelSerializer):
    translations = MessageTranslationSerializer(many=True, read_only=True)

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
