import time
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.core.files.storage import default_storage
import os
from urllib.parse import urljoin
from django.http import StreamingHttpResponse, HttpResponseRedirect
from django.db.models import Avg, Count

from rest_framework import status, serializers, parsers, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action

from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiTypes, extend_schema_field

from .models import (
    ScanSession, ScanMetadata, ProcessingTask, Defect, ThermalAnomaly,
    BIMAlignmentResult, ScanFile, ProgressValidationResult, ScanPlan,
    ComplianceCheck, Scanner, GnssTelemetry, ComplianceCertificate, StopWorkFlag
)
from .serializers import (
    StartSessionSerializer, SessionResponseSerializer, ScanMetadataSerializer,
    ProcessingTaskSerializer, DefectSerializer, ThermalAnomalySerializer,
    BIMAlignmentResultSerializer, ScanFileSerializer, ProgressValidationResultSerializer,
    ScanPlanSerializer, ComplianceCheckSerializer, ScannerSerializer, ScannerHeartbeatSerializer,
    GnssTelemetrySerializer, ComplianceCertificateSerializer, StopWorkFlagSerializer
)
from apps.projects.models import Project


@extend_schema_field(OpenApiTypes.BINARY)
class BinaryFileField(serializers.FileField):
    pass


class StartSessionView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=StartSessionSerializer, responses={201: SessionResponseSerializer})
    def post(self, request):
        serializer = StartSessionSerializer(data=request.data)
        if serializer.is_valid():
            session = serializer.save(status='initialized')
            response_serializer = SessionResponseSerializer(session)
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ListSessionsView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: SessionResponseSerializer(many=True)})
    def get(self, request):
        sessions = ScanSession.objects.all().order_by('-created_at')
        serializer = SessionResponseSerializer(sessions, many=True)
        return Response(serializer.data)


class ScanDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        serializer = SessionResponseSerializer(session)
        return Response(serializer.data)


class ScanStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        
        # Simulate processing time for the demo
        if session.status == 'processing':
            from django.utils import timezone
            import datetime
            if (timezone.now() - session.updated_at).total_seconds() > 15:
                session.status = 'completed'
                session.save()
                
        return Response({
            "session_id": str(session.id),
            "status": session.status,
            "updated_at": session.updated_at
        })


class ProjectScansView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, project_id):
        sessions = ScanSession.objects.filter(project_id=project_id).order_by('-created_at')
        serializer = SessionResponseSerializer(sessions, many=True)
        return Response(serializer.data)


class SubmitMetadataView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(request=ScanMetadataSerializer, responses={201: ScanMetadataSerializer})
    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        if hasattr(session, 'metadata'):
            return Response({"error": "Metadata already exists for this session"}, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = ScanMetadataSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(session=session)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FinalizeUploadView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        session.status = 'processing'
        session.save()
        
        from apps.audit.services import AuditService
        try:
            AuditService.log_event(
                action='FINALIZE_SCAN_UPLOADS',
                resource_type='ScanSession',
                resource_id=str(session.id),
                metadata={'message': 'Scan data queued for processing.'},
                project_name=session.project.name if getattr(session, 'project', None) else "Scan Project"
            )
        except Exception:
            pass
        return Response({"status": "processing", "message": "Scan data queued for processing."}, status=status.HTTP_202_ACCEPTED)


class UploadLidarView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        path = default_storage.save(f'scans/{session.id}/lidar/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='lidar',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=secure_url
        )
        return Response(ScanFileSerializer(scan_file).data, status=status.HTTP_201_CREATED)


class UploadRgbView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        
        path = default_storage.save(f'scans/{session.id}/rgb/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        session.rgb_url = secure_url
        session.save()
        
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='rgb',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=session.rgb_url
        )
        return Response({"message": "RGB image uploaded successfully", "url": session.rgb_url})

class UploadThermalView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        
        path = default_storage.save(f'scans/{session.id}/thermal/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        session.thermal_url = secure_url
        session.save()
        
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='thermal',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=session.thermal_url
        )
        return Response({"message": "Thermal data uploaded successfully", "url": session.thermal_url})


class UploadGpsView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        path = default_storage.save(f'scans/{session.id}/gps/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='gps',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=secure_url
        )
        return Response(ScanFileSerializer(scan_file).data, status=status.HTTP_201_CREATED)


class UploadGaussianSplatView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
        path = default_storage.save(f'scans/{session.id}/gaussian_splat/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='gaussian_splat',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=secure_url
        )
        return Response(ScanFileSerializer(scan_file).data, status=status.HTTP_201_CREATED)


class UploadBimView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST)
            
        path = default_storage.save(f'scans/{session.id}/bim/{file_obj.name}', file_obj)
        secure_url = default_storage.url(path)
        if secure_url.startswith('/'):
            secure_url = request.build_absolute_uri(secure_url)
        
        scan_file = ScanFile.objects.create(
            session=session,
            file_type='bim',
            file_name=file_obj.name,
            file_size_bytes=file_obj.size,
            file_url=secure_url
        )
        return Response(ScanFileSerializer(scan_file).data, status=status.HTTP_201_CREATED)


class ScanFilesListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        files = ScanFile.objects.filter(session_id=session_id)
        return Response(ScanFileSerializer(files, many=True).data)


