from django.urls import path

from . import views

# Three literal paths and no converter routes. The spec's contract is exactly
# these three, and adding a `<uuid:...>` detail route beside them would be the
# first step towards a queue that is browsed rather than flushed.
urlpatterns = [
    path('queue/', views.SyncEnqueueView.as_view(), name='sync-queue'),
    path('process/', views.SyncProcessView.as_view(), name='sync-process'),
    path('status/', views.SyncStatusView.as_view(), name='sync-status'),
]
