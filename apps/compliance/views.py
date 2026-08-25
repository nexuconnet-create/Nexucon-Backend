from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from django.utils import timezone
from .models import (
    NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement,
    ComplianceReview, ComplianceCertificate, EscalationRule
)
from .serializers import (
    NonConformanceReportSerializer, CorrectiveActionPlanSerializer,
    RegulatoryRequirementSerializer, ComplianceReviewSerializer,
    ComplianceCertificateSerializer, EscalationRuleSerializer
)
from .services import ComplianceService

class NonConformanceReportViewSet(viewsets.ModelViewSet):
    queryset = NonConformanceReport.objects.all().select_related('project')
    serializer_class = NonConformanceReportSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        severity = self.request.query_params.get('severity')
        category = self.request.query_params.get('category')
        status_val = self.request.query_params.get('status')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if severity and severity.lower() != 'all':
            qs = qs.filter(severity__iexact=severity)
        if category and category.lower() != 'all':
            qs = qs.filter(category__iexact=category)
        if status_val and status_val.lower() != 'all':
            qs = qs.filter(status__iexact=status_val)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(ncr_reference__icontains=search) |
                Q(reported_by_name__icontains=search) |
                Q(assignee_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        ncr = ComplianceService.create_ncr(self.request.data, self.request.user)
        serializer.instance = ncr

    @action(detail=True, methods=['post'], url_path='escalate')
    def escalate(self, request, pk=None):
        ncr = self.get_object()
        target_level = request.data.get('escalation_level')
        escalated = ComplianceService.escalate_ncr(ncr, request.user, target_level)
        return Response(NonConformanceReportSerializer(escalated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='close')
    def close_ncr(self, request, pk=None):
        ncr = self.get_object()
        resolution_notes = request.data.get('resolution_notes', '')
        closed = ComplianceService.close_ncr(ncr, resolution_notes, request.user)
        return Response(NonConformanceReportSerializer(closed).data, status=status.HTTP_200_OK)


class CorrectiveActionPlanViewSet(viewsets.ModelViewSet):
    queryset = CorrectiveActionPlan.objects.all().select_related('project', 'ncr')
    serializer_class = CorrectiveActionPlanSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        priority = self.request.query_params.get('priority')
        status_val = self.request.query_params.get('status')
        ncr_id = self.request.query_params.get('ncr')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if priority and priority.lower() != 'all':
            qs = qs.filter(priority__iexact=priority)
        if status_val and status_val.lower() != 'all':
            qs = qs.filter(status__iexact=status_val)
        if ncr_id:
            qs = qs.filter(ncr_id=ncr_id)
        return qs

    def perform_create(self, serializer):
        capa = ComplianceService.create_capa(self.request.data, self.request.user)
        serializer.instance = capa

    @action(detail=True, methods=['post'], url_path='transition')
    def transition(self, request, pk=None):
        capa = self.get_object()
        new_status = request.data.get('status')
        notes = request.data.get('verification_notes')
        if not new_status:
            return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)
        updated = ComplianceService.transition_capa(capa, new_status, notes, request.user)
        return Response(CorrectiveActionPlanSerializer(updated).data, status=status.HTTP_200_OK)


class RegulatoryRequirementViewSet(viewsets.ModelViewSet):
    queryset = RegulatoryRequirement.objects.all().select_related('project')
    serializer_class = RegulatoryRequirementSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        ComplianceService.seed_default_requirements()
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        status_val = self.request.query_params.get('status')
        search = self.request.query_params.get('search')

        if category and category.lower() != 'all':
            qs = qs.filter(category__iexact=category)
        if status_val and status_val.lower() != 'all':
            qs = qs.filter(status__iexact=status_val)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(requirement_reference__icontains=search) |
                Q(authority__icontains=search)
            )
        return qs

    @action(detail=True, methods=['post'], url_path='update-status')
    def update_status(self, request, pk=None):
        req_obj = self.get_object()
        new_status = request.data.get('status', 'Compliant')
        req_obj.status = new_status
        req_obj.last_checked = timezone.localdate()
        req_obj.save()
        return Response(RegulatoryRequirementSerializer(req_obj).data, status=status.HTTP_200_OK)


