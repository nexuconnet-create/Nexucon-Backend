from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import GenerateReportView, DownloadReportView, QualityReportListView, ReportTemplateViewSet

router = DefaultRouter()
router.register(r'report-templates', ReportTemplateViewSet, basename='report_template')

urlpatterns = router.urls + [
    path('quality-reports/', QualityReportListView.as_view(), name='quality_reports'),
    path('scans/<str:session_id>/report/', GenerateReportView.as_view(), name='generate_report'),
    path('scans/<str:session_id>/report/<str:file_format>/', DownloadReportView.as_view(), name='download_report'),
]
