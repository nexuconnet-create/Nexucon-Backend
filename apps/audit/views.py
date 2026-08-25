from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from django.http import HttpResponse
from .models import AuditEvent
from .serializers import AuditEventSerializer
from .services import AuditService

class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Authoritative read-only viewset for inspecting immutable audit events and cryptographic proof.
    """
    queryset = AuditEvent.objects.all()
    serializer_class = AuditEventSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        AuditService.seed_initial_audit_records()
        qs = super().get_queryset()
        
        resource_type = self.request.query_params.get('resource_type')
        action_name = self.request.query_params.get('action')
        module = self.request.query_params.get('module')
        severity = self.request.query_params.get('severity')
        user_role = self.request.query_params.get('user_role') or self.request.query_params.get('role')
        project = self.request.query_params.get('project')
        search = self.request.query_params.get('search')
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')

        if resource_type and resource_type.upper() != 'ALL':
            qs = qs.filter(resource_type__iexact=resource_type)
            
        if action_name and action_name.upper() != 'ALL':
            qs = qs.filter(action__icontains=action_name)
            
        if module and module.upper() != 'ALL':
            mod = module.lower()
            if mod == 'approvals':
                qs = qs.filter(Q(resource_type__icontains='Approval') | Q(action__icontains='APPROV') | Q(resource_type__icontains='Decision'))
            elif mod == 'inspections':
                qs = qs.filter(Q(resource_type__icontains='Inspection') | Q(action__icontains='INSPECT'))
            elif mod == 'documents':
                qs = qs.filter(Q(resource_type__icontains='Document') | Q(action__icontains='DOCUMENT') | Q(action__icontains='DRAWING'))
            elif mod == 'users':
                qs = qs.filter(Q(resource_type__icontains='User') | Q(action__icontains='USER') | Q(action__icontains='ROLE') | Q(action__icontains='LOGIN'))
            elif mod == 'compliance':
                qs = qs.filter(Q(resource_type__icontains='NonConformance') | Q(action__icontains='NCR') | Q(action__icontains='COMPLIAN'))
            elif mod == 'bim':
                qs = qs.filter(Q(resource_type__icontains='BIM') | Q(action__icontains='BIM') | Q(action__icontains='CLASH'))
            elif mod == 'gpr':
                qs = qs.filter(Q(resource_type__icontains='GPR') | Q(action__icontains='GPR'))

        if severity and severity.upper() != 'ALL':
            qs = qs.filter(severity__iexact=severity)

        if user_role and user_role.upper() != 'ALL':
            qs = qs.filter(user_role__icontains=user_role)

        if project and project.upper() != 'ALL':
            qs = qs.filter(project_name__icontains=project)

        if date_from:
            qs = qs.filter(timestamp__date__gte=date_from)
        if date_to:
            qs = qs.filter(timestamp__date__lte=date_to)

        if search:
            qs = qs.filter(
                Q(action__icontains=search) |
                Q(audit_reference__icontains=search) |
                Q(user_name__icontains=search) |
                Q(user_role__icontains=search) |
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

    @action(detail=False, methods=['get', 'post'], url_path='verify-chain', permission_classes=[permissions.AllowAny])
    def verify_chain(self, request):
        res = AuditService.verify_hash_chain()
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='summary', permission_classes=[permissions.AllowAny])
    def summary(self, request):
        summary_data = AuditService.get_audit_summary()
        return Response(summary_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get', 'post'], url_path='export', permission_classes=[permissions.AllowAny])
    def export_ledger(self, request):
        filters = {
            'resource_type': request.data.get('resource_type') if request.method == 'POST' else request.query_params.get('resource_type'),
            'action': request.data.get('action') if request.method == 'POST' else request.query_params.get('action'),
            'severity': request.data.get('severity') if request.method == 'POST' else request.query_params.get('severity')
        }
        user = request.user if request.user.is_authenticated else None
        csv_data = AuditService.export_audit_csv(filters=filters, user=user)
        response = HttpResponse(csv_data, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="Nexucon_Audit_Ledger.csv"'
        return response
