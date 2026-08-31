import logging
from celery import shared_task
from django.apps import apps

logger = logging.getLogger(__name__)

@shared_task(name="apps.reports.tasks.generate_report_task")
def generate_report_task(session_id: str):
    """
    Async Celery task to compile quality report for a completed ScanSession.
    """
    logger.info(f"Async QA/QC report generation triggered for ScanSession: {session_id}")
    
    ScanSession = apps.get_model('scans', 'ScanSession')
    QualityReport = apps.get_model('reports', 'QualityReport')
    
    try:
        session = ScanSession.objects.get(id=session_id)
    except ScanSession.DoesNotExist:
        logger.error(f"ScanSession {session_id} not found.")
        return False

    # Initialize a pending report
    report, created = QualityReport.objects.get_or_create(
        scan=session,
        defaults={
            'project_id': session.project_id,
            'status': 'generating'
        }
    )
    if not created:
        report.status = 'generating'
        report.save()

    try:
        from apps.reports.services import ReportService
        ReportService.generate_qaqc_report(session)
        return True
    except Exception as e:
        logger.error(f"Failed to generate report asynchronously: {e}")
        report.status = 'failed'
        report.save()
        return False
