from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly
from django.db.models import Q
from django.utils import timezone
import datetime

from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
from .serializers import (
    DailySiteUpdateSerializer, FieldObservationSerializer,
    SiteIssueSerializer, ConstructionMilestoneSerializer,
    SiteVerificationSerializer
)
from .services import MonitoringService

class DailySiteUpdateViewSet(viewsets.ModelViewSet):
    queryset = DailySiteUpdate.objects.all().select_related('project', 'reported_by')
    serializer_class = DailySiteUpdateSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        project_param = self.request.query_params.get('project')
        type_param = self.request.query_params.get('type')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if type_param:
            queryset = queryset.filter(update_type__iexact=type_param)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(update_reference__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(reported_by_name__icontains=search_param) |
                Q(work_summary__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return MonitoringService.log_daily_update(
            data=self.request.data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        update = self.perform_create(None)
        return Response({
            'success': True,
            'message': 'Daily site update logged successfully',
            'data': DailySiteUpdateSerializer(update).data
        }, status=status.HTTP_201_CREATED)


class FieldObservationViewSet(viewsets.ModelViewSet):
    queryset = FieldObservation.objects.all().select_related('project', 'assigned_officer')
    serializer_class = FieldObservationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        project_param = self.request.query_params.get('project')
        category_param = self.request.query_params.get('category')
        severity_param = self.request.query_params.get('severity')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if category_param:
            queryset = queryset.filter(category__iexact=category_param)

        if severity_param:
            queryset = queryset.filter(severity__iexact=severity_param)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(observation_reference__icontains=search_param) |
                Q(title__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(description__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return MonitoringService.create_observation(
            data=self.request.data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        obs = self.perform_create(None)
        return Response({
            'success': True,
            'message': 'Field observation recorded successfully',
            'data': FieldObservationSerializer(obs).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        obs = self.get_object()
        notes = request.data.get('notes', 'Field observation resolved after site corrective check.')
        updated = MonitoringService.resolve_observation(
            observation=obs,
            notes=notes,
            actor=request.user
        )
        return Response({
            'success': True,
            'message': f"Observation {obs.observation_reference} resolved",
            'data': FieldObservationSerializer(updated).data
        })


class SiteIssueViewSet(viewsets.ModelViewSet):
    queryset = SiteIssue.objects.all().select_related('project')
    serializer_class = SiteIssueSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        project_param = self.request.query_params.get('project')
        severity_param = self.request.query_params.get('severity')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if severity_param:
            queryset = queryset.filter(severity__iexact=severity_param)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(issue_reference__icontains=search_param) |
                Q(title__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(description__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return MonitoringService.report_issue(
            data=self.request.data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        issue = self.perform_create(None)
        return Response({
            'success': True,
            'message': 'Site issue reported successfully',
            'data': SiteIssueSerializer(issue).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='escalate')
    def escalate(self, request, pk=None):
        issue = self.get_object()
        updated = MonitoringService.escalate_issue(issue, actor=request.user)
        return Response({
            'success': True,
            'message': f"Issue {issue.issue_reference} escalated to Directorate",
            'data': SiteIssueSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        issue = self.get_object()
        notes = request.data.get('notes', 'Site defect rectified and verified.')
        evidence = request.data.get('evidence', [])
        updated = MonitoringService.resolve_issue(issue, notes=notes, evidence=evidence, actor=request.user)
        return Response({
            'success': True,
            'message': f"Issue {issue.issue_reference} marked as resolved",
            'data': SiteIssueSerializer(updated).data
        })


class ConstructionMilestoneViewSet(viewsets.ModelViewSet):
    queryset = ConstructionMilestone.objects.all().select_related('project')
    serializer_class = ConstructionMilestoneSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        project_param = self.request.query_params.get('project')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(name__icontains=search_param) |
                Q(project__name__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return MonitoringService.create_milestone(
            data=self.request.data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        milestone = self.perform_create(None)
        return Response({
            'success': True,
            'message': 'Milestone created successfully',
            'data': ConstructionMilestoneSerializer(milestone).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='verify')
    def verify(self, request, pk=None):
        milestone = self.get_object()
        updated = MonitoringService.verify_milestone(milestone, actor=request.user)
        return Response({
            'success': True,
            'message': f"Milestone {milestone.name} signed off as verified",
            'data': ConstructionMilestoneSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='flag-delay')
    def flag_delay(self, request, pk=None):
        milestone = self.get_object()
        reason = request.data.get('reason', 'Construction progress pacing delay.')
        updated = MonitoringService.flag_milestone_delay(milestone, reason=reason, actor=request.user)
        return Response({
            'success': True,
            'message': f"Milestone {milestone.name} flagged as delayed",
            'data': ConstructionMilestoneSerializer(updated).data
        })


class SiteVerificationViewSet(viewsets.ModelViewSet):
    queryset = SiteVerification.objects.all().select_related('project')
    serializer_class = SiteVerificationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        project_param = self.request.query_params.get('project')
        method_param = self.request.query_params.get('method')
        status_param = self.request.query_params.get('status')
        search_param = self.request.query_params.get('search')

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if method_param:
            queryset = queryset.filter(method__iexact=method_param)

        if status_param:
            queryset = queryset.filter(status__iexact=status_param)

        if search_param:
            queryset = queryset.filter(
                Q(verification_reference__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(device_identifier__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        return MonitoringService.record_site_verification(
            data=self.request.data,
            user=self.request.user
        )

    def create(self, request, *args, **kwargs):
        vrf = self.perform_create(None)
        return Response({
            'success': True,
            'message': 'Site verification recorded successfully',
            'data': SiteVerificationSerializer(vrf).data
        }, status=status.HTTP_201_CREATED)


from apps.projects.models import Project

class MonitoringStatsViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticatedOrReadOnly]

    def list(self, request):
        return self.overview(request)

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        """Return real-time aggregated stats across all 6 monitoring tabs."""
        today = timezone.now().date()
        week_ahead = today + datetime.timedelta(days=7)

        # Tab 1: Live Site View
        active_sites_count = Project.objects.filter(status__in=['ACTIVE', 'IN_PROGRESS', 'UNDER_CONSTRUCTION']).count()
        if active_sites_count == 0:
            active_sites_count = Project.objects.exclude(status__in=['SUSPENDED', 'CANCELLED', 'ARCHIVED']).count()

        daily_updates = list(DailySiteUpdate.objects.all())
        daily_photos_count = sum(len(u.photos or []) for u in daily_updates if u.update_type == 'DAILY_PHOTO')
        if daily_photos_count == 0:
            daily_photos_count = DailySiteUpdate.objects.filter(update_type='DAILY_PHOTO').count()
        
        drone_surveys_count = DailySiteUpdate.objects.filter(update_type='DRONE_SURVEY').count()
        active_observations_count = FieldObservation.objects.filter(status__in=['OPEN', 'UNDER_REVIEW', 'ACTION_REQUIRED']).count()

        # Tab 2: Site Progress
        delayed_milestones = ConstructionMilestone.objects.filter(Q(status='DELAYED') | Q(is_delayed=True)).count()
        on_schedule_sites = max(0, active_sites_count - delayed_milestones) if active_sites_count > 0 else 0
        verified_milestones = ConstructionMilestone.objects.filter(status='VERIFIED').count()
        progress_reports_count = DailySiteUpdate.objects.count()

        # Tab 3: Field Observations
        quality_obs_count = FieldObservation.objects.filter(category='QUALITY').count()
        safety_obs_count = FieldObservation.objects.filter(category='SAFETY').count()
        resolved_obs_count = FieldObservation.objects.filter(status__in=['RESOLVED', 'CLOSED']).count()

        # Tab 4: Site Issues
        open_issues_count = SiteIssue.objects.filter(status__in=['OPEN', 'IN_PROGRESS', 'UNDER_REVIEW']).count()
        critical_issues_count = SiteIssue.objects.filter(severity='CRITICAL').count()
        under_review_issues_count = SiteIssue.objects.filter(status='UNDER_REVIEW').count()
        resolved_issues_count = SiteIssue.objects.filter(status__in=['RESOLVED', 'CLOSED']).count()

        # Tab 5: Construction Milestones
        milestones_due_this_week = ConstructionMilestone.objects.filter(target_date__gte=today, target_date__lte=week_ahead).count()
        milestones_upcoming = ConstructionMilestone.objects.filter(status='UPCOMING').count()

        # Tab 6: Site Verification
        pending_verifications = SiteVerification.objects.filter(status='PENDING_VERIFICATION').count()
        verified_verifications = SiteVerification.objects.filter(status='VERIFIED').count()
        variance_detected = SiteVerification.objects.filter(Q(status='VARIANCE_DETECTED') | Q(variance_detected=True)).count()
        active_devices = SiteVerification.objects.values('device_identifier').distinct().count()

        return Response({
            'success': True,
            'data': {
                'live': {
                    'active_sites': active_sites_count,
                    'daily_photos': daily_photos_count,
                    'drone_surveys': drone_surveys_count,
                    'active_observations': active_observations_count
                },
                'progress': {
                    'on_schedule': on_schedule_sites,
                    'delayed': delayed_milestones,
                    'milestone_reached': verified_milestones,
                    'progress_reports': progress_reports_count
                },
                'observations': {
                    'active': active_observations_count,
                    'quality': quality_obs_count,
                    'safety': safety_obs_count,
                    'resolved': resolved_obs_count
                },
                'issues': {
                    'open': open_issues_count,
                    'critical': critical_issues_count,
                    'under_review': under_review_issues_count,
                    'resolved': resolved_issues_count
                },
                'milestones': {
                    'due_this_week': milestones_due_this_week,
                    'verified': verified_milestones,
                    'delayed': delayed_milestones,
                    'upcoming': milestones_upcoming
                },
                'verification': {
                    'pending': pending_verifications,
                    'verified': verified_verifications,
                    'variance_detected': variance_detected,
                    'active_devices': active_devices
                }
            }
        })


class SiteProgressViewSet(viewsets.ViewSet):
    """
    Endpoints for physical construction progress, programme breakdowns, 
    and schedule tracking across active projects.
    """
    permission_classes = [IsAuthenticatedOrReadOnly]

    def list(self, request):
        """Get physical construction progress details across all active projects."""
        project_param = request.query_params.get('project')
        data = MonitoringService.get_project_progress_details(project_param)
        return Response({
            'success': True,
            'data': data
        })

    def retrieve(self, request, pk=None):
        """Get deep progress details for a specific project."""
        data = MonitoringService.get_project_progress_details(pk)
        return Response({
            'success': True,
            'data': data
        })

    @action(detail=False, methods=['post'], url_path='update')
    def update_progress(self, request):
        """Update progress percentage and create a progress log in database."""
        data = MonitoringService.update_project_progress(request.data, request.user)
        return Response({
            'success': True,
            'message': 'Project progress updated successfully',
            'data': data
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='flag-delay')
    def flag_delay(self, request):
        """Flag construction delay and record a non-conformance notice."""
        issue = MonitoringService.flag_project_schedule_delay(request.data, request.user)
        return Response({
            'success': True,
            'message': 'Schedule delay notice recorded',
            'data': {
                'issue_id': str(issue.id),
                'reference': issue.issue_reference,
                'title': issue.title
            }
        }, status=status.HTTP_201_CREATED)

