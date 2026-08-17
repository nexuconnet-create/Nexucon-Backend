from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from .models import Agency, Profile
from .serializers import AgencyProfileSerializer
from apps.applications.models import Application
from apps.inspections.models import Inspection

class AgencyProfileView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        if not hasattr(request.user, 'government_profile') or not request.user.government_profile.agency:
            # Auto-create profile if missing, especially for users who might have skipped onboarding in dev
            agency, _ = Agency.objects.get_or_create(
                name=f"Agency - {request.user.id}",
                defaults={
                    'code': f"AG-{str(request.user.id)[:8]}",
                    'department_name': "Default Department"
                }
            )
            if not hasattr(request.user, 'government_profile'):
                Profile.objects.create(user=request.user, agency=agency)
            else:
                profile = request.user.government_profile
                profile.agency = agency
                profile.save()
        else:
            agency = request.user.government_profile.agency
            
        serializer = AgencyProfileSerializer(agency)
        
        return Response({
            'success': True,
            'message': 'Agency profile retrieved',
            'data': serializer.data,
            'errors': None
        })

    def put(self, request):
        if not hasattr(request.user, 'government_profile') or not request.user.government_profile.agency:
            # Auto-create profile if missing, especially for users who might have skipped onboarding in dev
            agency, _ = Agency.objects.get_or_create(
                name=f"Agency - {request.user.id}",
                defaults={
                    'code': f"AG-{str(request.user.id)[:8]}",
                    'department_name': "Default Department"
                }
            )
            if not hasattr(request.user, 'government_profile'):
                Profile.objects.create(user=request.user, agency=agency)
            else:
                profile = request.user.government_profile
                profile.agency = agency
                profile.save()
        else:
            agency = request.user.government_profile.agency
            
        serializer = AgencyProfileSerializer(agency, data=request.data, partial=True)
        
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Agency profile updated successfully',
                'data': serializer.data,
                'errors': None
            })
            
        return Response({
            'success': False,
            'message': 'Failed to update agency profile',
            'data': None,
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

class QuickActionsSummaryView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        applications_pending = Application.objects.filter(status__in=['SUBMITTED', 'UNDER_REVIEW']).count()
        inspections_due = Inspection.objects.filter(status__in=['REQUESTED', 'SCHEDULED']).count()
        
        return Response({
            'applications_pending_review': applications_pending,
            'inspections_due': inspections_due,
            'site_verifications_pending': 0,
            'bim_models_pending_review': 0,
        })
