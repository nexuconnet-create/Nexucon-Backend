from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, EmailDeliveryViewSet, NotificationPreferenceViewSet, WebhookEndpointViewSet

router = DefaultRouter()
router.register(r'deliveries', EmailDeliveryViewSet, basename='email-delivery')
router.register(r'webhooks', WebhookEndpointViewSet, basename='webhook')
router.register(r'preferences', NotificationPreferenceViewSet, basename='notification-preferences')
router.register(r'', NotificationViewSet, basename='notifications')


urlpatterns = [
    path('', include(router.urls)),
]
