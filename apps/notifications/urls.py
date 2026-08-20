from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, NotificationPreferenceViewSet

router = DefaultRouter()
router.register(r'preferences', NotificationPreferenceViewSet, basename='notification-preferences')
router.register(r'', NotificationViewSet, basename='notifications')

urlpatterns = [
    path('', include(router.urls)),
]
