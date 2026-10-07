from django.contrib import admin

from .models import SyncQueueItem


@admin.register(SyncQueueItem)
class SyncQueueItemAdmin(admin.ModelAdmin):
    """Read-mostly. The queue is written by devices, and an operator editing a
    payload here would be editing a field capture on someone else's behalf.

    Failed items are the ones worth looking at, so the list is filterable by
    status and the error is shown inline.
    """

    list_display = ['reference', 'inspector', 'entity_type', 'action',
                    'sync_status', 'retry_count', 'target_model', 'queued_at']
    list_filter = ['sync_status', 'entity_type', 'action']
    search_fields = ['client_item_id', 'entity_id', 'inspector__email',
                     'target_id', 'last_error']
    readonly_fields = ['reference', 'payload', 'payload_hash', 'target_model',
                       'target_id', 'queued_at', 'claimed_at', 'processed_at',
                       'synced_at', 'created_at', 'updated_at']
    date_hierarchy = 'queued_at'

    def has_add_permission(self, request):
        # Items are created by the enqueue endpoint, under the inspector's own
        # identity. An item added here would be attributed to whoever the admin
        # picked rather than to the device that captured it.
        return False
