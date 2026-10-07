from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/v1/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('swagger/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui-alias'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-docs-alias'),
    path('redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    path('api/v1/health/', include('common.urls')),
    # Core Apps
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/government/', include('apps.government.urls')),
    path('api/v1/projects/', include('apps.projects.urls')),
    path('api/v1/applications/', include('apps.applications.urls')),
    path('api/v1/permits/', include('apps.permits.urls')),
    path('api/v1/inspections/', include('apps.inspections.urls')),
    path('api/v1/monitoring/', include('apps.monitoring.urls')),
    path('api/v1/bim/', include('apps.bim.urls')),
    path('api/v1/documents/', include('apps.documents.urls')),
    path('api/v1/compliance/', include('apps.compliance.urls')),
    path('api/v1/approvals/', include('apps.approvals.urls')),
    path('api/v1/analytics/', include('apps.analytics.urls')),
    path('api/v1/notifications/', include('apps.notifications.urls')),
    path('api/v1/audit/', include('apps.audit.urls')),
    path('api/v1/stakeholders/', include('apps.stakeholders.urls')),
    path('api/v1/integrations/', include('apps.settings.urls')),
    path('api/v1/settings/', include('apps.settings.urls')),
    
    # New Client Requests Apps
    path('api/v1/emergency/', include('apps.emergency.urls')),
    path('api/v1/public-portal/', include('apps.public_portal.urls')),
    path('api/v1/public/', include('apps.public_portal.urls')),
    
    # Migrated Apps
    path('api/v1/scans/', include('apps.scans.urls')),
    path('api/v1/digital-eye/', include('apps.digital_eye.urls')),
    path('api/v1/', include('apps.reports.urls')),
    path('api/v1/processing/', include('apps.processing.urls')),

    # Evidence Intelligence
    # NOTE: apps.digital_eye.urls is included once, above, under "Migrated Apps".
    # It was previously included a second time here, which registered every
    # digital-eye route name twice and emitted duplicate drf-spectacular
    # operations. The first registration always won resolution, so removing the
    # duplicate changes no response — only the schema and the route table.
    path('api/v1/evidence/', include('apps.evidence.urls')),

    # Inspector PWA — dual-path ingestion (live telemetry sessions).
    # The manual-import path (apps.data_import) registers under /import/.
    path('api/v1/telemetry/', include('apps.telemetry.urls')),

    # Inspector PWA — the offline replay queue. Its own app rather than a
    # module of telemetry: `entity_type` spans inspections, findings, stop
    # work orders, evidence and telemetry, so telemetry is a peer here and not
    # a parent, and the two have opposite dependency directions.
    path('api/v1/sync/', include('apps.sync.urls')),

    # Inspector PWA — manual import (CSV/JSON/PDF upload, validate, commit).
    # The app is `data_import` because a module named `import` is a syntax
    # error; the URL prefix stays `import/`, because a URL string is not a
    # Python identifier and the spec's contract is the URL.
    path('api/v1/import/', include('apps.data_import.urls')),
]

from django.conf import settings
from django.conf.urls.static import static
from django.urls import re_path
from django.views.static import serve

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    ]
