from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/v1/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/v1/health/', include('common.urls')),
    # Core Apps
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/government/', include('apps.government.urls')),
    path('api/v1/projects/', include('apps.projects.urls')),
    path('api/v1/applications/', include('apps.applications.urls')),
    path('api/v1/permits/', include('apps.permits.urls')),
    path('api/v1/inspections/', include('apps.inspections.urls')),
    path('api/v1/documents/', include('apps.documents.urls')),
    path('api/v1/stakeholders/', include('apps.stakeholders.urls')),
    
    # New Client Requests Apps
    path('api/v1/emergency/', include('apps.emergency.urls')),
    path('api/v1/public-portal/', include('apps.public_portal.urls')),
]
