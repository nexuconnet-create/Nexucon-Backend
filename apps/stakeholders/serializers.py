from rest_framework import serializers
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    Certification, TrainingRecord, MessageTranslation, MeetingActionItem,
    BuildingStageInspection, ProjectTimelineMilestone, StatutoryFinancialTransaction
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
    email = serializers.SerializerMethodField()
    invite_code = serializers.SerializerMethodField()
    temporary_password = serializers.SerializerMethodField()
    invitation_status = serializers.SerializerMethodField()

    class Meta:
        model = Inspector
        fields = '__all__'
        read_only_fields = ('id', 'inspector_id', 'created_at', 'updated_at')

    def _get_target_email(self, obj):
        if hasattr(obj, 'user') and obj.user and obj.user.email:
            return obj.user.email
        if hasattr(obj, 'email') and obj.email:
            return obj.email
        # Lookup invitation by inspector name
        from apps.settings.models import UserInvitation
        inv = UserInvitation.objects.filter(name__iexact=obj.name).first()
        if inv:
            return inv.email
        # Try matching last name
        last_word = obj.name.strip().split()[-1] if obj.name else ''
        if last_word and len(last_word) > 2:
            inv = UserInvitation.objects.filter(name__icontains=last_word).first()
            if inv:
                return inv.email
        return None

    def get_email(self, obj):
        return self._get_target_email(obj)

    def get_invite_code(self, obj):
        target_email = self._get_target_email(obj)
        if target_email:
            from apps.settings.models import UserInvitation
            inv = UserInvitation.objects.filter(email__iexact=target_email).first()
            if inv:
                return inv.invite_code
        return None

    def get_temporary_password(self, obj):
        target_email = self._get_target_email(obj)
        if target_email:
            from apps.settings.models import UserInvitation
            inv = UserInvitation.objects.filter(email__iexact=target_email).first()
            if inv and inv.status == 'Pending':
                return inv.temporary_password
        return None

    def get_invitation_status(self, obj):
        target_email = self._get_target_email(obj)
        if target_email:
            from apps.settings.models import UserInvitation
            inv = UserInvitation.objects.filter(email__iexact=target_email).first()
            if inv:
                return inv.status
        return 'Accepted'


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


class BuildingStageInspectionSerializer(serializers.ModelSerializer):
    inspector_details = InspectorSerializer(source='assigned_inspector', read_only=True)

    class Meta:
        model = BuildingStageInspection
        fields = '__all__'
        read_only_fields = ('id', 'stage_id', 'created_at')


class ProjectTimelineMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTimelineMilestone
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class StatutoryFinancialTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StatutoryFinancialTransaction
        fields = '__all__'
        read_only_fields = ('id', 'created_at')