class ComplianceReviewViewSet(viewsets.ModelViewSet):
    queryset = ComplianceReview.objects.all().select_related('project')
    serializer_class = ComplianceReviewSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        stage = self.request.query_params.get('stage')
        review_type = self.request.query_params.get('type')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if stage and stage.lower() != 'all':
            qs = qs.filter(stage__iexact=stage)
        if review_type and review_type.lower() != 'all':
            qs = qs.filter(review_type__iexact=review_type)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(review_reference__icontains=search) |
                Q(auditor_name__icontains=search)
            )
        return qs

    @action(detail=True, methods=['post'], url_path='advance-stage')
    def advance_stage(self, request, pk=None):
        review = self.get_object()
        new_stage = request.data.get('stage')
        findings_summary = request.data.get('findings_summary')
        if not new_stage:
            return Response({'error': 'stage is required'}, status=status.HTTP_400_BAD_REQUEST)
        updated = ComplianceService.advance_review_stage(review, new_stage, findings_summary, request.user)
        return Response(ComplianceReviewSerializer(updated).data, status=status.HTTP_200_OK)


class EscalationRuleViewSet(viewsets.ModelViewSet):
    queryset = EscalationRule.objects.all()
    serializer_class = EscalationRuleSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        ComplianceService.seed_default_escalation_rules()
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        if category and category.lower() != 'all':
            qs = qs.filter(trigger_category__iexact=category)
        return qs

    @action(detail=True, methods=['post'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        rule = self.get_object()
        rule.is_active = not rule.is_active
        rule.save()
        return Response(EscalationRuleSerializer(rule).data, status=status.HTTP_200_OK)


class ComplianceCertificateViewSet(viewsets.ModelViewSet):
    queryset = ComplianceCertificate.objects.all().select_related('project')
    serializer_class = ComplianceCertificateSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        status_val = self.request.query_params.get('status')
        category = self.request.query_params.get('category')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if status_val and status_val.lower() != 'all':
            qs = qs.filter(status__iexact=status_val)
        if category and category.lower() != 'all':
            qs = qs.filter(category__iexact=category)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(certificate_reference__icontains=search) |
                Q(authority__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        file_obj = self.request.FILES.get('file')
        cert = ComplianceService.issue_certificate(self.request.data, self.request.user, file_obj=file_obj)
        serializer.instance = cert

    @action(detail=True, methods=['get'], url_path='verify')
    def verify(self, request, pk=None):
        cert = self.get_object()
        return Response({
            "is_valid": cert.status == 'Active',
            "certificate_reference": cert.certificate_reference,
            "title": cert.title,
            "authority": cert.authority,
            "expiry_date": cert.expiry_date,
            "qr_verification_hash": cert.qr_verification_hash
        }, status=status.HTTP_200_OK)


class ComplianceStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        stats_data = ComplianceService.get_overview_stats()
        return Response(stats_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='generate-report')
    def generate_report(self, request):
        stats_data = ComplianceService.get_overview_stats()
        open_ncrs = NonConformanceReport.objects.filter(status__in=['Open', 'In Progress']).select_related('project')
        certs = ComplianceCertificate.objects.filter(status='Active').select_related('project')
        
        return Response({
            "generated_at": timezone.now().isoformat(),
            "scorecard": stats_data,
            "active_infractions": NonConformanceReportSerializer(open_ncrs[:10], many=True).data,
            "awarded_certificates": ComplianceCertificateSerializer(certs[:10], many=True).data,
            "report_download_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/compliance_audit_report.pdf"
        }, status=status.HTTP_200_OK)
