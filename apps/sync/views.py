"""
Sync queue API views (Inspector PWA — Part 3).

Routes:
  POST sync/queue/    journal one offline write
  POST sync/process/  apply everything claimable, oldest first
  GET  sync/status/   queue depth, health, and what is stuck

The queue is scoped to the authenticated caller and nothing else. It is the
journal of *a device's* offline writes; a Director reading a colleague's queue
would learn what that colleague has captured but not yet filed, which is not a
supervisory fact — it is their unfinished work. There is no route that reads
another user's queue.
"""
import logging

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import user_role_name

from .serializers import (
    EnqueueRequestSerializer,
    ProcessResponseSerializer,
    StatusResponseSerializer,
    SyncQueueItemSerializer,
)
from .services import SyncError, SyncService

logger = logging.getLogger(__name__)


def _record_audit(user, action, resource_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type='SyncQueueItem',
            resource_id=str(resource_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


class SyncEnqueueView(APIView):
    """`POST sync/queue/` — journal an offline write.

    Responds 201 when the item is new and 200 when it is a retry of one already
    queued. The distinction is carried by ``deduplicated`` so a client can tell
    the two apart without reading the status code; both are successes, because
    on a flaky connection a retry is the expected case rather than an error.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=EnqueueRequestSerializer,
                   responses={201: SyncQueueItemSerializer})
    def post(self, request):
        serializer = EnqueueRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            item, created = SyncService.enqueue(
                user=request.user,
                client_item_id=data['client_item_id'],
                entity_type=data['entity_type'],
                action=data['action'],
                payload=data['payload'],
                entity_id=data.get('entity_id') or '',
            )
        except SyncError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        if created:
            _record_audit(request.user, 'sync.item.queue', item.reference, {
                'entity_type': item.entity_type,
                'action': item.action,
                'client_item_id': item.client_item_id,
            })

        body = SyncQueueItemSerializer(item).data
        body['deduplicated'] = not created
        return Response(body, status=(
            status.HTTP_201_CREATED if created else status.HTTP_200_OK))


class SyncProcessView(APIView):
    """`POST sync/process/` — flush the queue.

    Synchronous: the PWA needs the answer while it still has a connection, so
    the failure of one item is reported in the same response that reports the
    success of the others. Each item is applied independently — one malformed
    finding does not stop the valid inspection behind it from landing.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        parameters=[
            OpenApiParameter('limit', int, description='Max items to apply (1-200, default 50).'),
            OpenApiParameter('retry_exhausted', bool,
                             description='Also retry items already past the retry cap.'),
        ],
        responses={200: ProcessResponseSerializer},
    )
    def post(self, request):
        limit = request.query_params.get('limit') or request.data.get('limit') or 50
        raw_flag = (request.query_params.get('retry_exhausted')
                    or request.data.get('retry_exhausted'))
        include_exhausted = str(raw_flag).lower() in ('1', 'true', 'yes', 'on')

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return Response({'detail': 'limit must be an integer.'},
                            status=status.HTTP_400_BAD_REQUEST)

        results = SyncService.process(
            user=request.user, request=request,
            limit=limit, include_exhausted=include_exhausted,
        )
        if results['synced'] or results['failed']:
            _record_audit(request.user, 'sync.queue.process', request.user.id, {
                'processed': results['processed'],
                'synced': results['synced'],
                'failed': results['failed'],
                'skipped': results['skipped'],
            })
        return Response(results)


class SyncStatusView(APIView):
    """`GET sync/status/` — queue depth, health, and what is stuck."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: StatusResponseSerializer})
    def get(self, request):
        return Response(SyncService.status(user=request.user))
