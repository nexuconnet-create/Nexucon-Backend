from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria, ApprovalComment
from .serializers import (
    ApprovalRequestSerializer, ApprovalDecisionSerializer, 
    TechnicalReviewCriteriaSerializer, ApprovalCommentSerializer
)
from .services import ApprovalService

class ApprovalRequestViewSet(viewsets.ModelViewSet):
    queryset = ApprovalRequest.objects.all().select_related('project')
    serializer_class = ApprovalRequestSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        req_type = self.request.query_params.get('type')
        discipline = self.request.query_params.get('discipline')
        priority = self.request.query_params.get('priority')
        status_val = self.request.query_params.get('status')
        source_type = self.request.query_params.get('source_entity_type')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if req_type and req_type.lower() != 'all':
            qs = qs.filter(request_type__iexact=req_type)
        if discipline and discipline.lower() != 'all':
            qs = qs.filter(discipline__iexact=discipline)
        if priority and priority.lower() != 'all':
            qs = qs.filter(priority__iexact=priority)
        if status_val and status_val.lower() != 'all':
            qs = qs.filter(status__iexact=status_val)
        if source_type and source_type.lower() != 'all':
            qs = qs.filter(source_entity_type__iexact=source_type)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(request_reference__icontains=search) |
                Q(submitted_by_name__icontains=search) |
                Q(description__icontains=search) |
                Q(source_version_hash__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        request_obj = ApprovalService.create_request(self.request.data, self.request.user)
        serializer.instance = request_obj

    @action(detail=True, methods=['post'], url_path='assign')
    def assign_reviewer(self, request, pk=None):
        req_obj = self.get_object()
        reviewer_name = request.data.get('reviewer_name', 'Government Reviewer')
        reviewer_user = request.user if getattr(request.user, 'is_authenticated', False) else None
        updated = ApprovalService.assign_reviewer(req_obj, reviewer_user, reviewer_name, request.user)
        return Response(ApprovalRequestSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        req_obj = self.get_object()
        notes = request.data.get('notes', '')
        pin = request.data.get('pin')
        conditions = request.data.get('conditions')
        decision = ApprovalService.approve_request(req_obj, request.user, notes, pin, conditions)
        return Response({
            "message": "Request successfully approved",
            "decision": ApprovalDecisionSerializer(decision).data,
            "request": ApprovalRequestSerializer(req_obj).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        req_obj = self.get_object()
        reason = request.data.get('reason', 'Administrative rejection.')
        decision = ApprovalService.reject_request(req_obj, request.user, reason)
        return Response({
            "message": "Request rejected",
            "decision": ApprovalDecisionSerializer(decision).data,
            "request": ApprovalRequestSerializer(req_obj).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='request-revision')
    def request_revision(self, request, pk=None):
        req_obj = self.get_object()
        revision_notes = request.data.get('revision_notes') or request.data.get('notes', 'Revision required.')
        attachment = request.data.get('attachment_url')
        decision = ApprovalService.request_revision(req_obj, request.user, revision_notes, attachment)
        return Response({
            "message": "Revision requested and notification sent to contractor",
            "decision": ApprovalDecisionSerializer(decision).data,
            "request": ApprovalRequestSerializer(req_obj).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='request-info')
    def request_info(self, request, pk=None):
        req_obj = self.get_object()
        query_text = request.data.get('query', 'Additional documentation requested.')
        decision = ApprovalService.request_info(req_obj, request.user, query_text)
        return Response({
            "message": "Information requested",
            "decision": ApprovalDecisionSerializer(decision).data,
            "request": ApprovalRequestSerializer(req_obj).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='escalate')
    def escalate(self, request, pk=None):
        req_obj = self.get_object()
        reason = request.data.get('reason', 'High financial value / SLA exceeded')
        target_level = request.data.get('target_level', 'Permanent Secretary / Director General')
        decision = ApprovalService.escalate_request(req_obj, request.user, reason, target_level)
        return Response({
            "message": f"Escalated to {target_level}",
            "decision": ApprovalDecisionSerializer(decision).data,
            "request": ApprovalRequestSerializer(req_obj).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='sign')
    def sign(self, request, pk=None):
        req_obj = self.get_object()
        updated = ApprovalService.sign_document(req_obj, request.user)
        return Response(ApprovalRequestSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='compliance-gate')
    def compliance_gate(self, request, pk=None):
        req_obj = self.get_object()
        gate_result = ApprovalService.evaluate_compliance_gate(req_obj)
        return Response(gate_result, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='comments')
    def comments(self, request, pk=None):
        req_obj = self.get_object()
        if request.method == 'POST':
            content = request.data.get('content', '')
            ctype = request.data.get('comment_type', 'General')
            attachment = request.data.get('attachment_url')
            comment = ApprovalService.add_comment(req_obj, request.user, content, ctype, attachment)
            return Response(ApprovalCommentSerializer(comment).data, status=status.HTTP_201_CREATED)
        comments_qs = req_obj.comments.all()
        return Response(ApprovalCommentSerializer(comments_qs, many=True).data, status=status.HTTP_200_OK)


class ApprovalDecisionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ApprovalDecision.objects.all().select_related('approval_request')
    serializer_class = ApprovalDecisionSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        outcome = self.request.query_params.get('outcome')
        search = self.request.query_params.get('search')

        if outcome and outcome.lower() != 'all':
            qs = qs.filter(outcome__iexact=outcome)
        if search:
            qs = qs.filter(
                Q(decision_reference__icontains=search) |
                Q(decider_name__icontains=search) |
                Q(decision_notes__icontains=search) |
                Q(approval_request__title__icontains=search) |
                Q(approval_request__request_reference__icontains=search)
            )
        return qs


class TechnicalCriteriaViewSet(viewsets.ModelViewSet):
    queryset = TechnicalReviewCriteria.objects.all()
    serializer_class = TechnicalReviewCriteriaSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=True, methods=['post'], url_path='evaluate')
    def evaluate(self, request, pk=None):
        status_val = request.data.get('status', 'pass')
        notes = request.data.get('notes')
        criterion = ApprovalService.evaluate_criterion(pk, status_val, notes, request.user)
        return Response(TechnicalReviewCriteriaSerializer(criterion).data, status=status.HTTP_200_OK)


class ApprovalStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        stats_data = ApprovalService.get_approval_stats()
        return Response(stats_data, status=status.HTTP_200_OK)
