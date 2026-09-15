from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ArchivedReportDownloadView, ArchivedReportListView,
    DownloadReportView, GenerateReportView, InspectionReportView,
    NCRReportView, NDTReportPreviewSectionsView, NDTReportPreviewView,
    NDTReportView, NDTWordExportView,
    ProjectIntelligenceReportView, QualityReportListView,
    ReportBrandingView, ReportCMSPasswordView, ReportCMSSectionView,
    ReportCMSSectionsView, ReportMapView, ReportSignOffView,
    ReportTemplateViewSet, ReportVerifyDownloadView, ReportVerifyView,
)
from .versions import ReportVersionView

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
    path('reports/projects/<uuid:project_id>/ndt-report-word/',
         NDTWordExportView.as_view(), name='project-ndt-report-word'),
    path('reports/projects/<uuid:project_id>/archived-reports/',
         ArchivedReportListView.as_view(), name='project-archived-reports'),
    path('reports/archived-reports/<uuid:report_id>/download/',
         ArchivedReportDownloadView.as_view(), name='archived-report-download'),
    path('reports/inspections/<uuid:inspection_id>/report/',
         InspectionReportView.as_view(), name='inspection-report'),
    path('reports/ncrs/<uuid:ncr_id>/report/',
         NCRReportView.as_view(), name='ncr-report'),
    # Report CMS (8 Sep meeting H7): password-protected editable template
    # sections + Word export. The app's URLs mount at the API root, so the
    # 'reports/' prefix keeps every reports endpoint under
    # /api/v1/reports/.
    path('reports/cms/sections/', ReportCMSSectionsView.as_view(),
         name='report-cms-sections'),
    path('reports/cms/sections/<str:key>/', ReportCMSSectionView.as_view(),
         name='report-cms-section'),
    path('reports/cms/password/', ReportCMSPasswordView.as_view(),
         name='report-cms-password'),
    # REFINED EXECUTIVE SUMMARY (11 Sep 2026): public verification of an
    # archived dossier (the cover QR resolves here), preview-before-generate,
    # per-project logo/watermark branding, and interactive-map data.
    path('reports/verify/', ReportVerifyView.as_view(),
         name='report-verify'),
    path('reports/verify/download/', ReportVerifyDownloadView.as_view(),
         name='report-verify-download'),
    path('reports/projects/<uuid:project_id>/ndt-report-preview/',
         NDTReportPreviewView.as_view(), name='project-ndt-report-preview'),
    # §2.1 preview sidebar: section→page map + page count + the preview PDF
    # itself, from one render pass.
    path('reports/projects/<uuid:project_id>/ndt-report-preview/sections/',
         NDTReportPreviewSectionsView.as_view(),
         name='project-ndt-report-preview-sections'),
    path('reports/projects/<uuid:project_id>/branding/',
         ReportBrandingView.as_view(), name='project-report-branding'),
    # Approving-engineer COREN credentials on the sign-off (C11, 4 Sep).
    path('reports/projects/<uuid:project_id>/signoff/',
         ReportSignOffView.as_view(), name='project-report-signoff'),
    path('reports/projects/<uuid:project_id>/map/',
         ReportMapView.as_view(), name='project-report-map'),
         
    # Report Versions API
    path('reports/archived-reports/<uuid:report_id>/versions/',
         ReportVersionView.as_view(), name='archived-report-versions'),
]
