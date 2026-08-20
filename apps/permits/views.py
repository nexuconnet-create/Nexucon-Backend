from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
import datetime
from .models import Permit
from .serializers import PermitSerializer
from .services import PermitService

class PermitViewSet(viewsets.ModelViewSet):
    queryset = Permit.objects.all().select_related('project', 'application', 'application__applicant', 'issued_by')
    serializer_class = PermitSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        project_param = self.request.query_params.get('project')
        expiring_soon_param = self.request.query_params.get('expiring_soon')
        search_param = self.request.query_params.get('search')

        if status_param:
            queryset = queryset.filter(status=status_param.upper())

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if expiring_soon_param in ['true', 'True', '1']:
            today = datetime.date.today()
            thirty_days = today + datetime.timedelta(days=30)
            queryset = queryset.filter(status='ACTIVE', expiry_date__gte=today, expiry_date__lte=thirty_days)

        if search_param:
            queryset = queryset.filter(
                Q(permit_number__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(application__application_reference__icontains=search_param) |
                Q(application__applicant__email__icontains=search_param)
            )

        return queryset

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        """Return counts for active, expired, and expiring permits."""
        today = datetime.date.today()
        thirty_days = today + datetime.timedelta(days=30)

        total = Permit.objects.count()
        active = Permit.objects.filter(status='ACTIVE').count()
        expiring_soon = Permit.objects.filter(status='ACTIVE', expiry_date__gte=today, expiry_date__lte=thirty_days).count()
        expired = Permit.objects.filter(status='EXPIRED').count()
        # Also include permits whose expiry_date < today and marked ACTIVE
        expired_by_date = Permit.objects.filter(expiry_date__lt=today).count()
        suspended = Permit.objects.filter(status='SUSPENDED').count()

        return Response({
            'success': True,
            'data': {
                'total': total,
                'active': active,
                'expiring_soon': expiring_soon,
                'expired': max(expired, expired_by_date),
                'suspended': suspended
            }
        })

    @action(detail=True, methods=['post'], url_path='renew')
    def renew(self, request, pk=None):
        """Extend permit validity."""
        permit = self.get_object()
        extension_months = int(request.data.get('extension_months', 12))
        notes = request.data.get('notes', '')

        updated = PermitService.renew_permit(
            permit=permit,
            user=request.user,
            extension_months=extension_months,
            notes=notes
        )

        return Response({
            'success': True,
            'message': f"Permit {permit.permit_number} renewed successfully until {updated.expiry_date}",
            'data': PermitSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='send-notice')
    def send_notice(self, request, pk=None):
        """Send formal expiry notice to developer/applicant."""
        permit = self.get_object()
        notice = PermitService.send_expiry_notice(permit=permit, user=request.user)
        return Response({
            'success': True,
            'message': f"Expiry reminder notice sent to applicant for permit {permit.permit_number}",
            'data': notice
        })

    @action(detail=True, methods=['post'], url_path='suspend')
    def suspend(self, request, pk=None):
        """Suspend permit due to regulatory issue or stop-work."""
        permit = self.get_object()
        reason = request.data.get('reason', 'Regulatory suspension')
        updated = PermitService.suspend_permit(permit=permit, user=request.user, reason=reason)
        return Response({
            'success': True,
            'message': f"Permit {permit.permit_number} has been suspended.",
            'data': PermitSerializer(updated).data
        })
