"""Sync queue models.

One row is one offline write, journalled on the server so a reconnecting client
can be told exactly what has already landed and what has not.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.hashing import canonical_json, sha256_hex


class SyncQueueItem(models.Model):
    """A single offline write awaiting replay.

    Three fields carry the whole idempotency contract, and they are worth
    telling apart because each answers a different question:

    ``client_item_id``
        The client's own key for *this queue entry*. Unique per inspector, so
        two devices belonging to two inspectors can never collide, and one
        inspector's client cannot overwrite another's queued work. Re-sending
        the same key is a retry, not a second write — that is what makes the
        queue safe to flush on a flaky connection.

    ``entity_id``
        The client's identifier for the *target row* (the spec's `entity_id`).
        For an UPDATE it names the record being changed. For a CREATE it is the
        client's provisional id and is recorded as provenance, not used as the
        server primary key.

    ``payload_hash``
        SHA-256 over the canonical payload. Same ``client_item_id`` with a
        *different* hash is refused with 409 — see ``services.enqueue``. That
        case is the one worth being loud about: a client that reuses an id for
        new content is trying to overwrite a queued write, and quietly
        accepting it is how a replay becomes data loss.
    """

    ENTITY_INSPECTION = 'INSPECTION'
    ENTITY_FINDING = 'FINDING'
    ENTITY_STOP_WORK_ORDER = 'STOP_WORK_ORDER'
    ENTITY_EVIDENCE = 'EVIDENCE'
    ENTITY_TELEMETRY = 'TELEMETRY'
    ENTITY_CHOICES = [
        (ENTITY_INSPECTION, 'Inspection'),
        (ENTITY_FINDING, 'Finding'),
        (ENTITY_STOP_WORK_ORDER, 'Stop work order'),
        (ENTITY_EVIDENCE, 'Evidence record'),
        (ENTITY_TELEMETRY, 'Telemetry session'),
    ]

    ACTION_CREATE = 'CREATE'
    ACTION_UPDATE = 'UPDATE'
    ACTION_DELETE = 'DELETE'
    ACTION_CHOICES = [
        (ACTION_CREATE, 'Create'),
        (ACTION_UPDATE, 'Update'),
        (ACTION_DELETE, 'Delete'),
    ]

    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_SYNCED = 'SYNCED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_SYNCED, 'Synced'),
        (STATUS_FAILED, 'Failed'),
    ]

    #: The primary key is an autoincrementing integer on purpose. The queue is
    #: replayed oldest-first and several items routinely share a `queued_at`
    #: timestamp (they were captured in the same offline burst, and they are
    #: inserted in the same millisecond on reconnect). Breaking that tie with a
    #: random UUID would replay a batch in an arbitrary order — and order is
    #: load-bearing here, because an UPDATE replayed before its CREATE is a
    #: corruption, not a hiccup. `id` is monotonic, so `['queued_at', 'id']`
    #: is a total order that matches capture order.
    id = models.BigAutoField(primary_key=True)

    #: The public identifier. The API addresses items by this, never by `id`,
    #: so the queue's internal ordering is not exposed as a sequential integer
    #: that would let one inspector count another's queue depth.
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='sync_queue_items',
        help_text='The user whose device queued this write',
    )
    client_item_id = models.CharField(
        max_length=128,
        help_text='Client-generated idempotency key, unique per inspector',
    )

    entity_type = models.CharField(max_length=32, choices=ENTITY_CHOICES, db_index=True)
    entity_id = models.CharField(
        max_length=64, blank=True, default='',
        help_text='The client\'s identifier for the target row',
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    payload_hash = models.CharField(max_length=64, editable=False, default='')

    sync_status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    retry_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(
        blank=True, default='',
        help_text=(
            'Why the last attempt failed, as a message. Never a stack trace: '
            'this string is returned to a mobile client.'
        ),
    )

    #: Where the write landed. `target_model` is the app label and class
    #: (`inspections.Finding`), the same convention `EvidenceRecord` uses, so a
    #: synced item can be traced to its row without a second lookup table.
    target_model = models.CharField(max_length=100, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='')

    queued_at = models.DateTimeField(auto_now_add=True, db_index=True)
    claimed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When a processor took this item; cleared on completion',
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['queued_at', 'id']
        verbose_name = 'Sync queue item'
        verbose_name_plural = 'Sync queue items'
        constraints = [
            models.UniqueConstraint(
                fields=['inspector', 'client_item_id'],
                name='uniq_sync_item_inspector_client_item',
            ),
        ]
        indexes = [
            models.Index(fields=['inspector', 'sync_status']),
            models.Index(fields=['inspector', 'entity_type', 'entity_id']),
            models.Index(fields=['sync_status', 'queued_at']),
        ]

    def __str__(self):
        return (f'{self.entity_type} {self.action} '
                f'[{self.client_item_id}] — {self.sync_status}')

    def save(self, *args, **kwargs):
        # The hash is derived from the payload and never accepted from a
        # client: a client-supplied hash would let a replayed request declare
        # itself identical to a different payload.
        self.payload_hash = self.compute_payload_hash()
        super().save(*args, **kwargs)

    def compute_payload_hash(self) -> str:
        return sha256_hex(canonical_json(self.payload))

    @property
    def max_retries(self) -> int:
        return getattr(settings, 'SYNC_MAX_RETRIES', 5)

    @property
    def exhausted(self) -> bool:
        """True when this item has failed as often as it is allowed to.

        Exhausted is a *reporting* state, not a terminal one. The row keeps its
        payload, its error and its retry count, and `POST sync/process/` will
        still pick it up if a human asks it to — the point is that the failure
        is visible in `GET sync/status/` rather than silently dropped.
        """
        return self.sync_status == self.STATUS_FAILED and self.retry_count >= self.max_retries

    @property
    def claim_is_stale(self) -> bool:
        """Has a PROCESSING claim outlived its timeout?

        A worker that is killed mid-apply leaves the row claimed. Without this
        the item would be unprocessable forever — the same data loss as
        dropping it, but harder to notice, because the queue looks calm.
        """
        if self.sync_status != self.STATUS_PROCESSING or not self.claimed_at:
            return True
        timeout = getattr(settings, 'SYNC_CLAIM_TIMEOUT_SECONDS', 300)
        return (timezone.now() - self.claimed_at).total_seconds() > timeout

    def mark_processing(self):
        self.sync_status = self.STATUS_PROCESSING
        self.claimed_at = timezone.now()
        self.save(update_fields=['sync_status', 'claimed_at', 'updated_at'])

    def mark_synced(self, target_model, target_id):
        self.sync_status = self.STATUS_SYNCED
        self.target_model = target_model or ''
        self.target_id = str(target_id) if target_id else ''
        self.last_error = ''
        self.claimed_at = None
        self.processed_at = timezone.now()
        self.synced_at = timezone.now()
        self.save(update_fields=[
            'sync_status', 'target_model', 'target_id', 'last_error',
            'claimed_at', 'processed_at', 'synced_at', 'updated_at',
        ])

    def mark_failed(self, message):
        self.sync_status = self.STATUS_FAILED
        # The retry count is incremented here, once per failed attempt, and
        # `exhausted` is derived from it rather than stored. A stored flag
        # would need clearing on a manual retry and would drift.
        self.retry_count = self.retry_count + 1
        self.last_error = (message or '')[:2000]
        self.claimed_at = None
        self.processed_at = timezone.now()
        self.save(update_fields=[
            'sync_status', 'retry_count', 'last_error', 'claimed_at',
            'processed_at', 'updated_at',
        ])
