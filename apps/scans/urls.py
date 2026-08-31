from django.urls import path
from .views import (
    StartSessionView, SubmitMetadataView, FinalizeUploadView,
    ScanDetailView, ScanStatusView, ProjectScansView,
    UploadLidarView, UploadRgbView, UploadThermalView, UploadGpsView, UploadGaussianSplatView, UploadBimView,
    StartAiProcessingView, AiProcessingStatusView, DefectsListView, StreamAiProcessingView,
    ThermalAnomaliesListView, AlignBimView, DeviationAnalysisView, StreamBimAlignmentView,
    ClashDetectionView, ProgressValidationView, ListSessionsView, ScanFilesListView,
    SyncTrimbleConnectView, TrimbleAuthView, TrimbleCallbackView, ComplianceCheckListView,
    FleetStatusView, GnssTelemetryView, ComplianceCertificateView,
    DefectDetailView, ThermalAnomalyDetailView, DeviationHeatmapView, QAInsightsView,
    IntegrationSettingsView, DeleteScanFileView, StopWorkFlagView, ApiDocsView,
    ScanFileContentView
)
from apps.audit.views import SessionTimelineView

urlpatterns = [
    path('integration-settings/', IntegrationSettingsView.as_view(), name='integration_settings'),
    path('qa-insights/', QAInsightsView.as_view(), name='qa_insights'),
    path('compliance-checks/', ComplianceCheckListView.as_view(), name='compliance_checks_list'),
    path('session/', StartSessionView.as_view(), name='start_session'),
    path('sessions/', ListSessionsView.as_view(), name='list_sessions'),

    # Fleet and telemetry. These MUST stay above the '<str:session_id>/'
    # catch-all below, which otherwise swallows any single-segment path.
    path('fleet/', FleetStatusView.as_view(), name='fleet_status'),
    path('gnss-telemetry/', GnssTelemetryView.as_view(), name='gnss_telemetry'),
    path('docs/', ApiDocsView.as_view(), name='api_docs'),

    # Trimble OAuth2 — also above the catch-all, which previously made these
    # unreachable (they resolved to ScanDetailView and 404'd).
    path('trimble-auth/', TrimbleAuthView.as_view(), name='trimble_auth'),
    path('trimble-callback/', TrimbleCallbackView.as_view(), name='trimble_callback'),
    path('trimble-callback', TrimbleCallbackView.as_view(), name='trimble_callback_no_slash'),

    # Session data and status
    path('<str:session_id>/', ScanDetailView.as_view(), name='scan_detail'),
    path('<str:session_id>/status/', ScanStatusView.as_view(), name='scan_status'),
    path('<str:session_id>/timeline/', SessionTimelineView.as_view(), name='scan_timeline'),
    path('project/<str:project_id>/', ProjectScansView.as_view(), name='project_scans'),

    # Uploads
    path('<str:session_id>/metadata/', SubmitMetadataView.as_view(), name='submit_metadata'),
    path('<str:session_id>/finalize/', FinalizeUploadView.as_view(), name='finalize_upload'),
    path('<str:session_id>/upload/lidar/', UploadLidarView.as_view(), name='upload_lidar'),
    path('<str:session_id>/upload/rgb/', UploadRgbView.as_view(), name='upload_rgb'),
    path('<str:session_id>/upload/thermal/', UploadThermalView.as_view(), name='upload_thermal'),
    path('<str:session_id>/upload/gps/', UploadGpsView.as_view(), name='upload_gps'),
    path('<str:session_id>/upload/gaussian_splat/', UploadGaussianSplatView.as_view(), name='upload_gaussian_splat'),
    path('<str:session_id>/upload/bim/', UploadBimView.as_view(), name='upload_bim'),
    path('<str:session_id>/files/', ScanFilesListView.as_view(), name='list_scan_files'),

    # AI Processing
    path('<str:session_id>/process/', StartAiProcessingView.as_view(), name='start_ai_processing'),
    path('<str:session_id>/process/stream/', StreamAiProcessingView.as_view(), name='stream_ai_processing'),
    path('<str:session_id>/processing-status/', AiProcessingStatusView.as_view(), name='ai_processing_status'),
    path('<str:session_id>/defects/', DefectsListView.as_view(), name='list_defects'),
    path('<str:session_id>/defects/<str:defect_id>/', DefectDetailView.as_view(), name='defect_detail'),
    path('<str:session_id>/thermal-anomalies/', ThermalAnomaliesListView.as_view(), name='list_thermal_anomalies'),
    path('<str:session_id>/thermal-anomalies/<str:anomaly_id>/', ThermalAnomalyDetailView.as_view(), name='thermal_anomaly_detail'),

    # BIM Integration
    path('<str:session_id>/align-bim/', AlignBimView.as_view(), name='align_bim'),
    path('<str:session_id>/align-bim/stream/', StreamBimAlignmentView.as_view(), name='stream_bim_alignment'),
    path('<str:session_id>/deviation/', DeviationAnalysisView.as_view(), name='deviation_analysis'),
    path('<str:session_id>/heatmap/', DeviationHeatmapView.as_view(), name='deviation_heatmap'),
    path('<str:session_id>/clash/', ClashDetectionView.as_view(), name='clash_detection'),
    path('<str:session_id>/progress/', ProgressValidationView.as_view(), name='progress_validation'),
    path('<str:session_id>/sync-trimble/', SyncTrimbleConnectView.as_view(), name='sync_trimble_connect'),

    # Compliance certificates
    path('<str:session_id>/compliance-certificate/', ComplianceCertificateView.as_view(), name='compliance_certificate'),
    path('<str:session_id>/stop-work-flag/', StopWorkFlagView.as_view(), name='stop_work_flag'),
    path('<str:session_id>/files/<str:file_id>/', DeleteScanFileView.as_view(), name='delete_scan_file'),
    path('<str:session_id>/files/<str:file_id>/content/', ScanFileContentView.as_view(), name='scan_file_content'),
]

from rest_framework.routers import DefaultRouter
from .views import ScanPlanViewSet, ScannerViewSet

router = DefaultRouter()
router.register(r'plans', ScanPlanViewSet, basename='scan_plan')
router.register(r'scanners', ScannerViewSet, basename='scanner')

urlpatterns = router.urls + urlpatterns

