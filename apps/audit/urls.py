from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AuditEventViewSet

router = DefaultRouter()
router.register(r'events', AuditEventViewSet, basename='audit-events')

from .views import SessionTimelineView
urlpatterns = [
    path('timeline/<uuid:session_id>/', SessionTimelineView.as_view(), name='session-timeline'),

    path('', include(router.urls)),
]
