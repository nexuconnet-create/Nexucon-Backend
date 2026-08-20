from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import AuditEvent
from .serializers import AuditEventSerializer
from .services import AuditService

class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only viewset for inspecting immutable audit events and cryptographic proof.
    """
    queryset = AuditEvent.objects.all()
    serializer_class = AuditEventSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        AuditService.seed_initial_audit_records()
        qs = super().get_queryset()
        resource_type = self.request.query_params.get('resource_type')
        action_name = self.request.query_params.get('action')
        severity = self.request.query_params.get('severity')
        search = self.request.query_params.get('search')

        if resource_type and resource_type.upper() != 'ALL':
            qs = qs.filter(resource_type__iexact=resource_type)
        if action_name and action_name.upper() != 'ALL':
            qs = qs.filter(action__iexact=action_name)
        if severity and severity.upper() != 'ALL':
            qs = qs.filter(severity__iexact=severity)
        if search:
            qs = qs.filter(
                Q(action__icontains=search) |
                Q(audit_reference__icontains=search) |
                Q(user_name__icontains=search) |
                Q(resource_type__icontains=search) |
                Q(project_name__icontains=search) |
                Q(resource_id__icontains=search)
            )
        return qs

    @action(detail=True, methods=['get'], url_path='diff')
    def diff(self, request, pk=None):
        event = self.get_object()
        diff_data = AuditService.compute_diff(event)
        return Response(diff_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='verify-chain')
    def verify_chain(self, request):
        res = AuditService.verify_hash_chain()
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        summary_data = AuditService.get_audit_summary()
        return Response(summary_data, status=status.HTTP_200_OK)