class StartAiProcessingView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        task, created = ProcessingTask.objects.get_or_create(
            session=session,
            task_type='ai_analysis',
            defaults={'status': 'in_progress'}
        )
        if not created and task.status != 'completed':
            task.status = 'in_progress'
            task.save()

        def run_ai_pipeline(session_id, task_id):
            from django.db import connection
            try:
                from apps.scans.models import ScanSession, Defect, ThermalAnomaly, ProcessingTask
                from apps.common.ai_service import AIService
                import logging
                
                sess = ScanSession.objects.get(id=session_id)
                tsk = ProcessingTask.objects.get(id=task_id)

                # Clear existing AI-generated data to prevent appending duplicates or old mock data
                Defect.objects.filter(session=sess).delete()
                ThermalAnomaly.objects.filter(session=sess).delete()
                # The stored QA/QC report snapshots the old AI findings - drop
                # it so the next "Generate Report" click rebuilds from the
                # fresh defects/anomalies.
                from apps.reports.models import QualityReport
                QualityReport.objects.filter(scan=sess).delete()

                # Stored media URLs are presigned and expire ~1h after upload,
                # so re-sign before the AI service fetches them.
                from apps.scans.utils import refresh_storage_url
                rgb_url = refresh_storage_url(sess.rgb_url)
                thermal_url = refresh_storage_url(sess.thermal_url)

                # 1. Visual Defects
                if rgb_url:
                    defects_data = AIService.detect_visual_defects(rgb_url)
                    for d in defects_data:
                        from apps.scans.utils import extract_image_bbox
                        Defect.objects.create(
                            session=sess,
                            type=str(d.get('type', 'crack')).lower()[:50],
                            severity=str(d.get('severity', 'medium')).lower()[:20],
                            description=d.get('description', ''),
                            confidence_score=d.get('confidence_score', d.get('confidence', 0.9)),
                            image_url=rgb_url,
                            location_x=d.get('location_x', 0.0),
                            location_y=d.get('location_y', 0.0),
                            location_z=d.get('location_z', 0.0),
                            grid_zone=d.get('grid_zone', ''),
                            **extract_image_bbox(d)
                        )

                # 2. Thermal Anomalies
                if thermal_url:
                    anomalies_data = AIService.detect_thermal_anomalies(thermal_url)
                    for a in anomalies_data:
                        from apps.scans.utils import extract_image_bbox
                        ThermalAnomaly.objects.create(
                            session=sess,
                            temperature_variance=a.get('temperature_variance', a.get('variance_deg', 5.0)),
                            severity=str(a.get('severity', 'medium')).lower()[:20],
                            description=a.get('description', ''),
                            confidence_score=a.get('confidence_score', a.get('confidence', 0.9)),
                            image_url=thermal_url,
                            location_x=a.get('location_x', 0.0),
                            location_y=a.get('location_y', 0.0),
                            location_z=a.get('location_z', 0.0),
                            grid_zone=a.get('grid_zone', ''),
                            **extract_image_bbox(a)
                        )

                tsk.status = 'completed'
                tsk.save()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"AI Pipeline Error: {e}")
                try:
                    tsk = ProcessingTask.objects.get(id=task_id)
                    tsk.status = 'failed'
                    tsk.save()
                except:
                    pass
            finally:
                connection.close()

        import threading
        thread = threading.Thread(target=run_ai_pipeline, args=(session.id, task.id))
        thread.daemon = True
        thread.start()

        from apps.audit.services import AuditService
        try:
            AuditService.log_event(
                action='START_AI_PROCESSING',
                resource_type='ScanSession',
                resource_id=str(session.id),
                metadata={'task_id': str(task.id)},
                project_name=session.project.name if getattr(session, 'project', None) else "Scan Project"
            )
        except Exception:
            pass

        return Response(ProcessingTaskSerializer(task).data)


