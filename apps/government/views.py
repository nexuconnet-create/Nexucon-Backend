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
        from common.permissions import scoped_projects
        from django.db.models import Q
        user = request.user
        user_email = (user.email or '').strip().lower()

        if user.is_superuser:
            app_qs = Application.objects.all()
            insp_qs = Inspection.objects.all()
        elif user_email == 'siteiq@nexucon.net':
            allowed = scoped_projects(user)
            app_qs = Application.objects.filter(
                Q(project__in=allowed) |
                Q(applicant__email__iexact='siteiq@nexucon.net') |
                Q(applicant__email__iexact='nexucon@gmail.com')
            )
            insp_qs = Inspection.objects.filter(project__in=allowed)
        else:
            allowed = scoped_projects(user)
            app_qs = Application.objects.filter(
                Q(project__in=allowed) |
                Q(applicant=user)
            ).exclude(
                Q(applicant__email__iexact='siteiq@nexucon.net') |
                Q(applicant__email__iexact='nexucon@gmail.com')
            )
            insp_qs = Inspection.objects.filter(project__in=allowed)

        applications_pending = app_qs.filter(status__in=['SUBMITTED', 'UNDER_REVIEW']).count()
        inspections_due = insp_qs.filter(status__in=['REQUESTED', 'SCHEDULED']).count()
        
        try:
            from apps.monitoring.models import SiteVerification
            sv_qs = SiteVerification.objects.all() if user.is_superuser else SiteVerification.objects.filter(project__in=scoped_projects(user))
            site_verifications_pending = sv_qs.filter(status='PENDING_VERIFICATION').count()
        except Exception:
            site_verifications_pending = 0

        try:
            from apps.bim.models import BIMModel
            bim_qs = BIMModel.objects.all() if user.is_superuser else BIMModel.objects.filter(project__in=scoped_projects(user))
            bim_models_pending = bim_qs.filter(status__in=['Under Review', 'PENDING', 'processing']).count()
        except Exception:
            bim_models_pending = 0

        return Response({
            'applications_pending_review': applications_pending,
            'inspections_due': inspections_due,
            'site_verifications_pending': site_verifications_pending,
            'bim_models_pending_review': bim_models_pending,
        })
