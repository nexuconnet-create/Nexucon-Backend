from django.urls import path

from . import views

# Literal paths are declared before any converter route so a literal segment
# can never be swallowed by a catch-all. Every session route uses the `uuid:`
# converter, so `sessions/`, `devices/` and `device-tokens/` cannot be
# mistaken for an id.
urlpatterns = [
    path('session/start/', views.TelemetrySessionStartView.as_view(),
         name='telemetry-session-start'),
    path('session/from-file/', views.TelemetrySessionFromFileView.as_view(),
         name='telemetry-session-from-file'),
    path('session/<uuid:session_id>/data/', views.TelemetryPacketAppendView.as_view(),
         name='telemetry-session-data'),
    path('session/<uuid:session_id>/end/', views.TelemetrySessionEndView.as_view(),
         name='telemetry-session-end'),
    path('session/<uuid:session_id>/status/', views.TelemetrySessionStatusView.as_view(),
         name='telemetry-session-status'),
    path('sessions/', views.TelemetrySessionListView.as_view(),
         name='telemetry-session-list'),
    path('devices/', views.TelemetryDeviceListView.as_view(),
         name='telemetry-device-list'),
    path('device-tokens/', views.DeviceTokenListCreateView.as_view(),
         name='telemetry-device-token-list'),
    path('device-tokens/<uuid:token_id>/revoke/',
         views.DeviceTokenRevokeView.as_view(),
         name='telemetry-device-token-revoke'),
]
