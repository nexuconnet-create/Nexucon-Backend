from django.contrib import admin
from .models import Notification, NotificationPreference, WebhookEndpoint

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    pass

@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    pass

@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    pass

