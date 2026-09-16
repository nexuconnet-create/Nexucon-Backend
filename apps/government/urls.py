from django.urls import path
from .views import AgencyProfileView, QuickActionsSummaryView
from .inspector_views import InspectorDashboardView

urlpatterns = [
    path('agency-profile/', AgencyProfileView.as_view(), name='agency-profile'),
    path('dashboard/quick-actions/', QuickActionsSummaryView.as_view(), name='quick-actions-summary'),
    path('inspectors/me/dashboard/', InspectorDashboardView.as_view(), name='inspector-me-dashboard'),
]

