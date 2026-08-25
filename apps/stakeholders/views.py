from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    Certification, TrainingRecord, MeetingActionItem, MessageTranslation
)
from .serializers import (
    DeveloperSerializer, ContractorSerializer, ConsultantSerializer,
    InspectorSerializer, LicensedProfessionalSerializer, ProjectStakeholderTeamSerializer,
    BlacklistRecordSerializer, StakeholderMeetingSerializer, StakeholderMessageSerializer,
    CertificationSerializer, TrainingRecordSerializer, MeetingActionItemSerializer, MessageTranslationSerializer
)
from .services import StakeholderService
from .translation import TranslationService

class DeveloperViewSet(viewsets.ModelViewSet):
    queryset = Developer.objects.all().order_by('-created_at')
    serializer_class = DeveloperSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(developer_id__icontains=search) | Q(hq_location__icontains=search))
        return qs

    def perform_create(self, serializer):
        dev = serializer.save()
        StakeholderService.log_audit(
            user=self.request.user,
            action="STAKEHOLDER_DEVELOPER_CREATED",
            resource_id=dev.id,
            new_state={"name": dev.name, "developer_id": dev.developer_id}
        )


class ContractorViewSet(viewsets.ModelViewSet):
    queryset = Contractor.objects.all().order_by('-created_at')
    serializer_class = ContractorSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(contractor_id__icontains=search) | Q(contractor_type__icontains=search))
        return qs

    def perform_create(self, serializer):
        con = serializer.save()
        StakeholderService.log_audit(
            user=self.request.user,
            action="STAKEHOLDER_CONTRACTOR_CREATED",
            resource_id=con.id,
            new_state={"name": con.name, "contractor_id": con.contractor_id}
        )

    @action(detail=True, methods=['post'], url_path='validate-license')
    def validate_license(self, request, pk=None):
        contractor = self.get_object()
        res = StakeholderService.validate_license_via_api(contractor.license_number or contractor.contractor_id, 'Regulatory Registry')
        return Response(res, status=status.HTTP_200_OK)


class ConsultantViewSet(viewsets.ModelViewSet):
    queryset = Consultant.objects.all().order_by('-created_at')
    serializer_class = ConsultantSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        specialty = self.request.query_params.get('specialty')
        if specialty and specialty.upper() != 'ALL':
            qs = qs.filter(specialty__icontains=specialty)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(consultant_id__icontains=search) | Q(specialty__icontains=search))
        return qs

    def perform_create(self, serializer):
        cns = serializer.save()
        StakeholderService.log_audit(
            user=self.request.user,
            action="STAKEHOLDER_CONSULTANT_CREATED",
            resource_id=cns.id,
            new_state={"name": cns.name, "specialty": cns.specialty}
        )


class InspectorViewSet(viewsets.ModelViewSet):
    queryset = Inspector.objects.all().order_by('-created_at')
    serializer_class = InspectorSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        zone = self.request.query_params.get('zone')
        search = self.request.query_params.get('search')
        if zone and zone.upper() != 'ALL':
            qs = qs.filter(assigned_zone__icontains=zone)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(inspector_id__icontains=search) | Q(role_title__icontains=search))
        return qs

    def perform_create(self, serializer):
        ins = serializer.save()
        StakeholderService.log_audit(
            user=self.request.user,
            action="STAKEHOLDER_INSPECTOR_CREATED",
            resource_id=ins.id,
            new_state={"name": ins.name, "inspector_id": ins.inspector_id}
        )

    @action(detail=True, methods=['post'], url_path='reassign-zone')
    def reassign_zone(self, request, pk=None):
        new_zone = request.data.get('zone')
        if not new_zone:
            return Response({"error": "New zone is required"}, status=status.HTTP_400_BAD_REQUEST)
        inspector = self.get_object()
        updated = StakeholderService.assign_inspector_zone(inspector.inspector_id, new_zone, request.user)
        return Response(InspectorSerializer(updated).data, status=status.HTTP_200_OK)


class LicensedProfessionalViewSet(viewsets.ModelViewSet):
    queryset = LicensedProfessional.objects.all().order_by('-created_at')
    serializer_class = LicensedProfessionalSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        authority = self.request.query_params.get('authority')
        search = self.request.query_params.get('search')
        if authority and authority.upper() != 'ALL':
            qs = qs.filter(license_authority__iexact=authority)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(license_id__icontains=search) | Q(firm_name__icontains=search) | Q(role_title__icontains=search))
        return qs

    def perform_create(self, serializer):
        prof = serializer.save()
        StakeholderService.log_audit(
            user=self.request.user,
            action="STAKEHOLDER_PROFESSIONAL_REGISTERED",
            resource_id=prof.id,
            new_state={"name": prof.name, "authority": prof.license_authority}
        )

    @action(detail=True, methods=['post'], url_path='verify-license')
    def verify_license(self, request, pk=None):
        prof = StakeholderService.verify_professional_license(pk, request.user)
        return Response(LicensedProfessionalSerializer(prof).data, status=status.HTTP_200_OK)


