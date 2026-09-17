from django.urls import path

from . import views

# The spec lists its import routes without a trailing slash
# (`/api/v1/import/{batch_id}/validate`). Every other route on this platform
# carries one, and `APPEND_SLASH` only rescues a GET — a POST to the spec's
# exact spelling would 404 in production, which is the worst possible way for a
# documented contract to be wrong. Both spellings are therefore registered, to
# the same view and the same name, so a client written against the spec and a
# client written against the rest of this API both work.
urlpatterns = [
    # Literal segments first. `templates/` and `record-types/` cannot collide
    # with `uuid:batch_id` (the converters differ), but the ordering is kept so
    # the file reads the way `scans/urls.py` and `inspections/urls.py` do —
    # both of which record a shadowing bug that this ordering prevents.
    path('upload/', views.ImportUploadView.as_view(), name='import-upload'),
    path('record-types/', views.ImportRecordTypeListView.as_view(),
         name='import-record-types'),
    path('batches/', views.ImportBatchListView.as_view(), name='import-batch-list'),
    path('templates/<str:record_type>/', views.ImportTemplateView.as_view(),
         name='import-template'),
    path('templates/<str:record_type>', views.ImportTemplateView.as_view(),
         name='import-template-noslash'),

    path('<uuid:batch_id>/validate/', views.ImportValidateView.as_view(),
         name='import-validate'),
    path('<uuid:batch_id>/commit/', views.ImportCommitView.as_view(),
         name='import-commit'),
    path('<uuid:batch_id>/status/', views.ImportStatusView.as_view(),
         name='import-status'),

    # The spec's exact spellings.
    path('<uuid:batch_id>/validate', views.ImportValidateView.as_view(),
         name='import-validate-noslash'),
    path('<uuid:batch_id>/commit', views.ImportCommitView.as_view(),
         name='import-commit-noslash'),
    path('<uuid:batch_id>/status', views.ImportStatusView.as_view(),
         name='import-status-noslash'),
]
