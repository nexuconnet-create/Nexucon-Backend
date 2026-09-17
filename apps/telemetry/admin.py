from django.contrib import admin

from .models import DeviceToken, TelemetryPacket, TelemetrySession


class TelemetryPacketInline(admin.TabularInline):
    model = TelemetryPacket
    extra = 0
    can_delete = False
    readonly_fields = ['sequence', 'payload', 'previous_hash', 'chain_hash',
                       'recorded_at', 'received_at']

    def has_add_permission(self, request, obj=None):
        # Packets arrive from a device, never from the admin — the log is the
        # evidence of what the instrument sent.
        return False


@admin.register(TelemetrySession)
class TelemetrySessionAdmin(admin.ModelAdmin):
    list_display = ['session_reference', 'device', 'project', 'data_type',
                    'transport', 'status', 'sync_status', 'packet_count',
                    'created_at']
    list_filter = ['data_type', 'transport', 'status', 'sync_status']
    search_fields = ['session_reference', 'device__device_id',
                     'project__name', 'operator_name', 'source_file_name']
    readonly_fields = ['id', 'session_reference', 'sha256_hash', 'data_payload',
                       'packet_count', 'promoted_at', 'created_at', 'updated_at']
    inlines = [TelemetryPacketInline]


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ['device', 'label', 'key_prefix', 'issued_by', 'issued_at',
                    'last_used_at', 'expires_at', 'revoked_at']
    list_filter = ['revoked_at', 'expires_at', 'device__device_type']
    search_fields = ['device__device_id', 'label', 'key_prefix']
    # The secret is not a field, so there is nothing here to expose — only the
    # digest, which is not editable into a working credential.
    readonly_fields = ['id', 'key_prefix', 'hashed_key', 'issued_at',
                       'last_used_at']

    def has_add_permission(self, request):
        # Minted through the API, which is the only path that can show the
        # plaintext to the person who asked for it.
        return False

