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

        # Seed baseline notifications if database is clean
        if not qs.exists():
            self.seed_defaults()
            qs = Notification.objects.all()

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

    def seed_defaults(self):
        defaults = [
            {"title": "Emergency Dispatch: Scaffold Collapse Alert", "message": "Emergency response team dispatched to Sector 4 Marina coastal site.", "category": "EMERGENCY", "priority": "Critical", "location": "Marina Waterfront Block B", "action_url": "/government/dashboard/notifications/emergency"},
            {"title": "Critical Finding: Foundation Slab Deflection", "message": "LiDAR survey detected 4.2mm structural settlement on Pier 12.", "category": "CRITICAL", "priority": "Critical", "location": "Lekki Deep Sea Logistics Hub", "action_url": "/government/dashboard/inspections/findings"},
            {"title": "New Permit Application: Marina Tower Phase 2", "message": "High-rise commercial construction permit submitted by Apex Engineering.", "category": "APPLICATIONS", "priority": "High", "action_url": "/government/dashboard/applications"},
            {"title": "Inspection Request: Rebar Placement Check", "message": "Stage 3 foundation reinforcement inspection scheduled for tomorrow 10:00 AM.", "category": "INSPECTIONS", "priority": "Medium", "action_url": "/government/dashboard/inspections/requests"},
            {"title": "Technical Review Required: Structural Calculations", "message": "Delegation of Authority approval pending for Eko Atlantic Marina Tower.", "category": "APPROVALS", "priority": "High", "action_url": "/government/dashboard/approvals/pending"},
            {"title": "Overdue CAPA: Water Drainage Containment", "message": "Corrective action plan overdue by 48 hours for non-conformance NCR-041.", "category": "OVERDUE", "priority": "High", "action_url": "/government/dashboard/compliance/corrective-actions"},
            {"title": "Compliance NCR Flagged: Batch Steel Test Missing", "message": "Statutory rebar tensile test certificates required before concrete pour.", "category": "COMPLIANCE", "priority": "High", "action_url": "/government/dashboard/compliance/non-conformances"},
        ]
        for d in defaults:
            Notification.objects.get_or_create(title=d["title"], defaults=d)

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

    @action(detail=False, methods=['get', 'put', 'patch'], url_path='preferences')
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
        if request.method in ['PUT', 'PATCH']:
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
