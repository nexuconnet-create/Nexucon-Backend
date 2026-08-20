from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import Notification, NotificationPreference
from .serializers import NotificationSerializer, NotificationPreferenceSerializer
from .services import NotificationService

class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        NotificationService.seed_initial_notifications()
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        priority = self.request.query_params.get('priority')
        unread_only = self.request.query_params.get('unread_only')
        search = self.request.query_params.get('search')

        if category and category.upper() != 'ALL':
            qs = qs.filter(category__iexact=category)
        if priority and priority.upper() != 'ALL':
            qs = qs.filter(priority__iexact=priority)
        if unread_only in ['true', '1', True]:
            qs = qs.filter(is_read=False)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(message__icontains=search) |
                Q(notification_reference__icontains=search) |
                Q(location__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        notif = NotificationService.send_notification(self.request.data, self.request.user)
        serializer.instance = notif

    @action(detail=True, methods=['post'], url_path='read')
    def mark_read(self, request, pk=None):
        notif = NotificationService.mark_as_read(pk, request.user)
        return Response(NotificationSerializer(notif).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='mark-all-read')
    def mark_all_read(self, request):
        category = request.data.get('category')
        count = NotificationService.mark_all_as_read(category, request.user)
        return Response({"status": "Success", "marked_read_count": count}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='acknowledge')
    def acknowledge(self, request, pk=None):
        notif = NotificationService.acknowledge_critical(pk, request.user)
        return Response(NotificationSerializer(notif).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='sound-alarm')
    def sound_alarm(self, request):
        location = request.data.get('location', 'All Active Sectors')
        reason = request.data.get('reason', 'Immediate evacuation order triggered by government command.')
        notif = NotificationService.sound_site_alarm(location, reason, request.user)
        return Response(NotificationSerializer(notif).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='ping')
    def ping_assignee(self, request, pk=None):
        method = request.data.get('method', 'Email')
        res = NotificationService.ping_assignee(pk, method, request.user)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='unread-counts')
    def unread_counts(self, request):
        counts = NotificationService.get_unread_counts(request.user)
        return Response(counts, status=status.HTTP_200_OK)


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return NotificationPreference.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