class ProjectStakeholderTeamViewSet(viewsets.ModelViewSet):
    queryset = ProjectStakeholderTeam.objects.all().order_by('-created_at')
    serializer_class = ProjectStakeholderTeamSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(project_name__icontains=search) | Q(project_reference__icontains=search) | Q(location__icontains=search))
        return qs

    @action(detail=True, methods=['post'], url_path='add-member')
    def add_member(self, request, pk=None):
        role_key = request.data.get('role_key')
        member_data = request.data.get('member_data')
        if not role_key or not member_data:
            return Response({"error": "role_key and member_data are required"}, status=status.HTTP_400_BAD_REQUEST)
        team = StakeholderService.add_team_member(pk, role_key, member_data, request.user)
        return Response(ProjectStakeholderTeamSerializer(team).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='remove-member')
    def remove_member(self, request, pk=None):
        role_key = request.data.get('role_key')
        if not role_key:
            return Response({"error": "role_key is required"}, status=status.HTTP_400_BAD_REQUEST)
        team = StakeholderService.remove_team_member(pk, role_key, request.user)
        return Response(ProjectStakeholderTeamSerializer(team).data, status=status.HTTP_200_OK)


class BlacklistRecordViewSet(viewsets.ModelViewSet):
    queryset = BlacklistRecord.objects.all().order_by('-blacklisted_at')
    serializer_class = BlacklistRecordSerializer
    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=['post'], url_path='toggle')
    def toggle(self, request):
        data = request.data
        rec = StakeholderService.toggle_blacklist(
            entity_type=data.get('entity_type', 'Contractor'),
            entity_id=data.get('entity_id', ''),
            entity_name=data.get('entity_name', ''),
            reason=data.get('reason', 'Administrative decision'),
            status=data.get('status', 'Blacklisted'),
            user=request.user
        )
        return Response(BlacklistRecordSerializer(rec).data, status=status.HTTP_200_OK)


class StakeholderMeetingViewSet(viewsets.ModelViewSet):
    queryset = StakeholderMeeting.objects.all().order_by('-date', '-created_at')
    serializer_class = StakeholderMeetingSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        return super().get_queryset()

    def perform_create(self, serializer):
        meeting = StakeholderService.schedule_meeting(self.request.data, self.request.user)
        serializer.instance = meeting

    @action(detail=True, methods=['post'], url_path='start')
    def start_meeting(self, request, pk=None):
        res = StakeholderService.start_meeting(pk, request.user)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='add-action-item')
    def add_action_item(self, request, pk=None):
        title = request.data.get('title')
        assignee = request.data.get('assignee_name', 'Project Lead')
        due_date = request.data.get('due_date', 'Within 5 Business Days')
        if not title:
            return Response({"error": "title is required"}, status=status.HTTP_400_BAD_REQUEST)
        item = StakeholderService.add_meeting_action_item(pk, title, assignee, due_date, request.user)
        return Response(MeetingActionItemSerializer(item).data, status=status.HTTP_201_CREATED)


class StakeholderMessageViewSet(viewsets.ModelViewSet):
    queryset = StakeholderMessage.objects.all().order_by('created_at')
    serializer_class = StakeholderMessageSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        channel = self.request.query_params.get('channel')
        if channel and channel.upper() != 'ALL':
            qs = qs.filter(channel_name__iexact=channel)
        return qs

    def perform_create(self, serializer):
        msg = StakeholderService.send_message(self.request.data, self.request.user)
        serializer.instance = msg

    @action(detail=True, methods=['post'], url_path='translate')
    def translate(self, request, pk=None):
        target_lang = request.data.get('target_language', 'yo')
        try:
            translation_result = TranslationService.translate_message(pk, target_lang, request.user)
            return Response(translation_result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class CertificationViewSet(viewsets.ModelViewSet):
    queryset = Certification.objects.all().order_by('-created_at')
    serializer_class = CertificationSerializer
    permission_classes = [permissions.AllowAny]

class TrainingRecordViewSet(viewsets.ModelViewSet):
    queryset = TrainingRecord.objects.all().order_by('-created_at')
    serializer_class = TrainingRecordSerializer
    permission_classes = [permissions.AllowAny]


class StakeholderStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.AllowAny]

    def list(self, request):
        stats = StakeholderService.get_stakeholder_stats()
        return Response(stats, status=status.HTTP_200_OK)
