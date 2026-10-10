from django.urls import path, include
from rest_framework.routers import DefaultRouter
<<<<<<< Updated upstream
from .views import (
    NotificationViewSet, EmailDeliveryViewSet, NotificationPreferenceViewSet,
    WebhookEndpointViewSet, PushDeviceTokenViewSet,
)
=======
from .views import NotificationViewSet, NotificationPreferenceViewSet, WebhookEndpointViewSet
>>>>>>> Stashed changes

# NOTE: NotificationPreferenceViewSet is intentionally NOT registered.
# /preferences/ is served by NotificationViewSet's `preferences` @action so a
# GET returns the caller's single preference object (dict), not a list — the
# contract both the frontend and the notification preference tests rely on.
router = DefaultRouter()
<<<<<<< Updated upstream
router.register(r'deliveries', EmailDeliveryViewSet, basename='email-delivery')
router.register(r'webhooks', WebhookEndpointViewSet, basename='webhook')
router.register(r'push-devices', PushDeviceTokenViewSet, basename='push-devices')
=======
router.register(r'webhooks', WebhookEndpointViewSet, basename='webhook')
router.register(r'preferences', NotificationPreferenceViewSet, basename='notification-preferences')
>>>>>>> Stashed changes
router.register(r'', NotificationViewSet, basename='notifications')


urlpatterns = [
    path('', include(router.urls)),
]