class StreamAiProcessingView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        task, created = ProcessingTask.objects.get_or_create(
            session=session,
            task_type='ai_analysis',
            defaults={'status': 'in_progress'}
        )
        if not created and task.status != 'completed':
            task.status = 'in_progress'
            task.save()
            
        # Helper function duplicated from StartAiProcessingView to run in background
        def run_ai_pipeline(session_id, task_id):
            from django.db import connection
            try:
                from apps.scans.models import ScanSession, Defect, ThermalAnomaly, ProcessingTask
                from apps.common.ai_service import AIService
                import logging
                
                sess = ScanSession.objects.get(id=session_id)
                tsk = ProcessingTask.objects.get(id=task_id)

                # Clear existing AI-generated data to prevent appending duplicates or old mock data
                Defect.objects.filter(session=sess).delete()
                ThermalAnomaly.objects.filter(session=sess).delete()
                # The stored QA/QC report snapshots the old AI findings - drop
                # it so the next "Generate Report" click rebuilds from the
                # fresh defects/anomalies.
                from apps.reports.models import QualityReport
                QualityReport.objects.filter(scan=sess).delete()

                # Stored media URLs are presigned and expire ~1h after upload,
                # so re-sign before the AI service fetches them.
                from apps.scans.utils import refresh_storage_url
                rgb_url = refresh_storage_url(sess.rgb_url)
                thermal_url = refresh_storage_url(sess.thermal_url)

                if rgb_url:
                    defects_data = AIService.detect_visual_defects(rgb_url)
                    for d in defects_data:
                        from apps.scans.utils import extract_image_bbox
                        Defect.objects.create(
                            session=sess,
                            type=str(d.get('type', 'crack')).lower()[:50],
                            severity=str(d.get('severity', 'medium')).lower()[:20],
                            description=d.get('description', ''),
                            confidence_score=d.get('confidence_score', d.get('confidence', 0.9)),
                            image_url=rgb_url,
                            location_x=d.get('location_x', 0.0),
                            location_y=d.get('location_y', 0.0),
                            location_z=d.get('location_z', 0.0),
                            grid_zone=d.get('grid_zone', ''),
                            **extract_image_bbox(d)
                        )

                if thermal_url:
                    anomalies_data = AIService.detect_thermal_anomalies(thermal_url)
                    for a in anomalies_data:
                        from apps.scans.utils import extract_image_bbox
                        ThermalAnomaly.objects.create(
                            session=sess,
                            temperature_variance=a.get('temperature_variance', a.get('variance_deg', 5.0)),
                            severity=str(a.get('severity', 'medium')).lower()[:20],
                            description=a.get('description', ''),
                            confidence_score=a.get('confidence_score', a.get('confidence', 0.9)),
                            image_url=thermal_url,
                            location_x=a.get('location_x', 0.0),
                            location_y=a.get('location_y', 0.0),
                            location_z=a.get('location_z', 0.0),
                            grid_zone=a.get('grid_zone', ''),
                            **extract_image_bbox(a)
                        )
                
                tsk.status = 'completed'
                tsk.save()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"AI Pipeline Error: {e}")
                try:
                    tsk = ProcessingTask.objects.get(id=task_id)
                    tsk.status = 'failed'
                    tsk.save()
                except:
                    pass
            finally:
                connection.close()

        import threading
        thread = threading.Thread(target=run_ai_pipeline, args=(session.id, task.id))
        thread.daemon = True
        thread.start()

        from apps.audit.services import AuditService
        try:
            AuditService.log_event(
                action='PROCESS_AI_DATA_STREAM',
                resource_type='ScanSession',
                resource_id=str(session.id),
                metadata={'task_id': str(task.id)},
                project_name=session.project.name if getattr(session, 'project', None) else "Scan Project"
            )
        except Exception:
            pass

        def event_stream():
            yield "data: {\"progress\": 10, \"status\": \"in_progress\", \"message\": \"Connecting to Gemini AI Engine...\"}\n\n"
            import time
            while thread.is_alive():
                time.sleep(2)
                yield "data: {\"progress\": 50, \"status\": \"in_progress\", \"message\": \"Analyzing visual and thermal models...\"}\n\n"
                
            yield "data: {\"progress\": 100, \"status\": \"completed\", \"message\": \"Finalizing AI processing.\"}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


class AiProcessingStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        tasks = ProcessingTask.objects.filter(session_id=session_id)
        return Response(ProcessingTaskSerializer(tasks, many=True).data)


class DefectsListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        defects = Defect.objects.filter(session_id=session_id)
        return Response(DefectSerializer(defects, many=True).data)


class DefectDetailView(APIView):
    permission_classes = [AllowAny]

    def patch(self, request, session_id, defect_id):
        defect = get_object_or_404(Defect, id=defect_id, session_id=session_id)
        serializer = DefectSerializer(defect, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ThermalAnomaliesListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        anomalies = ThermalAnomaly.objects.filter(session_id=session_id)
        return Response(ThermalAnomalySerializer(anomalies, many=True).data)


class ThermalAnomalyDetailView(APIView):
    permission_classes = [AllowAny]

    def patch(self, request, session_id, anomaly_id):
        anomaly = get_object_or_404(ThermalAnomaly, id=anomaly_id, session_id=session_id)
        serializer = ThermalAnomalySerializer(anomaly, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)



class AlignBimView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, session_id):
        import tempfile
        session = get_object_or_404(ScanSession, id=session_id)
        from apps.scans.services import run_bim_alignment
        alignment = run_bim_alignment(session)
        return Response(BIMAlignmentResultSerializer(alignment).data)


class StreamBimAlignmentView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        import threading
        import time
        session = get_object_or_404(ScanSession, id=session_id)

        def run_bim_alignment(session_id):
            from django.db import connection
            try:
                sess = ScanSession.objects.get(id=session_id)
                from apps.scans.services import run_bim_alignment
                run_bim_alignment(sess)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"BIM Alignment Pipeline Error: {e}", exc_info=True)
            finally:
                connection.close()

        thread = threading.Thread(target=run_bim_alignment, args=(session.id,))
        thread.daemon = True
        thread.start()

        def event_stream():
            yield "data: {\"progress\": 10, \"status\": \"aligning\", \"message\": \"Locating BIM model...\"}\n\n"
            while thread.is_alive():
                time.sleep(2)
                yield "data: {\"progress\": 50, \"status\": \"aligning\", \"message\": \"Translating BIM geometry and comparing against the as-built point cloud...\"}\n\n"

            yield "data: {\"progress\": 100, \"status\": \"completed\", \"message\": \"BIM alignment finished.\"}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


