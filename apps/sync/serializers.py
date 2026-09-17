"""Sync queue serializers.

The request serializer is deliberately permissive where the service is strict.
``entity_type`` and ``action`` are choices so a client can read the vocabulary
out of the schema, but the rules that depend on *both* — which actions a given
entity type accepts — are enforced in ``SyncService.enqueue``, because that is
the only place that can say "this type supports CREATE and UPDATE, and DELETE
is not something the field does" in one message.
"""
from rest_framework import serializers

from .appliers import KNOWN_ACTIONS, REGISTRY
from .models import SyncQueueItem


class EnqueueRequestSerializer(serializers.Serializer):
    """The spec's `{ entity_type, entity_id, action, payload }` plus a key.

    ``client_item_id`` is the one field the spec's contract does not list. It
    is required anyway: without a client-generated idempotency key there is no
    way to tell a retry from a second write, and a reconnecting PWA retries
    constantly. The alternative — trusting the client not to resend — is a
    duplicate finding in a statutory registry.
    """

    client_item_id = serializers.CharField(
        max_length=128,
        help_text='Client-generated key, unique per inspector. Resending the '
                  'same key with the same payload is a retry, not a write.',
    )
    entity_type = serializers.ChoiceField(choices=sorted(REGISTRY))
    entity_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default='',
        help_text='The client\'s identifier for the target row. Required for '
                  'an UPDATE.',
    )
    action = serializers.ChoiceField(choices=list(KNOWN_ACTIONS))
    payload = serializers.JSONField()


class SyncQueueItemSerializer(serializers.ModelSerializer):
    """One queue row, as the client that queued it sees it."""

    max_retries = serializers.IntegerField(read_only=True)
    exhausted = serializers.BooleanField(read_only=True)

    class Meta:
        model = SyncQueueItem
        fields = [
            'reference', 'client_item_id', 'entity_type', 'entity_id', 'action',
            'payload', 'payload_hash', 'sync_status', 'retry_count',
            'max_retries', 'exhausted', 'last_error', 'target_model',
            'target_id', 'queued_at', 'processed_at', 'synced_at',
        ]
        read_only_fields = fields


class AppliedItemSerializer(serializers.Serializer):
    """One item's outcome inside a `process/` response.

    Present for every item the run touched, successfully or not, so a client
    never has to infer an item's fate from an absent entry and a count.
    """

    reference = serializers.CharField()
    entity_type = serializers.CharField()
    action = serializers.CharField()
    status = serializers.CharField()
    target_model = serializers.CharField(required=False)
    target_id = serializers.CharField(required=False)
    error = serializers.CharField(allow_null=True, required=False)


class ProcessResponseSerializer(serializers.Serializer):
    """`POST sync/process/` — what this run did.

    `remaining` counts items left pending after the run, so a client can loop
    without re-reading `status/` between passes.
    """

    processed = serializers.IntegerField()
    synced = serializers.IntegerField()
    failed = serializers.IntegerField()
    skipped = serializers.IntegerField()
    remaining = serializers.IntegerField()
    applied = AppliedItemSerializer(many=True)


class ExhaustedItemSerializer(serializers.Serializer):
    """An item past its retry cap.

    Reported, never dropped: the payload and the error both survive so a human
    can decide what to do with the field work that could not land.
    """

    reference = serializers.CharField()
    client_item_id = serializers.CharField()
    entity_type = serializers.CharField()
    action = serializers.CharField()
    retry_count = serializers.IntegerField()
    last_error = serializers.CharField(allow_null=True)
    queued_at = serializers.DateTimeField()


class StatusResponseSerializer(serializers.Serializer):
    """`GET sync/status/` — queue depth and health.

    ``last_synced_at`` is null until something has actually synced. It is not
    the time of the request: an indicator reading "synced just now" before
    anything ever synced is the defect this endpoint replaces.
    """

    counts = serializers.DictField(child=serializers.IntegerField())
    pending = serializers.IntegerField()
    failed = serializers.IntegerField()
    synced = serializers.IntegerField()
    processing = serializers.IntegerField()
    oldest_pending_at = serializers.DateTimeField(allow_null=True)
    last_synced_at = serializers.DateTimeField(allow_null=True)
    max_retries = serializers.IntegerField()
    exhausted = ExhaustedItemSerializer(many=True)
