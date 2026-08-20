from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from .models import Application
from .serializers import ApplicationSerializer, ApplicationCreateSerializer
from .services import ApplicationService

User = get_user_model()

class ApplicationViewSet(viewsets.ModelViewSet):
    queryset = Application.objects.all().select_related('project', 'applicant', 'permit')
    serializer_class = ApplicationSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return ApplicationCreateSerializer
        return ApplicationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        project_param = self.request.query_params.get('project')
        priority_param = self.request.query_params.get('priority')
        type_param = self.request.query_params.get('type') or self.request.query_params.get('application_type')
        search_param = self.request.query_params.get('search')

        if status_param:
            status_param = status_param.upper()
            if status_param == 'SUBMITTED':
                queryset = queryset.filter(status='SUBMITTED')
            elif status_param in ['REVIEW', 'UNDER_REVIEW']:
                queryset = queryset.filter(status__in=['UNDER_REVIEW', 'REVIEW_COMPLETED', 'APPROVAL_REQUESTED'])
            elif status_param in ['CONDITIONAL', 'CONDITIONAL_APPROVAL']:
                queryset = queryset.filter(status='CONDITIONAL_APPROVAL')
            elif status_param == 'APPROVED':
                queryset = queryset.filter(status='APPROVED')
            elif status_param == 'REJECTED':
                queryset = queryset.filter(status='REJECTED')
            elif status_param == 'EXPIRED':
                queryset = queryset.filter(status__in=['EXPIRED', 'RENEWED'])
            elif status_param not in ['PERMITS', 'ALL']:
                queryset = queryset.filter(status=status_param)

        if project_param:
            queryset = queryset.filter(project_id=project_param)

        if priority_param:
            queryset = queryset.filter(priority__iexact=priority_param)

        if type_param:
            queryset = queryset.filter(application_type__icontains=type_param)

        if search_param:
            queryset = queryset.filter(
                Q(application_reference__icontains=search_param) |
                Q(title__icontains=search_param) |
                Q(project__name__icontains=search_param) |
                Q(applicant__email__icontains=search_param) |
                Q(applicant__first_name__icontains=search_param) |
                Q(applicant__last_name__icontains=search_param)
            )

        return queryset

    def perform_create(self, serializer):
        data = serializer.validated_data
        application = ApplicationService.create_application(
            data=data,
            user=self.request.user
        )
        return application

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = self.perform_create(serializer)
        out_serializer = ApplicationSerializer(application)
        return Response({
            'success': True,
            'message': 'Application created successfully',
            'data': out_serializer.data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        """Return counts for dashboard tabs and overview cards."""
        total = Application.objects.count()
        submitted = Application.objects.filter(status='SUBMITTED').count()
        under_review = Application.objects.filter(status__in=['UNDER_REVIEW', 'REVIEW_COMPLETED', 'APPROVAL_REQUESTED']).count()
        conditional = Application.objects.filter(status='CONDITIONAL_APPROVAL').count()
        approved = Application.objects.filter(status='APPROVED').count()
        rejected = Application.objects.filter(status='REJECTED').count()
        expired = Application.objects.filter(status__in=['EXPIRED', 'RENEWED']).count()

        return Response({
            'success': True,
            'data': {
                'total': total,
                'submitted': submitted,
                'under_review': under_review,
                'conditional': conditional,
                'approved': approved,
                'rejected': rejected,
                'expired': expired
            }
        })

    @action(detail=False, methods=['get'], url_path='review-queue')
    def review_queue(self, request):
        """Queue for reviewing officers."""
        applications = Application.objects.filter(
            status__in=['SUBMITTED', 'UNDER_REVIEW', 'REVIEW_COMPLETED', 'APPROVAL_REQUESTED']
        ).select_related('project', 'applicant').order_by('submission_date', 'created_at')
        serializer = ApplicationSerializer(applications, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        })

    @action(detail=True, methods=['post'])
    def transition(self, request, pk=None):
        """Execute a state transition."""
        application = self.get_object()
        new_status = request.data.get('status')
        reason = request.data.get('reason') or request.data.get('notes')
        conditions = request.data.get('conditions')

        if not new_status:
            return Response({'success': False, 'message': 'Target status is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            updated = ApplicationService.transition_status(
                application=application,
                new_status=new_status,
                user=request.user,
                reason=reason,
                conditions=conditions
            )
            return Response({
                'success': True,
                'message': f"Application transitioned to {new_status}",
                'data': ApplicationSerializer(updated).data
            })
        except ValidationError as e:
            return Response({'success': False, 'message': str(e.message if hasattr(e, 'message') else e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'success': False, 'message': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='assign-reviewer')
    def assign_reviewer(self, request, pk=None):
        """Assign an officer to review this application."""
        application = self.get_object()
        reviewer_id = request.data.get('reviewer_id')
        reviewer_name = request.data.get('reviewer_name')
        deadline = request.data.get('review_deadline')

        reviewer_user = None
        if reviewer_id:
            try:
                reviewer_user = User.objects.get(pk=reviewer_id)
            except User.DoesNotExist:
                pass

        if not reviewer_user and not reviewer_name:
            # Fallback to current user if assigning self
            reviewer_user = request.user

        if reviewer_user:
            updated = ApplicationService.assign_reviewer(
                application=application,
                reviewer_user=reviewer_user,
                actor=request.user,
                deadline=deadline
            )
        else:
            application.assigned_reviewer_name = reviewer_name
            if deadline:
                application.review_deadline = deadline
            if application.status == 'SUBMITTED':
                application.status = 'UNDER_REVIEW'
            application.save()
            updated = application

        return Response({
            'success': True,
            'message': f"Reviewer assigned successfully",
            'data': ApplicationSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='request-docs')
    def request_docs(self, request, pk=None):
        """Request additional documents from applicant."""
        application = self.get_object()
        document_items = request.data.get('document_items', [])
        instructions = request.data.get('instructions', '')

        if not document_items:
            return Response({'success': False, 'message': 'At least one document item is required.'}, status=status.HTTP_400_BAD_REQUEST)

        updated = ApplicationService.request_additional_documents(
            application=application,
            document_items=document_items,
            instructions=instructions,
            actor=request.user
        )

        return Response({
            'success': True,
            'message': 'Document request dispatched to applicant',
            'data': ApplicationSerializer(updated).data
        })

    @action(detail=True, methods=['post'], url_path='update-review-item')
    def update_review_item(self, request, pk=None):
        """Update checklist item verification status."""
        application = self.get_object()
        item_id = request.data.get('item_id')
        item_status = request.data.get('status') # 'PASSED', 'FAILED', 'PENDING'
        notes = request.data.get('notes', '')

        review_items = list(application.review_items or [])
        updated_any = False
        for item in review_items:
            if item.get('id') == item_id:
                item['status'] = item_status
                if notes:
                    item['notes'] = notes
                updated_any = True
                break

        if not updated_any:
            review_items.append({
                'id': item_id or f"item_{len(review_items)+1}",
                'name': request.data.get('name', 'Custom Checklist Item'),
                'status': item_status,
                'notes': notes
            })

        application.review_items = review_items
        application.save()

        return Response({
            'success': True,
            'message': 'Review checklist item updated',
            'data': ApplicationSerializer(application).data
        })
