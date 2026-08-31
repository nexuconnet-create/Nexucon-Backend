from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    EdgeSyncView, NodeStatusView, AIFeedbackStatsView, AIModelViewSet,
)

router = DefaultRouter()
router.register(r'ai-models', AIModelViewSet, basename='ai_model')

urlpatterns = router.urls + [
    path('scans/<str:session_id>/edge-sync/', EdgeSyncView.as_view(), name='edge_sync'),
    path('node-status/', NodeStatusView.as_view(), name='node_status'),
    path('ai-feedback-stats/', AIFeedbackStatsView.as_view(), name='ai_feedback_stats'),
]
