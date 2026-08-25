from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, EmailDeliveryViewSet

router = DefaultRouter()
router.register(r'deliveries', EmailDeliveryViewSet, basename='email-delivery')
router.register(r'', NotificationViewSet, basename='notification')

urlpatterns = [
    path('', include(router.urls)),
]