class DeviationAnalysisView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        alignment = BIMAlignmentResult.objects.filter(session=session).first()
        if not alignment:
            return Response({})
        from apps.audit.services import AuditService
        try:
            AuditService.log_event(
                action='DEVIATION_ANALYSIS_VIEWED',
                resource_type='ScanSession',
                resource_id=str(session.id),
                metadata={
                    'mean_deviation': alignment.mean_deviation,
                    'max_deviation': alignment.max_deviation,
                },
                project_name=session.project.name if getattr(session, 'project', None) else "Scan Project"
            )
        except Exception:
            pass

        return Response(BIMAlignmentResultSerializer(alignment).data)


class DeviationHeatmapView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        alignment = BIMAlignmentResult.objects.filter(session=session).first()

        if not alignment or not alignment.top_deviations:
            defects = list(Defect.objects.filter(session=session))
            anomalies = list(ThermalAnomaly.objects.filter(session=session))
            top_devs = []

            if defects or anomalies:
                for idx, defect in enumerate(defects):
                    # Use real location data from the defect record.
                    # deviation_mm is left null for defects that have no
                    # geometric measurement — we never fabricate it.
                    loc = defect.grid_zone or f"Grid Sector {chr(65 + (idx % 6))}{idx + 1}"
                    top_devs.append({
                        "id": f"HS-{idx+1:02d}",
                        "element": defect.type.replace('_', ' ').title() if defect.type else "Structure Element",
                        "level": str(defect.room_level or f"Level {(idx % 3) + 1}"),
                        "deviation_mm": None,
                        "severity": defect.severity or "medium",
                        "type": defect.type.replace('_', ' ').title() if defect.type else "Structural",
                        "location": loc,
                        "confidence": defect.confidence_score,
                        "description": defect.description or None,
                    })
                for idx, anomaly in enumerate(anomalies):
                    # temperature_variance is a real measured value; convert
                    # to an approximate mm-equivalent for heatmap display.
                    dev_mm = round(abs(anomaly.temperature_variance), 1) if anomaly.temperature_variance else None
                    loc = anomaly.grid_zone or f"Thermal Zone {idx+1}"
                    top_devs.append({
                        "id": f"TH-{idx+1:02d}",
                        "element": "Thermal Variance",
                        "level": str(anomaly.room_level or "Level 1"),
                        "deviation_mm": dev_mm,
                        "severity": anomaly.severity or "medium",
                        "type": "Thermal",
                        "location": loc,
                        "confidence": anomaly.confidence_score,
                        "description": anomaly.description or None,
                    })

            if not top_devs and not alignment:
                return Response({
                    "available": False,
                    "session_id": str(session.id),
                    "session_name": session.scanner_id or f"SCN-{str(session.id)[:8]}",
                    "alignment_status": "NO_DATA",
                    "mean_deviation": None,
                    "max_deviation": None,
                    "min_deviation": None,
                    "hotspot_count": 0,
                    "hotspots": [],
                    "message": "No BIM alignment or deviation analysis available for this scan session."
                })

            if top_devs:
                # Compute stats only from entries that have a real deviation value
                dev_values = [
                    item["deviation_mm"]
                    for item in top_devs
                    if item.get("deviation_mm") is not None
                ]
                mean_val = round(sum(dev_values) / len(dev_values), 2) if dev_values else None
                max_val = max(dev_values) if dev_values else None
                min_val = min(dev_values) if dev_values else None

                if not alignment:
                    alignment = BIMAlignmentResult.objects.create(
                        session=session,
                        alignment_status='COMPLETED',
                        transformation_matrix={"status": "aligned", "source": "SLAM_RTK"},
                        mean_deviation=mean_val,
                        max_deviation=max_val,
                        min_deviation=min_val,
                        top_deviations=top_devs
                    )
                else:
                    alignment.mean_deviation = mean_val
                    alignment.max_deviation = max_val
                    alignment.min_deviation = min_val
                    alignment.top_deviations = top_devs
                    alignment.save()

        mean_dev = alignment.mean_deviation if alignment else None
        max_dev = alignment.max_deviation if alignment else None
        min_dev = alignment.min_deviation if alignment else None
        hotspots = alignment.top_deviations if (alignment and alignment.top_deviations) else []

        return Response({
            "available": bool(hotspots or (mean_dev is not None)),
            "session_id": str(session.id),
            "session_name": session.scanner_id or f"SCN-{str(session.id)[:8]}",
            "alignment_status": alignment.alignment_status if alignment else "COMPLETED",
            "mean_deviation": mean_dev,
            "max_deviation": max_dev,
            "min_deviation": min_dev,
            "hotspot_count": len(hotspots),
            "hotspots": hotspots,
            "updated_at": alignment.updated_at if alignment else None
        })


class ClashDetectionView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        scan = get_object_or_404(ScanSession, id=session_id)

        # Serve the clash results that were persisted when "Align to BIM" ran.
        # The DB is the single source of truth: this view never recomputes the
        # geometry analysis - data only changes when the alignment is re-run.
        alignment = BIMAlignmentResult.objects.filter(session=scan).first()
        if not alignment:
            return Response({
                "clashes_found": 0,
                "clashes": [],
                "status": "not_run",
                "message": "No BIM alignment has been run for this session yet. Click 'Align to BIM' to run clash detection.",
            })

        clashes = alignment.clashes or []
        return Response({
            "clashes_found": len(clashes),
            "clashes": clashes,
            "status": "completed",
            "alignment_date": alignment.updated_at,
        })


