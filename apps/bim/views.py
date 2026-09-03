from rest_framework import viewsets, status, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q, Count
from common.permissions import scoped_projects
from .models import (
    BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, 
    BIMProgressValidation, BIMConstructionMilestone
)
from .serializers import (
    BIMModelSerializer, BIMModelVersionSerializer, BIMClashSerializer,
    BIMAnnotationSerializer, BIMProgressValidationSerializer,
    BIMConstructionMilestoneSerializer
)
from .services import BIMService


def _assert_project_scope(request, project, project_id=None):
    """
    Multi-tenant guard for write actions: the target project must be inside
    the requesting user's scoped_projects (plan §8 district isolation).
    """
    if request.user.is_superuser:
        return
    if project is None and project_id:
        from apps.projects.models import Project
        project = Project.objects.filter(pk=project_id).first()
    if project is None or not scoped_projects(request.user).filter(pk=project.pk).exists():
        raise PermissionDenied("Target project is outside your assigned scope.")


class BIMModelViewSet(viewsets.ModelViewSet):
    queryset = BIMModel.objects.all().select_related('project')
    serializer_class = BIMModelSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — a user only reaches BIM models on projects
        # within their scope. Cross-project detail lookups 404.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(project__in=scoped_projects(self.request.user))
        discipline = self.request.query_params.get('discipline')
        status_val = self.request.query_params.get('status')
        project_id = self.request.query_params.get('project')
        certified = self.request.query_params.get('certified')
        search = self.request.query_params.get('search')

        if discipline:
            qs = qs.filter(discipline__iexact=discipline)
        if status_val:
            qs = qs.filter(status__iexact=status_val)
        if project_id:
            qs = qs.filter(project_id=project_id)
        if certified:
            qs = qs.filter(is_digitally_certified=True)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(model_reference__icontains=search) |
                Q(project__name__icontains=search) |
                Q(discipline__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        _assert_project_scope(self.request, project)
        model = BIMService.upload_model(serializer.validated_data, self.request.user)
        serializer.instance = model

    @action(detail=True, methods=['post'], url_path='certify')
    def certify(self, request, pk=None):
        model = self.get_object()
        hash_signature = request.data.get('hash_signature')
        certified_model = BIMService.stamp_and_certify(model, request.user, hash_signature)
        return Response(BIMModelSerializer(certified_model).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='request-changes')
    def request_changes(self, request, pk=None):
        model = self.get_object()
        reason = request.data.get('reason', 'Design modification requested by reviewing officer.')
        updated_model = BIMService.request_changes(model, reason, request.user)
        return Response(BIMModelSerializer(updated_model).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='create-version')
    def create_version(self, request, pk=None):
        model = self.get_object()
        version = BIMService.create_version(model, request.data, request.user)
        return Response(BIMModelVersionSerializer(version).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='approved-models')
    def approved_models(self, request):
        certified = self.get_queryset().filter(is_digitally_certified=True)
        serializer = self.get_serializer(certified, many=True)
        return Response(serializer.data)


class BIMModelVersionViewSet(viewsets.ModelViewSet):
    queryset = BIMModelVersion.objects.all().select_related('model')
    serializer_class = BIMModelVersionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — versions inherit their model's project scope.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(model__project__in=scoped_projects(self.request.user))
        model_id = self.request.query_params.get('model')
        if model_id:
            qs = qs.filter(model_id=model_id)
        return qs

    @action(detail=False, methods=['post'], url_path='compare')
    def compare_versions(self, request):
        v1_id = request.data.get('version_a')
        v2_id = request.data.get('version_b')
        v1 = self.get_queryset().filter(pk=v1_id).first()
        v2 = self.get_queryset().filter(pk=v2_id).first()

        if not v1 or not v2:
            return Response({"error": "Both version IDs are required."}, status=status.HTTP_400_BAD_REQUEST)

        diff = {
            "version_a": BIMModelVersionSerializer(v1).data,
            "version_b": BIMModelVersionSerializer(v2).data,
            "elements_added": abs(v2.stats_added - v1.stats_added),
            "elements_modified": abs(v2.stats_modified - v1.stats_modified),
            "elements_removed": abs(v2.stats_removed - v1.stats_removed),
            "status": "Diff computation completed successfully."
        }
        return Response(diff, status=status.HTTP_200_OK)


class BIMClashViewSet(viewsets.ModelViewSet):
    queryset = BIMClash.objects.all().select_related('project', 'primary_model', 'secondary_model')
    serializer_class = BIMClashSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — clashes stay within the user's project scope.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(project__in=scoped_projects(self.request.user))
        project_id = self.request.query_params.get('project')
        severity = self.request.query_params.get('severity')
        status_val = self.request.query_params.get('status')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        model_id = self.request.query_params.get('model') or self.request.query_params.get('primary_model')
        if model_id:
            qs = qs.filter(Q(primary_model_id=model_id) | Q(secondary_model_id=model_id))
        if severity:
            qs = qs.filter(severity=severity.upper())
        if status_val:
            qs = qs.filter(status=status_val.upper())
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(clash_reference__icontains=search) |
                Q(description__icontains=search)
            )
        return qs

    @action(detail=False, methods=['post'], url_path='run-matrix')
    def run_matrix(self, request):
        project_id = request.data.get('project')
        primary_model_id = request.data.get('primary_model')
        secondary_model_id = request.data.get('secondary_model')

        if not project_id or not primary_model_id:
            return Response({"error": "project and primary_model are required."}, status=status.HTTP_400_BAD_REQUEST)

        _assert_project_scope(request, None, project_id=project_id)
        clash = BIMService.run_clash_matrix(project_id, primary_model_id, secondary_model_id, request.user)
        return Response(BIMClashSerializer(clash).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='convert-to-issue')
    def convert_to_issue(self, request, pk=None):
        clash = self.get_object()
        site_issue = BIMService.convert_clash_to_site_issue(clash, request.user)
        return Response({
            "message": "Clash successfully converted into active site defect issue.",
            "site_issue_id": str(site_issue.id),
            "clash": BIMClashSerializer(clash).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        clash = self.get_object()
        clash.status = 'RESOLVED'
        clash.resolution_notes = request.data.get('resolution_notes', 'Resolved during design coordination.')
        clash.save()
        return Response(BIMClashSerializer(clash).data, status=status.HTTP_200_OK)


class BIMAnnotationViewSet(viewsets.ModelViewSet):
    queryset = BIMAnnotation.objects.all().select_related('model', 'project')
    serializer_class = BIMAnnotationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — annotations stay within the user's project scope.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(project__in=scoped_projects(self.request.user))
        status_val = self.request.query_params.get('status')
        priority = self.request.query_params.get('priority')
        model_id = self.request.query_params.get('model')
        search = self.request.query_params.get('search')

        if status_val and status_val != 'all':
            qs = qs.filter(status__iexact=status_val)
        if priority:
            qs = qs.filter(priority__iexact=priority)
        if model_id:
            qs = qs.filter(model_id=model_id)
        if search:
            qs = qs.filter(
                Q(text__icontains=search) |
                Q(annotation_reference__icontains=search) |
                Q(author_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        model_id = self.request.data.get('model')
        model = BIMModel.objects.get(pk=model_id)
        _assert_project_scope(self.request, model.project)
        annotation = BIMService.add_annotation(model, serializer.validated_data, self.request.user)
        serializer.instance = annotation

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        annotation = self.get_object()
        notes = request.data.get('notes', 'Resolved.')
        resolved = BIMService.resolve_annotation(annotation, notes, request.user)
        return Response(BIMAnnotationSerializer(resolved).data, status=status.HTTP_200_OK)


class BIMProgressValidationViewSet(viewsets.ModelViewSet):
    queryset = BIMProgressValidation.objects.all().select_related('project', 'model')
    serializer_class = BIMProgressValidationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — validations stay within the user's project scope.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(project__in=scoped_projects(self.request.user))
        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs

    @action(detail=False, methods=['post'], url_path='simulate')
    def simulate(self, request):
        project_id = request.data.get('project') or request.data.get('project_id')
        if not project_id:
            return Response({"error": "project is required."}, status=status.HTTP_400_BAD_REQUEST)
        _assert_project_scope(request, None, project_id=project_id)
        try:
            validation = BIMService.run_timeline_simulation(project_id, request.user)
            return Response(BIMProgressValidationSerializer(validation).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class BIMConstructionMilestoneViewSet(viewsets.ModelViewSet):
    """
    ViewSet for BIM & Model Review Construction Milestones and Verification Gates.
    """
    queryset = BIMConstructionMilestone.objects.all().select_related('project', 'bim_model', 'model_version', 'linked_construction_milestone')
    serializer_class = BIMConstructionMilestoneSerializer
    permission_classes = [permissions.IsAuthenticated]


    def get_queryset(self):
        qs = super().get_queryset()
        # Multi-tenant scoping — milestones stay within the user's project scope.
        if self.request.user.is_authenticated and not self.request.user.is_superuser:
            qs = qs.filter(project__in=scoped_projects(self.request.user))
        project_id = self.request.query_params.get('project')
        bim_model_id = self.request.query_params.get('bim_model')
        phase = self.request.query_params.get('phase')
        status_val = self.request.query_params.get('status')
        verification_status = self.request.query_params.get('verification_status')
        search = self.request.query_params.get('search')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if bim_model_id:
            qs = qs.filter(bim_model_id=bim_model_id)
        if phase:
            qs = qs.filter(phase__iexact=phase)
        if status_val:
            qs = qs.filter(verification_status__iexact=status_val)
        if verification_status:
            qs = qs.filter(verification_status__iexact=verification_status)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(milestone_code__icontains=search) |
                Q(bim_model__name__icontains=search) |
                Q(project__name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        _assert_project_scope(self.request, project)
        milestone = BIMService.create_bim_milestone(serializer.validated_data, self.request.user)
        serializer.instance = milestone

    @action(detail=True, methods=['get'], url_path='gate-status')
    def gate_status(self, request, pk=None):
        milestone = self.get_object()
        status_data = BIMService.evaluate_milestone_gate_status(milestone)
        return Response(status_data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='verify')
    def verify(self, request, pk=None):
        milestone = self.get_object()
        notes = request.data.get('notes', '')
        try:
            verified_ms = BIMService.verify_and_stamp_milestone(milestone, request.user, notes)
            return Response(BIMConstructionMilestoneSerializer(verified_ms).data, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='flag-deviation')
    def flag_deviation(self, request, pk=None):
        milestone = self.get_object()
        flagged_ms = BIMService.flag_milestone_deviation(milestone, request.user, request.data)
        return Response(BIMConstructionMilestoneSerializer(flagged_ms).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='request-re-verification')
    def request_re_verification(self, request, pk=None):
        milestone = self.get_object()
        reason = request.data.get('reason', 'Re-verification required due to design or physical deviation modifications.')
        reopened_ms = BIMService.request_milestone_re_verification(milestone, request.user, reason)
        return Response(BIMConstructionMilestoneSerializer(reopened_ms).data, status=status.HTTP_200_OK)


class BIMStatsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['get'], url_path='overview')
    def overview(self, request):
        # Multi-tenant scoping — stats only aggregate the user's project scope.
        if request.user.is_authenticated and not request.user.is_superuser:
            projects = scoped_projects(request.user)
            models_qs = BIMModel.objects.filter(project__in=projects)
            clashes_qs = BIMClash.objects.filter(project__in=projects)
            annotations_qs = BIMAnnotation.objects.filter(project__in=projects)
            milestones_qs = BIMConstructionMilestone.objects.filter(project__in=projects)
            validations_qs = BIMProgressValidation.objects.filter(project__in=projects)
        else:
            models_qs = BIMModel.objects.all()
            clashes_qs = BIMClash.objects.all()
            annotations_qs = BIMAnnotation.objects.all()
            milestones_qs = BIMConstructionMilestone.objects.all()
            validations_qs = BIMProgressValidation.objects.all()

        total_models = models_qs.count()
        active_models = models_qs.filter(status='Active').count()
        under_review = models_qs.filter(status='Under Review').count()
        certified_models = models_qs.filter(is_digitally_certified=True).count()

        active_clashes = clashes_qs.filter(status='OPEN').count()
        critical_clashes = clashes_qs.filter(severity='CRITICAL', status='OPEN').count()

        open_annotations = annotations_qs.filter(status='Open').count()
        in_progress_annotations = annotations_qs.filter(status='In Progress').count()
        resolved_annotations = annotations_qs.filter(status='Resolved').count()

        latest_validation = validations_qs.first()

        total_milestones = milestones_qs.count()
        verified_milestones = milestones_qs.filter(verification_status__in=['VERIFIED', 'COMPLETED']).count()
        deviation_milestones = milestones_qs.filter(verification_status='DEVIATION_FLAGGED').count()

        # No fabricated fallback figures: with no validation on record the
        # 4D progress fields are reported as unknown (None), not invented.
        data = {
            "models": {
                "total": total_models,
                "active": active_models,
                "under_review": under_review,
                "certified": certified_models,
            },
            "clashes": {
                "active": active_clashes,
                "critical": critical_clashes,
                "hard_clash": clashes_qs.filter(clash_type='HARD_CLASH', status='OPEN').count(),
            },
            "annotations": {
                "open": open_annotations,
                "in_progress": in_progress_annotations,
                "resolved": resolved_annotations,
                "total": annotations_qs.count()
            },
            "milestones": {
                "total": total_milestones,
                "verified": verified_milestones,
                "deviations_flagged": deviation_milestones,
                "pending_review": milestones_qs.filter(verification_status='PENDING_REVIEW').count()
            },
            "progress_4d": {
                "schedule_status": latest_validation.schedule_status if latest_validation else None,
                "days_variance": latest_validation.days_variance if latest_validation else None,
                "completed_elements": latest_validation.completed_elements_count if latest_validation else None,
                "earned_value": latest_validation.earned_value_usd if latest_validation else None
            }
        }
        return Response(data, status=status.HTTP_200_OK)

