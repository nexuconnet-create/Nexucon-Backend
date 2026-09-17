from django.urls import path
from .views import AgencyProfileView, QuickActionsSummaryView
from .inspector_views import InspectorDashboardView
from .inspector_accreditation_views import (
    InspectorDetailView, InspectorListCreateView, InspectorMeView,
)

urlpatterns = [
    path('agency-profile/', AgencyProfileView.as_view(), name='agency-profile'),
    path('dashboard/quick-actions/', QuickActionsSummaryView.as_view(), name='quick-actions-summary'),
    path('inspectors/me/dashboard/', InspectorDashboardView.as_view(), name='inspector-me-dashboard'),

    # `inspectors/me/` MUST be declared before `inspectors/<uuid:...>/`. Both
    # are literals here so Django's resolver would get it right anyway, but the
    # ordering rule is kept explicit because the uuid: converter is what makes
    # it safe — a `str:` converter would let `me` reach the detail view.
    #
    # These names are prefixed `government-` because `apps.stakeholders`
    # registers a DefaultRouter basename='inspector', which already claims
    # `inspector-list` and `inspector-detail` for the contractor-side inspector
    # directory. Django resolves duplicate route names last-registered-wins, so
    # an unprefixed `inspector-list` here would have made
    # `reverse('inspector-list')` silently point at the stakeholders endpoint.
    path('inspectors/me/', InspectorMeView.as_view(), name='government-inspector-me'),
    path('inspectors/', InspectorListCreateView.as_view(),
         name='government-inspector-list'),
    path('inspectors/<uuid:inspector_id>/', InspectorDetailView.as_view(),
         name='government-inspector-detail'),
]
