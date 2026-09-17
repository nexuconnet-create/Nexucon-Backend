"""
Sync queue service: the enqueue contract and the replay loop.

Two operations, and each one is built around a single failure mode.

``enqueue`` is built around the *reused idempotency key*. The same
``client_item_id`` arriving twice is a retry, and it must be free. The same id
arriving with **different content** is a client trying to overwrite a write it
has already queued — and a queue that accepts that is a queue where a replay
silently becomes data loss. So the payload hash decides: identical → the
original row comes back untouched with ``deduplicated: true``; different → 409,
and the stored row is not modified.

``process`` is built around the *double apply*. Two flushes on a reconnecting
phone (the app retries, the user also taps "Sync now") will call this
concurrently. Every item is therefore claimed with a conditional ``UPDATE``
that is only allowed to move an item out of a claimable state, and the caller
checks the rowcount. Without that check both callers would see a PENDING row in
their SELECT and both would apply it.

The item and its target row commit together. That pairing is what makes a
crash mid-apply recoverable: there is no state in which the finding exists but
the queue does not know it, because the transaction that wrote one wrote the
other. A worker killed at any point either leaves both or neither.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from common.errors import describe_drf_error
from common.hashing import canonical_json, sha256_hex

from .appliers import REGISTRY, KNOWN_ACTIONS, SyncApplyError
from .models import SyncQueueItem

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """Refused queue operation. Carries an HTTP status for the view."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class SyncService:

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    @classmethod
    def enqueue(cls, *, user, client_item_id, entity_type, action,
                payload, entity_id=''):
        """Journal one offline write.

        Returns ``(item, created)``. ``created`` is False when this is a retry
        of an item already queued — the caller reports that as
        ``deduplicated: true`` rather than as an error, because a retry is the
        normal case on a flaky connection.
        """
        client_item_id = (client_item_id or '').strip()
        if not client_item_id:
            raise SyncError('client_item_id is required — it is what makes a '
                            'retry idempotent instead of a second write.')
        if len(client_item_id) > 128:
            raise SyncError('client_item_id must be 128 characters or fewer.')

        entity_type = (entity_type or '').strip().upper()
        action = (action or '').strip().upper()

        applier = REGISTRY.get(entity_type)
        if applier is None:
            raise SyncError(
                f'"{entity_type}" is not a queueable entity type. '
                f'Valid types: {", ".join(sorted(REGISTRY))}.')
        if action not in KNOWN_ACTIONS:
            raise SyncError(
                f'"{action}" is not a known action. '
                f'Valid actions: {", ".join(KNOWN_ACTIONS)}.')
        if action not in applier.actions:
            # Refused here, at enqueue, and not on the next reconnect. A queue
            # that accepts work it can never do is a trap: the inspector is
            # told the write is safe when it will fail on every future flush.
            raise SyncError(
                f'"{action}" is not supported for {entity_type}. '
                f'Supported: {", ".join(sorted(applier.actions))}. '
                + ('Records of this kind are resolved or superseded in the '
                   'field, never deleted.' if action == 'DELETE' else ''))

        if not isinstance(payload, dict):
            raise SyncError('payload must be an object.')

        # Structural checks only — no database access. See Applier.validate.
        try:
            applier.validate(payload, action)
        except SyncApplyError as exc:
            raise SyncError(str(exc))

        if action == 'UPDATE' and not (entity_id or '').strip():
            raise SyncError(
                f'An UPDATE {entity_type} must carry entity_id so the server '
                'knows which record it changes.')

        payload_hash = sha256_hex(canonical_json(payload))

        # The unique constraint on (inspector, client_item_id) is the arbiter.
        # `get_or_create` retries its SELECT when an INSERT loses the race, so
        # two simultaneous enqueues of one id end with one row, not an error.
        item, created = SyncQueueItem.objects.get_or_create(
            inspector=user,
            client_item_id=client_item_id,
            defaults={
                'entity_type': entity_type,
                'entity_id': (entity_id or '').strip(),
                'action': action,
                'payload': payload,
                'payload_hash': payload_hash,
            },
        )
        if created:
            return item, True

        if item.payload_hash != payload_hash:
            raise SyncError(
                f'client_item_id "{client_item_id}" is already queued with a '
                'different payload. Queueing new content under an id that has '
                'already been sent would overwrite a write the device believes '
                'is safe — use a new client_item_id, or resend the original '
                'payload to have this treated as a retry.',
                status_code=409)

        if item.entity_type != entity_type or item.action != action:
            raise SyncError(
                f'client_item_id "{client_item_id}" is already queued as '
                f'{item.entity_type} {item.action}. A retry must repeat the '
                'same operation.',
                status_code=409)

        return item, False

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------

    @classmethod
    def claimable(cls, user, include_exhausted=False):
        """The queryset of items a flush may attempt, oldest first.

        Three states are claimable and each for its own reason:

        ``PENDING``     never attempted.
        ``FAILED``      attempted and failed, but still under the retry cap.
                        An item past the cap is *exhausted* and is left alone
                        unless the caller explicitly asks for it — retrying a
                        deterministic failure forever is how a queue turns a
                        bad payload into a permanent denial of service on the
                        good ones behind it.
        ``PROCESSING``  claimed by a worker whose claim has gone stale, i.e. a
                        process that died mid-apply. Without this the item
                        would be stuck forever, which is the same data loss as
                        dropping it and harder to see.
        """
        stale_before = timezone.now() - timedelta(
            seconds=getattr(settings, 'SYNC_CLAIM_TIMEOUT_SECONDS', 300))
        max_retries = getattr(settings, 'SYNC_MAX_RETRIES', 5)

        condition = (
            Q(sync_status=SyncQueueItem.STATUS_PENDING)
            | Q(sync_status=SyncQueueItem.STATUS_PROCESSING,
                claimed_at__lt=stale_before)
        )
        if include_exhausted:
            condition |= Q(sync_status=SyncQueueItem.STATUS_FAILED)
        else:
            condition |= Q(sync_status=SyncQueueItem.STATUS_FAILED,
                           retry_count__lt=max_retries)

        return (SyncQueueItem.objects
                .filter(inspector=user)
                .filter(condition)
                .order_by('queued_at', 'id'))

    @classmethod
    def _claim(cls, item, include_exhausted=False):
        """Atomically move one item into PROCESSING. True if we won it.

        The status filter is re-evaluated by the database inside the UPDATE, so
        a second caller that read the same PENDING row a moment ago matches
        zero rows here and gets ``False``. Checking the rowcount is the whole
        mechanism — a SELECT-then-save would let both callers through.
        """
        stale_before = timezone.now() - timedelta(
            seconds=getattr(settings, 'SYNC_CLAIM_TIMEOUT_SECONDS', 300))
        max_retries = getattr(settings, 'SYNC_MAX_RETRIES', 5)

        condition = (
            Q(sync_status=SyncQueueItem.STATUS_PENDING)
            | Q(sync_status=SyncQueueItem.STATUS_PROCESSING,
                claimed_at__lt=stale_before)
        )
        if include_exhausted:
            condition |= Q(sync_status=SyncQueueItem.STATUS_FAILED)
        else:
            condition |= Q(sync_status=SyncQueueItem.STATUS_FAILED,
                           retry_count__lt=max_retries)

        now = timezone.now()
        return SyncQueueItem.objects.filter(pk=item.pk).filter(condition).update(
            sync_status=SyncQueueItem.STATUS_PROCESSING,
            claimed_at=now,
            updated_at=now,
        ) == 1

    @classmethod
    def process(cls, *, user, request, limit=50, include_exhausted=False):
        """Apply pending items oldest-first. Synchronous, by design.

        The PWA needs the answer while it still has a connection: "these three
        landed, this one was rejected, here is why". Handing that to a
        background worker would mean the inspector sees a spinner and then,
        later and elsewhere, a failure.
        """
        limit = max(1, min(int(limit or 50), 200))

        results = {'processed': 0, 'synced': 0, 'failed': 0,
                   'skipped': 0, 'remaining': 0}
        applied = []

        for item in list(cls.claimable(user, include_exhausted)[:limit]):
            if not cls._claim(item, include_exhausted):
                # Another caller took it between the SELECT and here.
                results['skipped'] += 1
                continue

            results['processed'] += 1
            try:
                with transaction.atomic():
                    result = cls._apply(item, request)
                    # Marked inside the same transaction as the target row, so
                    # a crash cannot leave a written record the queue still
                    # believes is pending.
                    item.mark_synced(result.model_label, result.target_id)
            except SyncApplyError as exc:
                item.mark_failed(str(exc))
                results['failed'] += 1
                applied.append({'reference': str(item.reference),
                                'entity_type': item.entity_type,
                                'action': item.action,
                                'status': item.sync_status,
                                'error': item.last_error})
                continue
            except Exception as exc:  # noqa: BLE001 — every failure is recorded
                # An unexpected error is still the client's to see, but a
                # stack trace is not: `last_error` is rendered on a phone.
                logger.exception('sync apply failed for %s', item.reference)
                item.mark_failed(describe_drf_error(exc))
                results['failed'] += 1
                applied.append({'reference': str(item.reference),
                                'entity_type': item.entity_type,
                                'action': item.action,
                                'status': item.sync_status,
                                'error': item.last_error})
                continue

            results['synced'] += 1
            item.refresh_from_db()
            applied.append({'reference': str(item.reference),
                            'entity_type': item.entity_type,
                            'action': item.action,
                            'status': item.sync_status,
                            'target_model': item.target_model,
                            'target_id': item.target_id,
                            'error': ''})

        results['remaining'] = cls.claimable(user, include_exhausted).count()
        results['applied'] = applied
        return results

    @classmethod
    def _apply(cls, item, request):
        applier = REGISTRY.get(item.entity_type)
        if applier is None:
            # A registry entry removed after the item was queued. Reported
            # rather than skipped: silently discarding it would hide the fact
            # that a device is still queueing work the server dropped support
            # for.
            raise SyncApplyError(
                f'{item.entity_type} is no longer a supported entity type.')
        return applier.apply(item, request)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @classmethod
    def status(cls, *, user, exhausted_limit=50):
        """Queue depth and health for one inspector.

        ``last_synced_at`` is ``max(synced_at)`` or ``None``. It is never
        ``now()``: a sync indicator that reads "synced just now" before
        anything has ever synced is the exact defect this endpoint replaces.
        """
        queryset = SyncQueueItem.objects.filter(inspector=user)

        counts = {value: 0 for value, _label in SyncQueueItem.STATUS_CHOICES}
        for row in queryset.values('sync_status').annotate(count=Count('id')):
            counts[row['sync_status']] = row['count']

        oldest_pending = (queryset
                          .filter(sync_status=SyncQueueItem.STATUS_PENDING)
                          .order_by('queued_at')
                          .values_list('queued_at', flat=True)
                          .first())
        last_synced = (queryset
                       .filter(sync_status=SyncQueueItem.STATUS_SYNCED)
                       .order_by('-synced_at')
                       .values_list('synced_at', flat=True)
                       .first())

        max_retries = getattr(settings, 'SYNC_MAX_RETRIES', 5)
        exhausted = list(queryset.filter(
            sync_status=SyncQueueItem.STATUS_FAILED,
            retry_count__gte=max_retries,
        ).order_by('queued_at')[:exhausted_limit])

        return {
            'counts': counts,
            'pending': counts[SyncQueueItem.STATUS_PENDING],
            'failed': counts[SyncQueueItem.STATUS_FAILED],
            'synced': counts[SyncQueueItem.STATUS_SYNCED],
            'processing': counts[SyncQueueItem.STATUS_PROCESSING],
            'oldest_pending_at': oldest_pending,
            'last_synced_at': last_synced,
            'max_retries': max_retries,
            # An exhausted item is reported, never dropped. Its payload and its
            # error are both intact so a human can decide what to do with it.
            'exhausted': [
                {
                    'reference': str(item.reference),
                    'client_item_id': item.client_item_id,
                    'entity_type': item.entity_type,
                    'action': item.action,
                    'retry_count': item.retry_count,
                    'last_error': item.last_error,
                    'queued_at': item.queued_at,
                }
                for item in exhausted
            ],
        }
