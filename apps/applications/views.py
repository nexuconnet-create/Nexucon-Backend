from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.core.exceptions import PermissionDenied
from .models import Application
from .serializers import ApplicationSerializer

class ApplicationViewSet(viewsets.ModelViewSet):
    queryset = Application.objects.all().order_by('-created_at')
    serializer_class = ApplicationSerializer

    @action(detail=False, methods=['get'], url_path='review-queue')
    def review_queue(self, request):
        applications = Application.objects.filter(
            status__in=['SUBMITTED', 'UNDER_REVIEW', 'APPROVAL_REQUESTED']
        ).order_by('submission_date', 'created_at')
        serializer = self.get_serializer(applications, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        })

    @action(detail=True, methods=['post'])
    def transition(self, request, pk=None):
        application = self.get_object()
        new_status = request.data.get('status')
        
        valid_transitions = {
            'DRAFT': ['SUBMITTED'],
            'SUBMITTED': ['UNDER_REVIEW', 'REJECTED'],
            'UNDER_REVIEW': ['REVIEW_COMPLETED', 'REJECTED'],
            'REVIEW_COMPLETED': ['APPROVAL_REQUESTED'],
            'APPROVAL_REQUESTED': ['APPROVED', 'REJECTED'],
        }
        
        if new_status not in valid_transitions.get(application.status, []):
            return Response(
                {'success': False, 'message': f"Cannot transition from {application.status} to {new_status}"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Role Checks
        user_role = getattr(getattr(request.user, 'government_profile', None), 'role', None)
        role_name = user_role.name if user_role else None
        
        if new_status in ['APPROVED', 'REJECTED'] and role_name not in ['Agency Head', 'Director']:
            return Response(
                {'success': False, 'message': "Only Agency Heads or Directors can approve/reject applications."}, 
                status=status.HTTP_403_FORBIDDEN
            )
            
        if new_status == 'APPROVAL_REQUESTED':
            # Check eligibility (e.g. required documents are uploaded, inspections passed)
            # This is a stub for where complex business logic lives
            pass
            
        application.status = new_status
        application.save()
        
        if new_status == 'APPROVED':
            project = application.project
            project.status = 'ACTIVE'
            project.save()
        
        return Response({
            'success': True,
            'message': f"Application transitioned to {new_status}",
            'data': self.get_serializer(application).data
        })
