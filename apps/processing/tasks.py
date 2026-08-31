import logging
from celery import shared_task
from django.apps import apps

logger = logging.getLogger(__name__)

@shared_task(name="apps.processing.tasks.process_scan_pipeline")
def process_scan_pipeline(session_id: str):
    """
    Asynchronous Celery task pipeline representing full scan post-processing:
    1. Spatial Alignment & Fusion (LiDAR + RTK GPS + RGB)
    2. Thermal overlay registration
    3. AI defect/anomaly detection
    4. Auto QA/QC report generation
    """
    logger.info(f"Starting async processing pipeline for ScanSession: {session_id}")
    
    ScanSession = apps.get_model('scans', 'ScanSession')
    Defect = apps.get_model('scans', 'Defect')
    ProcessingTask = apps.get_model('scans', 'ProcessingTask')
    
    try:
        session = ScanSession.objects.get(id=session_id)
    except ScanSession.DoesNotExist:
        logger.error(f"ScanSession {session_id} not found.")
        return False
        
    session.status = 'processing'
    session.save()

    # Track processing task progress
    task = ProcessingTask.objects.create(
        session=session,
        task_type='ai_analysis',
        status='in_progress'
    )

    from apps.audit.services import AuditService
    AuditService.log_event(
        event_type='processing_started',
        entity_type='processing_task',
        entity_id=task.id,
        session_id=session.id,
        new_value='in_progress',
        description='AI analysis processing pipeline started.',
    )

    # Initialize channels layer for WebSocket streaming
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()

    def broadcast_ws_status(status, message):
        try:
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f"processing_{session_id}",
                    {
                        "type": "processing_status_update",
                        "status": status,
                        "message": message
                    }
                )
        except Exception as ws_err:
            logger.error(f"Failed to send WS status update: {ws_err}")

    broadcast_ws_status("processing", "Scan processing pipeline started.")

    try:
        # Import services dynamically to avoid circular dependencies
        from apps.scans.services import DataFusionService
        from apps.reports.services import ReportService
        from apps.scans.models import ThermalAnomaly, Defect

        # Clean up existing results before reprocessing
        ThermalAnomaly.objects.filter(session=session).delete()
        Defect.objects.filter(session=session).delete()
        # The stored QA/QC report snapshots the old AI findings - drop it so
        # the next "Generate Report" click rebuilds from the fresh data.
        from apps.reports.models import QualityReport
        QualityReport.objects.filter(scan=session).delete()

        # 1. Run point cloud to BIM alignment and Progress calculation
        broadcast_ws_status("processing", "Aligning LiDAR scan with BIM model...")
        DataFusionService.align_lidar_to_bim(session)
        broadcast_ws_status("processing", "LiDAR to BIM alignment completed.")

        broadcast_ws_status("processing", "Calculating scan progress...")
        DataFusionService.calculate_progress(session)
        broadcast_ws_status("processing", "Scan progress calculation completed.")

        # 2. Register thermal images overlay
        broadcast_ws_status("processing", "Registering thermal overlay images...")
        DataFusionService.apply_thermal_overlay(session)
        broadcast_ws_status("processing", "Thermal overlay registration completed.")

        # 3. AI Defect Detection
        broadcast_ws_status("processing", "Running AI visual and thermal defect detection...")
        from apps.common.ai_service import AIService
        
        rgb_file = session.files.filter(file_type='rgb').first()
        thermal_file = session.files.filter(file_type='thermal').first()
        
        # Re-signed on demand — stored URLs carry signatures that expire ~1h after upload.
        from apps.scans.utils import refresh_storage_url
        rgb_url = refresh_storage_url(session.rgb_url) or refresh_storage_url(rgb_file.file_url if rgb_file else None)
        thermal_url = refresh_storage_url(session.thermal_url) or refresh_storage_url(thermal_file.file_url if thermal_file else None)
        
        detected_defects = []
        if rgb_url:
            detected_defects.extend(AIService.detect_visual_defects(rgb_url))
            
        # Multimodal and Thermal Analysis
        if thermal_url:
            if rgb_url:
                # Multimodal analysis (CNN + SNN)
                delaminations = AIService.detect_delamination_multimodal(thermal_url, rgb_url)
                for d in delaminations:
                    # Append them to detected_defects so they are saved to Defect model
                    detected_defects.append(d)
        
        for d in detected_defects:
            loc_x = d.get("location_x", 0.0)
            loc_y = d.get("location_y", 0.0)
            loc_z = d.get("location_z", 0.0)

            from apps.scans.utils import extract_image_bbox
            Defect.objects.get_or_create(
                session=session,
                type=d.get("type", "crack"),
                defaults={
                    "severity": d.get("severity", "medium"),
                    "status": "OPEN",
                    "location_x": loc_x,
                    "location_y": loc_y,
                    "location_z": loc_z,
                    "description": d.get("description", ""),
                    "confidence_score": d.get("confidence_score"),
                    "is_false_positive": d.get("is_false_positive", False),
                    "grid_zone": d.get("grid_zone", "") or None,
                    **extract_image_bbox(d)
                }
            )

        broadcast_ws_status("processing", f"AI analysis completed. Found {len(detected_defects)} potential issues.")

        task.status = 'completed'
        task.result_data = {"status": "success", "defects_detected": len(detected_defects)}
        task.save()

        # Update session status
        session.status = 'completed'
        session.save()

        # 4. Trigger Auto QA/QC PDF Report generation
        broadcast_ws_status("processing", "Generating automated QA/QC PDF report...")
        logger.info(f"Triggering automated report generation for scan: {session_id}")
        ReportService.generate_qaqc_report(session)
        broadcast_ws_status("processing", "Automated QA/QC report generation completed.")

        # 5. Push data to Trimble Connect
        try:
            broadcast_ws_status("processing", "Syncing inspection data to Trimble Connect...")
            from apps.common.trimble_service import TrimbleConnectService
            logger.info(f"Syncing inspection data to Trimble Connect for session: {session_id}")
            defect_csv = TrimbleConnectService.generate_defect_csv(session)
            summary_json = TrimbleConnectService.generate_inspection_summary(session)
            ai_overlay_json = TrimbleConnectService.generate_ai_overlay_json(session)
            from apps.scans.utils import refresh_storage_url
            TrimbleConnectService.upload_files_to_trimble(
                session,
                defect_csv,
                summary_json,
                ai_overlay_json=ai_overlay_json,
                thermal_orthomosaic_url=refresh_storage_url(session.thermal_url)
            )
            broadcast_ws_status("processing", "Trimble Connect synchronization completed.")
        except Exception as trimble_e:
            logger.error(f"Failed to sync data to Trimble Connect: {trimble_e}")
            broadcast_ws_status("processing", f"Trimble Connect sync warning: {trimble_e}")

        AuditService.log_event(
            event_type='processing_completed',
            entity_type='scan_session',
            entity_id=session.id,
            session_id=session.id,
            old_value='processing',
            new_value='completed',
            description='Full processing pipeline completed successfully.',
        )

        # Dispatch webhooks
        try:
            from apps.notifications.tasks import dispatch_webhooks
            payload = {
                "session_id": str(session.id),
                "status": "completed",
                "message": "Processing pipeline finished."
            }
            dispatch_webhooks.delay('scan_processing_completed', payload, str(session.project.id) if session.project else None)
        except Exception as webhook_e:
            logger.error(f"Failed to queue webhook dispatch: {webhook_e}")

        broadcast_ws_status("completed", "Full processing pipeline completed successfully.")
        return True

    except Exception as e:
        logger.error(f"Error executing scan processing pipeline: {e}", exc_info=True)
        session.status = 'failed'
        session.save()
        task.status = 'failed'
        task.result_data = {"error": str(e)}
        task.save()
        AuditService.log_event(
            event_type='processing_failed',
            entity_type='scan_session',
            entity_id=session.id,
            session_id=session.id,
            old_value='processing',
            new_value='failed',
            description=f'Processing pipeline failed: {e}',
        )

        # Dispatch webhooks
        try:
            from apps.notifications.tasks import dispatch_webhooks
            payload = {
                "session_id": str(session.id),
                "status": "failed",
                "error": str(e)
            }
            dispatch_webhooks.delay('scan_processing_failed', payload, str(session.project.id) if session.project else None)
        except Exception as webhook_e:
            logger.error(f"Failed to queue webhook dispatch: {webhook_e}")

        broadcast_ws_status("failed", f"Processing pipeline failed: {str(e)}")
        return False
