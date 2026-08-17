from django.urls import path
from .views import AgencyProfileView, QuickActionsSummaryView

urlpatterns = [
    path('agency-profile/', AgencyProfileView.as_view(), name='agency-profile'),
    path('dashboard/quick-actions/', QuickActionsSummaryView.as_view(), name='quick-actions-summary'),
]
