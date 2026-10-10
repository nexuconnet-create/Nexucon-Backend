from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from django.db.models import Q
from .models import Notification, EmailDelivery, NotificationPreference
from .serializers import NotificationSerializer, EmailDeliverySerializer, NotificationPreferenceSerializer
from .services import NotificationService

class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        category = self.request.query_params.get('category')
        priority = self.request.query_params.get('priority')
        is_read = self.request.query_params.get('is_read')
        is_ack = self.request.query_params.get('is_acknowledged')

        if category and category.lower() != 'all':
            qs = qs.filter(category__iexact=category)
        if priority and priority.lower() != 'all':
            qs = qs.filter(priority__iexact=priority)
        if is_read is not None:
            qs = qs.filter(is_read=(is_read.lower() == 'true'))
        if is_ack is not None:
            qs = qs.filter(is_acknowledged=(is_ack.lower() == 'true'))

        return qs

    @action(detail=True, methods=['post'], url_path='read')
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save()
        return Response(NotificationSerializer(notif).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='read-all')
    def mark_all_read(self, request):
        category = request.data.get('category') or request.query_params.get('category')
        qs = Notification.objects.filter(is_read=False)
        if request.user.is_authenticated:
            qs = qs.filter(Q(recipient=request.user) | Q(recipient__isnull=True))
        if category and category.lower() != 'all':
            qs = qs.filter(category__iexact=category)
        qs.update(is_read=True, read_at=timezone.now())
        return Response({"message": "Notifications marked as read."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='acknowledge')
    def acknowledge(self, request, pk=None):
        notif = self.get_object()
        notif.is_acknowledged = True
        notif.acknowledged_at = timezone.now()
        if request.user.is_authenticated:
            notif.acknowledged_by = request.user
        notif.save()
        return Response(NotificationSerializer(notif).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='respond')
    def respond(self, request, pk=None):
        """
        Record a statutory directive, decision, or officer comment from the quick sidepop drawer.
        """
        notif = self.get_object()
        comment = request.data.get('comment') or request.data.get('directive') or request.data.get('message', '')
        action_type = request.data.get('action_type', 'DIRECTIVE')

        # Mark as read
        notif.is_read = True
        notif.read_at = timezone.now()

        # Update metadata with responses trail
        meta = notif.metadata or {}
        responses = meta.get('responses', [])
        user_name = request.user.get_full_name() or request.user.username if request.user.is_authenticated else 'Government Official'
        
        responses.append({
            'comment': comment,
            'action_type': action_type,
            'officer': user_name,
            'timestamp': timezone.now().isoformat()
        })
        meta['responses'] = responses
        meta['last_directive'] = comment
        notif.metadata = meta
        notif.save()

        # Log audit event
        try:
            from apps.audit.models import AuditEvent
            AuditEvent.objects.create(
                user=request.user if request.user.is_authenticated else None,
                action="NOTIFICATION_DIRECTIVE_SUBMITTED",
                resource_type="Notification",
                resource_id=str(notif.id),
                new_state={"directive": comment, "reference": notif.notification_reference}
            )
        except Exception:
            pass

        return Response(NotificationSerializer(notif).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='unread-counts')
    def unread_counts(self, request):
        all_notifs = Notification.objects.all()
        return Response({
            "total_unread": all_notifs.filter(is_read=False).count(),
            "critical": all_notifs.filter(category='CRITICAL', is_read=False).count(),
            "applications": all_notifs.filter(category='APPLICATIONS', is_read=False).count(),
            "inspections": all_notifs.filter(category='INSPECTIONS', is_read=False).count(),
            "compliance": all_notifs.filter(category='COMPLIANCE', is_read=False).count(),
            "approvals": all_notifs.filter(category='APPROVALS', is_read=False).count(),
            "emergency": all_notifs.filter(category='EMERGENCY', is_read=False).count(),
            "overdue": all_notifs.filter(category='OVERDUE', is_read=False).count(),
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get', 'post', 'put', 'patch'], url_path='preferences')
    def preferences(self, request):
        if not request.user or not request.user.is_authenticated:
            # Return standard default preferences for anonymous/demo
            return Response({
                "in_app_enabled": True,
                "email_enabled": True,
                "email_applications": True,
                "email_inspections": True,
                "email_approvals": True,
                "email_compliance": True,
                "email_emergency": True,
                "email_overdue": True,
                "email_critical": True,
                "email_bim": True,
                "email_gpr": True,
                "email_documents": True,
                "email_milestones": True,
            }, status=status.HTTP_200_OK)

        pref, _ = NotificationPreference.objects.get_or_create(user=request.user)
        if request.method in ['POST', 'PUT', 'PATCH']:
            serializer = NotificationPreferenceSerializer(pref, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(NotificationPreferenceSerializer(pref).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='trigger-test')
    def trigger_test(self, request):
        cat = request.data.get('category', 'APPLICATIONS')
        title = request.data.get('title', 'Statutory Audit Test Alert')
        message = request.data.get('message', 'This is a test notification dispatched to verify email delivery channels.')
        
        notif = NotificationService.dispatch_event(
            event_type="TEST_NOTIFICATION",
            title=title,
            message=message,
            category=cat,
            priority=request.data.get('priority', 'Medium'),
            recipient=request.user if request.user.is_authenticated else None,
            action_url=request.data.get('action_url', '/government/dashboard/notifications/applications')
        )
        return Response(NotificationSerializer(notif).data, status=status.HTTP_201_CREATED)


class EmailDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EmailDelivery.objects.all()
    serializer_class = EmailDeliverySerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return NotificationPreference.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


from .models import WebhookEndpoint
from .serializers import WebhookEndpointSerializer

class WebhookEndpointViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Webhook Endpoints.
    """
    queryset = WebhookEndpoint.objects.all().order_by('-created_at')
    serializer_class = WebhookEndpointSerializer



# ---------------------------------------------------------------------------
# Mobile push (FCM) device registration — plan §5 Week 6
# ---------------------------------------------------------------------------
from rest_framework import serializers as push_serializers

from .models import PushDeviceToken


class PushDeviceTokenSerializer(push_serializers.ModelSerializer):
    class Meta:
        model = PushDeviceToken
        fields = ['id', 'token', 'platform', 'device_name', 'is_active',
                  'last_used_at', 'created_at']
        read_only_fields = ['id', 'is_active', 'last_used_at', 'created_at']


class PushDeviceTokenViewSet(viewsets.ModelViewSet):
    """
    Register / list / remove FCM device tokens for push notifications.
    A device registers its real FCM token after the mobile app obtains it
    from Firebase — tokens are never generated server-side.
    """
    serializer_class = PushDeviceTokenSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']
    filterset_fields = ['platform', 'is_active']

    def get_queryset(self):
        return PushDeviceToken.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        from django.db import IntegrityError
        token = serializer.validated_data.get('token')
        existing = PushDeviceToken.objects.filter(token=token).first()
        if existing:
            # Re-registration (app reinstall / token refresh): keep one row.
            existing.user = self.request.user
            existing.platform = serializer.validated_data.get('platform', existing.platform)
            existing.device_name = serializer.validated_data.get('device_name', existing.device_name)
            existing.is_active = True
            existing.save()
            self.existing = existing
            return
        serializer.save(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = getattr(self, 'existing', None) or serializer.instance
        if instance is None:
            instance = PushDeviceToken.objects.filter(
                token=serializer.validated_data['token']).first()
        return Response(self.get_serializer(instance).data,
                        status=status.HTTP_201_CREATED)
