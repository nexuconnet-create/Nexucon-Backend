from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from django.db.models import Q
from django.utils import timezone
from django.contrib.auth import get_user_model
import datetime
from .models import Inspection, Checklist, Finding, StopWorkOrder
from .serializers import (
    InspectionSerializer, CreateInspectionSerializer,
    ChecklistSerializer, FindingSerializer, StopWorkOrderSerializer
)
from .services import InspectionService

User = get_user_model()

class InspectionViewSet(viewsets.ModelViewSet):
    queryset = Inspection.objects.all().select_related('project', 'inspector', 'permit', 'parent_inspection').prefetch_related('findings', 'stop_work_orders')
    serializer_class = InspectionSerializer
    permission_classes = [AllowAny]

    def get_serializer_class(self):
        if self.action == 'create':
            return CreateInspectionSerializer
        return InspectionSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        project_param = self.request.query_params.get('project')
        inspector_param = self.request.query_params.get('inspector')
        priority_param = self.request.query_params.get('priority')
        type_param = self.request.query_params.get('type')
        search_param = self.request.query_params.get('search')

        if status_param:
            status_param = status_param.lower()
            if status_param == 'requests':
                queryset = queryset.filter(status='REQUESTED')
            elif status_param == 'schedule':
                queryset = queryset.filter(status='SCHEDULED')
            elif status_param == 'active':
                queryset = queryset.filter(status='IN_PROGRESS')
            elif status_param == 'findings':
                queryset = queryset.filter(findings__isnull=False).distinct()
            elif status_param == 'stop-work':
                queryset = queryset.filter(Q(status='FAILED') | Q(stop_work_orders__status='ACTIVE')).distinct()
            elif status_param == 're-inspections':
                queryset = queryset.filter(Q(status='RE_INSPECTION_REQUIRED') | Q(inspection_type='Re-Inspection')).distinct()
            elif status_param == 'reports':
                queryset = queryset.filter(status='COMPLETED')
            else:
                queryset = queryset.filter(status=status_param.upper())

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if inspector_param:
            queryset = queryset.filter(Q(inspector_id=inspector_param) | Q(inspector_name__icontains=inspector_param))

        if priority_param:
            queryset = queryset.filter(priority__iexact=priority_param)

        if type_param:
            queryset = queryset.filter(inspection_type__icontains=type_param)

        if search_param:
            queryset = queryset.filter(
                Q(inspection_reference__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(inspector_name__icontains=search_param) |
                Q(inspection_type__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return InspectionService.create_inspection_request(
            data=serializer.validated_data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        if data.get('scheduled_date') == '':
            data['scheduled_date'] = None
        if data.get('permit') == '':
            data['permit'] = None

        serializer = self.get_serializer(data=data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'Validation failed',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            inspection = self.perform_create(serializer)
            out_serializer = InspectionSerializer(inspection)
            return Response({
                'success': True,
                'message': 'Inspection request created successfully',
                'data': out_serializer.data
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        """Return counts for all 7 inspection tabs."""
        today = timezone.now().date()

        requests_count = Inspection.objects.filter(status='REQUESTED').count()
        schedule_count = Inspection.objects.filter(status='SCHEDULED').count()
        active_count = Inspection.objects.filter(status='IN_PROGRESS').count()
        findings_count = Finding.objects.filter(is_resolved=False).count()
        stop_work_count = StopWorkOrder.objects.filter(status='ACTIVE').count()
        re_inspections_count = Inspection.objects.filter(Q(status='RE_INSPECTION_REQUIRED') | Q(inspection_type='Re-Inspection')).distinct().count()
        reports_count = Inspection.objects.filter(status='COMPLETED').count()
        total = Inspection.objects.count()

        return Response({
            'success': True,
            'data': {
                'requests': requests_count,
                'schedule': schedule_count,
                'active': active_count,
                'findings': findings_count,
                'stop_work': stop_work_count,
                're_inspections': re_inspections_count,
                'reports': reports_count,
                'total': total
            }
        })

    @action(detail=True, methods=['post'], url_path='assign')
    def assign_inspector(self, request, pk=None):
        """Assign inspector and set schedule date/time."""
        inspection = self.get_object()
        inspector_id = request.data.get('inspector_id')
        inspector_name = request.data.get('inspector_name')
        scheduled_date = request.data.get('scheduled_date')

        inspector_user = None
        if inspector_id:
            try:
                inspector_user = User.objects.filter(Q(id=inspector_id) | Q(username=inspector_id) | Q(email=inspector_id)).first()
            except Exception:
                pass

        updated = InspectionService.assign_and_schedule(
            inspection=inspection,
            inspector_user=inspector_user,
            scheduled_date=scheduled_date,
            actor=request.user,
            inspector_name=inspector_name
        )

        return Response({
            'success': True,
            'message': f"Inspection assigned to {updated.inspector_name} and scheduled.",
            'data': InspectionSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='checkin')
    def checkin(self, request, pk=None):
        """Inspector GPS check-in at site."""
        inspection = self.get_object()
        lat = request.data.get('latitude') or request.data.get('lat')
        lng = request.data.get('longitude') or request.data.get('lng')

        updated = InspectionService.check_in(
            inspection=inspection,
            lat=lat,
            lng=lng,
            actor=request.user
        )

        return Response({
            'success': True,
            'message': 'Inspector check-in verified via GPS',
            'data': InspectionSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='complete')
    def complete(self, request, pk=None):
        """Complete inspection with pass/fail/conditional outcome."""
        inspection = self.get_object()
        outcome = request.data.get('outcome', 'PASSED')
        checklist_results = request.data.get('checklist_results')
        summary_notes = request.data.get('summary_notes', '')

        updated = InspectionService.complete_inspection(
            inspection=inspection,
            outcome=outcome,
            checklist_results=checklist_results,
            summary_notes=summary_notes,
            actor=request.user
        )

        return Response({
            'success': True,
            'message': f"Inspection completed with outcome: {outcome}",
            'data': InspectionSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='log-finding')
    def log_finding(self, request, pk=None):
        """Log defect / finding on this inspection."""
        inspection = self.get_object()
        finding = InspectionService.log_finding(
            inspection=inspection,
            data=request.data,
            actor=request.user
        )
        return Response({
            'success': True,
            'message': 'Inspection finding logged successfully',
            'data': FindingSerializer(finding).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='issue-stop-work')
    def issue_stop_work(self, request, pk=None):
        """Issue Stop-Work Order directly from inspection."""
        inspection = self.get_object()
        reason = request.data.get('reason', 'Critical safety or structural non-conformance.')
        severity = request.data.get('severity', 'CRITICAL')

        swo = InspectionService.issue_stop_work(
            project=inspection.project,
            reason=reason,
            severity=severity,
            actor=request.user,
            inspection=inspection
        )

        return Response({
            'success': True,
            'message': f"Stop-Work Order {swo.order_number} issued. Project suspended.",
            'data': StopWorkOrderSerializer(swo).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='create-reinspection')
    def create_reinspection(self, request, pk=None):
        """Create a follow-up re-inspection."""
        inspection = self.get_object()
        scheduled_date = request.data.get('scheduled_date')
        inspector_name = request.data.get('inspector_name')
        inspector_id = request.data.get('inspector_id')
        notes = request.data.get('notes') or request.data.get('summary_notes')
        priority = request.data.get('priority', 'High')

        reinspection = InspectionService.create_reinspection(
            original_inspection=inspection,
            scheduled_date=scheduled_date,
            actor=request.user,
            inspector_name=inspector_name,
            inspector_id=inspector_id,
            notes=notes,
            priority=priority
        )

        return Response({
            'success': True,
            'message': f"Re-Inspection {reinspection.inspection_reference} scheduled successfully.",
            'data': InspectionSerializer(reinspection).data
        }, status=status.HTTP_201_CREATED)


class StopWorkOrderViewSet(viewsets.ModelViewSet):
    queryset = StopWorkOrder.objects.all().select_related('project', 'inspection', 'finding')
    serializer_class = StopWorkOrderSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        project_param = self.request.query_params.get('project')
        search_param = self.request.query_params.get('search')

        if status_param and status_param.upper() != 'ALL':
            queryset = queryset.filter(status=status_param.upper())

        if project_param:
            import uuid
            try:
                val = uuid.UUID(str(project_param))
                queryset = queryset.filter(project_id=val)
            except Exception:
                queryset = queryset.filter(
                    Q(project__reference_number=str(project_param)) |
                    Q(project__name__icontains=str(project_param))
                )

        if search_param:
            queryset = queryset.filter(
                Q(order_number__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(project__reference_number__icontains=search_param) |
                Q(reason__icontains=search_param)
            )

        return queryset

    def create(self, request, *args, **kwargs):
        project_id = request.data.get('project') or request.data.get('project_id')
        if not project_id:
            return Response({'success': False, 'message': 'Target construction project is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        project = None
        import uuid
        try:
            val = uuid.UUID(str(project_id))
            project = Project.objects.filter(id=val).first()
        except Exception:
            pass
        if not project:
            project = Project.objects.filter(Q(reference_number=str(project_id)) | Q(name__icontains=str(project_id))).first()
        if not project:
            project = Project.objects.first()

        if not project:
            return Response({'success': False, 'message': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        inspection_id = request.data.get('inspection') or request.data.get('inspection_id')
        inspection = None
        if inspection_id:
            try:
                inspection = Inspection.objects.filter(pk=inspection_id).first()
            except Exception:
                pass

        finding_id = request.data.get('finding') or request.data.get('finding_id')
        finding = None
        if finding_id:
            try:
                finding = Finding.objects.filter(pk=finding_id).first()
            except Exception:
                pass

        reason = request.data.get('reason', 'Critical safety/building code violation.')
        severity = request.data.get('severity', 'CRITICAL')

        swo = InspectionService.issue_stop_work(
            project=project,
            reason=reason,
            severity=severity,
            actor=request.user,
            inspection=inspection,
            finding=finding
        )

        return Response({
            'success': True,
            'message': f"Stop-Work Order {swo.order_number} issued. Project {project.name} suspended.",
            'data': StopWorkOrderSerializer(swo).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        """Return SWO metrics."""
        thirty_days_ago = timezone.now() - datetime.timedelta(days=30)
        active_count = StopWorkOrder.objects.filter(status='ACTIVE').count()
        appeals_count = StopWorkOrder.objects.filter(status='APPEALED').count()
        lifted_count = StopWorkOrder.objects.filter(status='LIFTED', lifted_at__gte=thirty_days_ago).count()
        total = StopWorkOrder.objects.count()

        return Response({
            'success': True,
            'data': {
                'active': active_count,
                'pending_appeals': appeals_count,
                'lifted_30d': lifted_count,
                'total': total
            }
        })

    @action(detail=True, methods=['post'], url_path='lift')
    def lift(self, request, pk=None):
        """Lift Stop-Work Order with signature justification."""
        swo = self.get_object()
        justification = request.data.get('justification') or request.data.get('reason', 'Corrective action verified.')

        updated = InspectionService.lift_stop_work(
            swo=swo,
            justification=justification,
            actor=request.user
        )

        return Response({
            'success': True,
            'message': f"Stop-Work Order {swo.order_number} lifted. Site activities reinstated.",
            'data': StopWorkOrderSerializer(updated).data
        })


class FindingViewSet(viewsets.ModelViewSet):
    queryset = Finding.objects.all().select_related('inspection', 'project')
    serializer_class = FindingSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        severity_param = self.request.query_params.get('severity')
        resolved_param = self.request.query_params.get('is_resolved')
        project_param = self.request.query_params.get('project')

        if severity_param:
            queryset = queryset.filter(severity=severity_param.upper())

        if resolved_param in ['true', 'True', '1']:
            queryset = queryset.filter(is_resolved=True)
        elif resolved_param in ['false', 'False', '0']:
            queryset = queryset.filter(is_resolved=False)

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        return queryset

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        """Mark finding resolved."""
        finding = self.get_object()
        notes = request.data.get('notes', 'Defect rectified and verified.')
        finding.is_resolved = True
        finding.resolved_at = timezone.now()
        finding.resolution_notes = notes
        finding.save()

        return Response({
            'success': True,
            'message': f"Finding {finding.finding_reference} marked as resolved.",
            'data': FindingSerializer(finding).data
        })


class ChecklistViewSet(viewsets.ModelViewSet):
    queryset = Checklist.objects.all()
    serializer_class = ChecklistSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