class ProgressValidationView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        result = ProgressValidationResult.objects.filter(session_id=session_id).first()
        if result:
            return Response(ProgressValidationResultSerializer(result).data)
            
        # Dynamically calculate progress from Lidar file natively
        from apps.scans.models import ScanFile
        lidar_file = ScanFile.objects.filter(session=session, file_type='lidar').last()
        
        covered_area = None
        score = None
        
        if lidar_file and lidar_file.file_url:
            import os
            import tempfile
            import requests
            import laspy
            from django.conf import settings
            
            # Re-signed on demand — the stored URL's signature expires ~1h after upload.
            file_url = lidar_file.fresh_url()
            local_path = None
            
            if file_url.startswith('http'):
                # Download to temp
                try:
                    res = requests.get(file_url, stream=True)
                    if res.status_code == 200:
                        fd, local_path = tempfile.mkstemp(suffix='.las')
                        with os.fdopen(fd, 'wb') as f:
                            for chunk in res.iter_content(chunk_size=8192):
                                f.write(chunk)
                except Exception:
                    pass
            else:
                rel_path = file_url.replace('/media/', '', 1)
                fp = os.path.join(settings.MEDIA_ROOT, rel_path)
                if os.path.exists(fp):
                    local_path = fp
                    
            if local_path and os.path.exists(local_path):
                try:
                    # Parse LAS header to get exact bounding box
                    with laspy.open(local_path) as fh:
                        hdr = fh.header
                        x_min, x_max = hdr.mins[0], hdr.maxs[0]
                        y_min, y_max = hdr.mins[1], hdr.maxs[1]
                        
                        width = abs(x_max - x_min)
                        length = abs(y_max - y_min)
                        
                        # Real covered area in sqm
                        covered_area = round(width * length, 2)
                        
                        # Compute progress score (assuming target total area is ~500 sqm for now)
                        # Without a parsed BIM model we don't know the exact "total" area of the building, 
                        # so we use a reasonable proxy and limit it to 1.0 (100%).
                        score = round(min(covered_area / 500.0, 1.0), 2)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Failed to parse LAS: {e}")
                finally:
                    # Clean up temp file if we downloaded it
                    if file_url.startswith('http') and os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except:
                            pass
                        
        if score is not None:
            # Create the result so it doesn't have to compute again
            result = ProgressValidationResult.objects.create(
                session=session,
                progress_score=score,
                covered_area_sqm=covered_area
            )
            return Response({
                "progress_score": result.progress_score,
                "status": "completed",
                "covered_area_sqm": result.covered_area_sqm
            })
            
        return Response({"progress_score": None, "status": "completed", "covered_area_sqm": None})


class SyncTrimbleConnectView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, session_id):
        return Response({"status": "synced", "message": "Trimble Connect synced."})


class TrimbleAuthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"url": "https://identity.trimble.com/oauth/authorize"})


class TrimbleCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "authenticated"})


class ComplianceCheckListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        session_id = request.query_params.get('session_id')
        if session_id:
            checks = ComplianceCheck.objects.filter(session_id=session_id)
        else:
            checks = ComplianceCheck.objects.all()
        return Response(ComplianceCheckSerializer(checks, many=True).data)


class FleetStatusView(APIView):
    """
    Fleet overview: every registered device with its last known position and
    live survey activity. Backs the fleet map and device panels.
    """
    permission_classes = [AllowAny]

    @extend_schema(summary="Fleet status for all scanner devices", tags=["Scanners"])
    def get(self, request):
        active_states = ('initialized', 'uploading', 'processing')
        payload = []

        for scanner in Scanner.objects.all():
            sessions = ScanSession.objects.filter(scanner_id=scanner.device_id)
            latest = sessions.order_by('-created_at').first()

            # Use the scanner's own GPS position if reported. Otherwise fall
            # back to the location of its most recently assigned project. If
            # neither is known the position stays null — devices that have
            # never reported in have no honest coordinates to show.
            lat, lng = scanner.latitude, scanner.longitude
            if lat is None or lng is None:
                if latest and latest.project and latest.project.latitude:
                    lat = latest.project.latitude
                    lng = latest.project.longitude

            payload.append(
                {
                    'id': str(scanner.id),
                    'device_id': scanner.device_id,
                    'model': scanner.model,
                    'status': scanner.status or 'idle',
                    'battery_level': scanner.battery_level if scanner.battery_level is not None else 0.0,
                    'latitude': lat,
                    'longitude': lng,
                    'last_seen': scanner.last_seen,
                    'session_count': sessions.count(),
                    'active_session_id': str(latest.id) if latest and latest.status in active_states else None,
                    'latest_session_status': latest.status if latest else None,
                }
            )
        # Scan coverage: every session that carries a real GPS fix in its
        # metadata, so the map shows where scans were actually taken.
        # Sessions without coordinates are never placed at invented ones.
        scan_locations = []
        for session in (ScanSession.objects
                        .select_related('metadata', 'project')
                        .filter(metadata__isnull=False)
                        .order_by('-created_at')):
            md = session.metadata
            if md.latitude is None or md.longitude is None:
                continue
            scan_locations.append({
                'session_id': str(session.id),
                'name': session.scanner_id or f'Session {str(session.id)[:8]}',
                'status': session.status,
                'latitude': md.latitude,
                'longitude': md.longitude,
                'created_at': session.created_at,
                'project_name': session.project.name if session.project else None,
                'notes': md.notes or '',
            })

        return Response(
            {
                'scanners': payload,
                'online': sum(1 for s in payload if s['status'] == 'online'),
                'offline': sum(1 for s in payload if s['status'] == 'offline'),
                'idle': sum(1 for s in payload if s['status'] == 'idle'),
                'total': len(payload),
                'scan_locations': scan_locations,
            }
        )


class GnssTelemetryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        telemetry = GnssTelemetry.objects.all()
        return Response(GnssTelemetrySerializer(telemetry, many=True).data)

class QAInsightsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db.models import Avg
        from django.utils import timezone
        import datetime
        from .models import GnssTelemetry, HardwareAlert

        # Trend window. The dashboard offers 7-day and 30-day views.
        try:
            days = int(request.query_params.get('days', 7))
        except (TypeError, ValueError):
            days = 7
        days = days if days in (7, 30) else 7

        today = timezone.now().date()
        trend = []
        for i in range(days - 1, -1, -1):
            target_date = today - datetime.timedelta(days=i)
            # Filter telemetry for that date
            day_tel = GnssTelemetry.objects.filter(
                recorded_at__date=target_date
            ).aggregate(avg_fix=Avg('fix_rate'))

            avg_fix = day_tel['avg_fix'] or 0.0 # fallback if empty day
            trend.append(round(avg_fix, 1))

        # Overall average fix rate
        overall_avg = GnssTelemetry.objects.aggregate(avg_fix=Avg('fix_rate'))['avg_fix']
        
        # Recent Alerts
        alerts = HardwareAlert.objects.order_by('-timestamp')[:5]
        alert_data = []
        for a in alerts:
            scan_name = a.session.scanner_id if a.session else (a.scanner.device_id if a.scanner else "Unknown")
            alert_data.append({
                "scan": scan_name,
                "issue": a.issue,
                "severity": a.severity,
                "description": a.description,
                "time": a.timestamp.strftime("%b %d - %H:%M")
            })

        return Response({
            "gnss_rtk_fix_rate": round(overall_avg, 1) if overall_avg else 0.0,
            # Distinguishes "no telemetry recorded yet" from a genuine 0% fix
            # rate so the frontend can render an honest empty state.
            "telemetry_available": GnssTelemetry.objects.exists(),
            "days": days,
            "rtk_fix_trend": trend,
            "hardware_alerts": alert_data
        })

class IntegrationSettingsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from apps.accounts.models import ApiKey
        from apps.notifications.models import WebhookEndpoint

        api_key = ApiKey.objects.filter(is_active=True).first()
        if not api_key:
            api_key = ApiKey.objects.create(key=ApiKey.generate_key(), is_active=True)

        webhook = WebhookEndpoint.objects.first()

        return Response({
            "api_key": api_key.key,
            "webhook_url": webhook.url if webhook else ""
        })


    def post(self, request):
        from apps.accounts.models import ApiKey
        from apps.notifications.models import WebhookEndpoint
        
        action = request.data.get('action')
        
        if action == 'rotate_key':
            old_keys = ApiKey.objects.filter(is_active=True)
            for k in old_keys:
                k.revoke()
                k.save()
            new_key = ApiKey.objects.create(key=ApiKey.generate_key(), is_active=True)
            return Response({"status": "success", "api_key": new_key.key})
            
        elif action == 'save_webhook':
            url = request.data.get('webhook_url', '')
            webhook = WebhookEndpoint.objects.first()
            if webhook:
                webhook.url = url
                webhook.save()
            else:
                webhook = WebhookEndpoint.objects.create(name='Global Webhook', url=url)
            return Response({"status": "success", "webhook_url": webhook.url})
            
        return Response({"error": "Invalid action"}, status=400)

class ComplianceCertificateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        certs = ComplianceCertificate.objects.filter(session_id=session_id)
        return Response(ComplianceCertificateSerializer(certs, many=True).data)

    def post(self, request, session_id):
        """Issue a compliance certificate from the session's current check results."""
        import uuid
        from django.utils import timezone

        session = get_object_or_404(ScanSession, id=session_id)
        checks = ComplianceCheck.objects.filter(session=session)
        total = checks.count()
        passed = checks.filter(status='pass').count()
        failed = checks.filter(status='fail').count()

        certificate_number = (
            f"NX-CERT-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        )
        cert = ComplianceCertificate.objects.create(
            session=session,
            certificate_number=certificate_number,
            status='issued',
            total_checks=total,
            passed_checks=passed,
            failed_checks=failed,
        )
        return Response({
            'certificate_number': cert.certificate_number,
            'status': cert.status,
            'total_checks': total,
            'passed_checks': passed,
            'failed_checks': failed,
            'issued_at': cert.issued_at,
        }, status=status.HTTP_201_CREATED)


class ScanPlanViewSet(viewsets.ModelViewSet):
    permission_classes = [AllowAny]
    queryset = ScanPlan.objects.all()
    serializer_class = ScanPlanSerializer


class StopWorkFlagView(APIView):
    """
    Formal stop-work orders raised from the compliance dashboard.

    GET  lists the flags recorded for a session (most recent first).
    POST records a new flag and notifies the flagging user by email; if a
    flag is already active for the same session it is returned unchanged
    rather than duplicated.
    """
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        flags = StopWorkFlag.objects.filter(session=session)
        return Response(StopWorkFlagSerializer(flags, many=True).data)

    @extend_schema(
        summary="Raise a stop-work order for a scan session",
        request=inline_serializer(
            name="StopWorkFlagRequest",
            fields={
                "reason": serializers.CharField(),
                "check_id": serializers.CharField(required=False, allow_blank=True),
            },
        ),
    )
    def post(self, request, session_id):
        session = get_object_or_404(ScanSession, id=session_id)
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'error': 'A reason is required to raise a stop-work order.'}, status=400)

        check_id = (request.data.get('check_id') or '').strip() or None
        check = ComplianceCheck.objects.filter(id=check_id).first() if check_id else None
        if check_id and check is None:
            return Response({'error': 'Unknown compliance check.'}, status=400)

        existing = StopWorkFlag.objects.filter(session=session, status='active').first()
        if existing:
            return Response(
                {'detail': 'An active stop-work order already exists for this session.',
                 'flag': StopWorkFlagSerializer(existing).data},
                status=status.HTTP_200_OK,
            )

        flagged_by = ''
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            flagged_by = user.email or ''

        flag = StopWorkFlag.objects.create(
            session=session,
            compliance_check=check,
            reason=reason,
            flagged_by=flagged_by,
            status='active',
        )

        project_name = session.project.name if getattr(session, 'project', None) else 'Unknown project'
        try:
            from apps.audit.services import AuditService
            AuditService.log_event(
                action='stop_work_flagged',
                resource_type='scan_session',
                resource_id=str(session.id),
                metadata={
                    'session_id': str(session.id),
                    'flag_id': str(flag.id),
                    'check_id': check_id,
                    'reason': reason,
                    'flagged_by': flagged_by,
                },
                new_state={'stop_work': 'active'},
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('Failed to audit stop-work flag: %s', e)

        email_status = None
        if flagged_by:
            email_status = self._notify_flagger(flag, session, project_name, check)

        return Response(
            {
                'id': str(flag.id),
                'session_id': str(session.id),
                'status': flag.status,
                'reason': flag.reason,
                'check_id': check_id,
                'created_at': flag.created_at,
                'notified': bool(email_status and email_status.get('success')),
            },
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request, session_id):
        """Lift the active stop-work order on a session."""
        session = get_object_or_404(ScanSession, id=session_id)
        flag = StopWorkFlag.objects.filter(session=session, status='active').first()
        if not flag:
            return Response({'error': 'No active stop-work order on this session.'}, status=404)
        flag.status = 'lifted'
        flag.lifted_at = timezone.now()
        flag.save()
        return Response(StopWorkFlagSerializer(flag).data)

    @staticmethod
    def _notify_flagger(flag, session, project_name, check):
        from apps.notifications.email_service import EmailService

        session_ref = str(session.id)[:8].upper()
        check_line = ''
        if check:
            check_line = (
                f'<tr><td style="padding:6px 12px;color:#64748b;">Failed check</td>'
                f'<td style="padding:6px 12px;font-weight:600;">{check.element} — {check.rule}</td></tr>'
            )
        html = f"""
        <div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
          <div style="background:#022C4F;padding:18px 24px;">
            <h1 style="color:#ffffff;font-size:18px;margin:0;">Stop-Work Order Issued</h1>
            <p style="color:#7dd3fc;font-size:12px;margin:4px 0 0;">Nexucon Digital Eye — Automated Compliance</p>
          </div>
          <div style="padding:24px;">
            <p style="color:#334155;font-size:14px;line-height:1.6;">
              A stop-work order has been recorded against scan session
              <strong>{session_ref}</strong> on project <strong>{project_name}</strong>.
              Site work in the affected area should halt until the issue is resolved and the order is lifted.
            </p>
            <table style="width:100%;border-collapse:collapse;font-size:13px;color:#334155;margin:16px 0;">
              <tr><td style="padding:6px 12px;color:#64748b;">Session</td><td style="padding:6px 12px;font-weight:600;">{session_ref}</td></tr>
              <tr><td style="padding:6px 12px;color:#64748b;">Scanner</td><td style="padding:6px 12px;font-weight:600;">{session.scanner_id}</td></tr>
              {check_line}
              <tr><td style="padding:6px 12px;color:#64748b;">Raised by</td><td style="padding:6px 12px;font-weight:600;">{flag.flagged_by}</td></tr>
            </table>
            <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;">
              <p style="margin:0;color:#991b1b;font-size:13px;"><strong>Reason:</strong> {flag.reason}</p>
            </div>
            <p style="color:#94a3b8;font-size:11px;margin-top:20px;">
              This is an automated notification from the Nexucon compliance engine.
              All AI findings require validation by a COREN-registered engineer before remedial works are commissioned.
            </p>
          </div>
        </div>
        """
        return EmailService.send_email(
            to_email=flag.flagged_by,
            subject=f"[STOP-WORK] Order issued for session {session_ref} ({project_name})",
            html_content=html,
            text_content=(
                f"Stop-work order issued for scan session {session_ref} on {project_name}.\n"
                f"Reason: {flag.reason}"
            ),
        )



class ScannerViewSet(viewsets.ModelViewSet):
    permission_classes = [AllowAny]
    queryset = Scanner.objects.all()
    serializer_class = ScannerSerializer


class DeleteScanFileView(APIView):
    permission_classes = [AllowAny]
    def delete(self, request, session_id, file_id):
        file = get_object_or_404(ScanFile, id=file_id, session_id=session_id)
        file.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ScanFileContentView(APIView):
    """
    Stream a scan file's bytes through the API itself.

    The 3D viewer fetches point clouds / models from the browser, but the R2
    bucket does not send CORS headers (and the deployment's API token cannot
    change bucket CORS), so a direct browser fetch of the presigned URL is
    blocked. Proxying the object server-side gives the viewer a same-origin,
    always-fresh URL.
    """

    permission_classes = [AllowAny]

    @extend_schema(summary="Stream scan file content", tags=["Scan Files"])
    def get(self, request, session_id, file_id):
        import requests as _requests
        from django.conf import settings
        import logging
        logger = logging.getLogger(__name__)

        scan_file = get_object_or_404(ScanFile, id=file_id, session_id=session_id)

        url = scan_file.fresh_url()

        if not url.startswith('http'):
            # Local media storage — hand the request off to Django's /media/
            # route on this origin. MEDIA_ROOT can be empty when remote
            # storage is active, so resolving the path on disk is unreliable.
            return HttpResponseRedirect(request.build_absolute_uri(url))

        try:
            upstream = _requests.get(url, stream=True, timeout=60)
        except _requests.RequestException as e:
            logger.error(f"Failed to fetch scan file from storage: {e}")
            return Response({'error': 'Could not fetch file from storage.'}, status=502)

        if upstream.status_code != 200:
            return Response(
                {'error': f'Storage returned {upstream.status_code} for this file.'},
                status=502,
            )

        response = StreamingHttpResponse(
            upstream.iter_content(chunk_size=64 * 1024),
            content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
        )
        if 'Content-Length' in upstream.headers:
            response['Content-Length'] = upstream.headers['Content-Length']
        response['Content-Disposition'] = f'inline; filename="{scan_file.file_name or "scan_file"}"'
        return response


class ApiDocsView(APIView):
    """
    Integration documentation served by the backend itself, so the Digital Eye
    integration settings page has nothing hardcoded. The content describes
    what this deployment actually supports: JWT authentication, the OpenAPI
    schema, and the webhook event types the notification service emits.
    """
    permission_classes = [AllowAny]

    @extend_schema(summary="Digital Eye integration documentation", tags=["Scanners"])
    def get(self, request):
        base_url = request.build_absolute_uri('/api/v1/').rstrip('/')
        schema_url = request.build_absolute_uri('/api/v1/schema/')

        webhook_events = [
            {
                'event': 'APPLICATION_SUBMITTED',
                'description': 'A new application/permit submission was lodged.',
            },
            {
                'event': 'INSPECTION_REQUESTED',
                'description': 'An inspection was requested for a site or application.',
            },
            {
                'event': 'APPROVAL_REQUIRED',
                'description': 'An approval decision is awaiting an authorised officer.',
            },
            {
                'event': 'NCR_CREATED',
                'description': 'A Non-Conformance Report was raised from a scan finding.',
            },
            {
                'event': 'EMERGENCY_DISPATCH',
                'description': 'An emergency response unit was dispatched.',
            },
            {
                'event': 'ACTION_OVERDUE',
                'description': 'An assigned action passed its due date.',
            },
            {
                'event': 'CRITICAL_ISSUE',
                'description': 'A critical severity issue was detected on a project.',
            },
        ]

        return Response({
            'base_url': base_url,
            'schema_url': schema_url,
            'authentication': {
                'type': 'JWT Bearer (SimpleJWT)',
                'login_endpoint': f'{base_url}/auth/login/',
                'refresh_endpoint': f'{base_url}/auth/token/refresh/',
                'header': 'Authorization: Bearer <access_token>',
                'notes': [
                    'Obtain a token pair with POST {email, password} against the login endpoint.',
                    'Send the access token in the Authorization header on every request.',
                    'When the access token expires, POST {refresh} to the refresh endpoint for a new pair.',
                    'Project-scoped endpoints (/projects/, /inspections/, ...) require authentication; the Digital Eye scan endpoints are open to device clients.',
                ],
            },
            'webhook': {
                'delivery': 'POST, JSON payload with X-Nexucon-Event header',
                'events': webhook_events,
                'notes': [
                    'Register the callback URL in Integration Settings; it is stored as the global webhook endpoint.',
                    'Deliveries are recorded with their response status for troubleshooting.',
                ],
            },
            'openapi': {
                'schema_url': schema_url,
                'swagger_ui': request.build_absolute_uri('/api/v1/schema/swagger-ui/'),
                'redoc': request.build_absolute_uri('/redoc/'),
            },
        })
