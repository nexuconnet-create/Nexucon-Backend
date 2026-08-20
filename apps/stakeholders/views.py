from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    Certification, TrainingRecord
)
from .serializers import (
    DeveloperSerializer, ContractorSerializer, ConsultantSerializer,
    InspectorSerializer, LicensedProfessionalSerializer, ProjectStakeholderTeamSerializer,
    BlacklistRecordSerializer, StakeholderMeetingSerializer, StakeholderMessageSerializer,
    CertificationSerializer, TrainingRecordSerializer
)
from .services import StakeholderService

class DeveloperViewSet(viewsets.ModelViewSet):
    queryset = Developer.objects.all().order_by('-created_at')
    serializer_class = DeveloperSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(developer_id__icontains=search) | Q(hq_location__icontains=search))
        return qs


class ContractorViewSet(viewsets.ModelViewSet):
    queryset = Contractor.objects.all().order_by('-created_at')
    serializer_class = ContractorSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(contractor_id__icontains=search) | Q(contractor_type__icontains=search))
        return qs

    @action(detail=True, methods=['post'], url_path='validate-license')
    def validate_license(self, request, pk=None):
        contractor = self.get_object()
        res = StakeholderService.validate_license_via_api(contractor.license_number or contractor.contractor_id, 'Regulatory Registry')
        return Response(res, status=status.HTTP_200_OK)


class ConsultantViewSet(viewsets.ModelViewSet):
    queryset = Consultant.objects.all().order_by('-created_at')
    serializer_class = ConsultantSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(consultant_id__icontains=search) | Q(specialty__icontains=search))
        return qs


class InspectorViewSet(viewsets.ModelViewSet):
    queryset = Inspector.objects.all().order_by('-created_at')
    serializer_class = InspectorSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        zone = self.request.query_params.get('zone')
        search = self.request.query_params.get('search')
        if zone:
            qs = qs.filter(assigned_zone__icontains=zone)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(inspector_id__icontains=search) | Q(role_title__icontains=search))
        return qs

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
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(license_id__icontains=search) | Q(firm_name__icontains=search) | Q(role_title__icontains=search))
        return qs


class ProjectStakeholderTeamViewSet(viewsets.ModelViewSet):
    queryset = ProjectStakeholderTeam.objects.all().order_by('-created_at')
    serializer_class = ProjectStakeholderTeamSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        qs = super().get_queryset()
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(project_name__icontains=search) | Q(project_reference__icontains=search) | Q(location__icontains=search))
        return qs


class BlacklistRecordViewSet(viewsets.ModelViewSet):
    queryset = BlacklistRecord.objects.all().order_by('-blacklisted_at')
    serializer_class = BlacklistRecordSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        StakeholderService.seed_initial_stakeholders()
        return super().get_queryset()

    @action(detail=False, methods=['post'], url_path='toggle')
    def toggle(self, request):
        entity_type = request.data.get('entity_type', 'Contractor')
        entity_id = request.data.get('entity_id')
        entity_name = request.data.get('entity_name', 'Unknown Entity')
        reason = request.data.get('reason', 'Regulatory compliance infraction')
        status_val = request.data.get('status', 'Blacklisted')

        rec = StakeholderService.toggle_blacklist(entity_type, entity_id, entity_name, reason, status_val, request.user)
        return Response(BlacklistRecordSerializer(rec).data, status=status.HTTP_200_OK)


class StakeholderMeetingViewSet(viewsets.ModelViewSet):
    """
    Meeting Scheduling & Live Call Room Dispatching.
    NOTE: Only the Agency Head or Director General can schedule official meetings.
    """
    queryset = StakeholderMeeting.objects.all().order_by('-created_at')
    serializer_class = StakeholderMeetingSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

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


class StakeholderMessageViewSet(viewsets.ModelViewSet):
    queryset = StakeholderMessage.objects.all().order_by('created_at')
    serializer_class = StakeholderMessageSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

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


class CertificationViewSet(viewsets.ModelViewSet):
    queryset = Certification.objects.all().order_by('-created_at')
    serializer_class = CertificationSerializer

class TrainingRecordViewSet(viewsets.ModelViewSet):
    queryset = TrainingRecord.objects.all().order_by('-created_at')
    serializer_class = TrainingRecordSerializer


class StakeholderStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        stats = StakeholderService.get_stakeholder_stats()
        return Response(stats, status=status.HTTP_200_OK)
