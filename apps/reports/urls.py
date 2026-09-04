from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ArchivedReportDownloadView, ArchivedReportListView,
    DownloadReportView, GenerateReportView, InspectionReportView,
    NCRReportView, NDTReportView, ProjectIntelligenceReportView,
    QualityReportListView, ReportTemplateViewSet,
)

router = DefaultRouter()
router.register(r'report-templates', ReportTemplateViewSet, basename='report_template')

urlpatterns = router.urls + [
    path('quality-reports/', QualityReportListView.as_view(), name='quality_reports'),
    path('scans/<str:session_id>/report/', GenerateReportView.as_view(), name='generate_report'),
    path('scans/<str:session_id>/report/<str:file_format>/', DownloadReportView.as_view(), name='download_report'),

    # AI Report Generator (plan §5 Weeks 5–6)
    path('reports/projects/<uuid:project_id>/intelligence-report/',
         ProjectIntelligenceReportView.as_view(), name='project-intelligence-report'),
    path('reports/projects/<uuid:project_id>/ndt-report/',
         NDTReportView.as_view(), name='project-ndt-report'),
    path('reports/projects/<uuid:project_id>/archived-reports/',
         ArchivedReportListView.as_view(), name='project-archived-reports'),
    path('reports/archived-reports/<uuid:report_id>/download/',
         ArchivedReportDownloadView.as_view(), name='archived-report-download'),
    path('reports/inspections/<uuid:inspection_id>/report/',
         InspectionReportView.as_view(), name='inspection-report'),
    path('reports/ncrs/<uuid:ncr_id>/report/',
         NCRReportView.as_view(), name='ncr-report'),
]
