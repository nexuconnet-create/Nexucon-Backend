from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from common.responses.standard import StandardResponse

from .serializers import (
    CustomTokenObtainPairSerializer, 
    UserRegistrationSerializer, 
    UserMeSerializer,
)
from .models import UserSession
from django.conf import settings
from rest_framework_simplejwt.exceptions import TokenError

class CustomLoginView(TokenObtainPairView):
    permission_classes = (AllowAny,)
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            access_token = response.data.get('access')
            refresh_token = response.data.get('refresh')
            user_data = response.data.get('user')
            
            # Extract device info
            user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown Device')
            ip = request.META.get('REMOTE_ADDR')
            
            # Find user
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user = User.objects.get(id=user_data['id'])
            
            # Create session
            import jwt
            decoded = jwt.decode(refresh_token, options={"verify_signature": False})
            jti = decoded.get('jti')
            
            UserSession.objects.create(
                user=user,
                device_info=user_agent,
                ip_address=ip,
                refresh_jti=jti
            )

            res = Response({
                'success': True,
                'message': 'Login successful',
                'data': {
                    'user': user_data,
                    'access': access_token,
                    'refresh': refresh_token,
                },
                'errors': None
            })
            
            res.set_cookie(
                settings.SIMPLE_JWT['AUTH_COOKIE'],
                access_token,
                max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
                secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
                httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
                samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
            )
            res.set_cookie(
                settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'],
                refresh_token,
                max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
                secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
                httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
                samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
            )
            return res
        return response


class UserRegistrationView(generics.CreateAPIView):
    permission_classes = (AllowAny,)
    serializer_class = UserRegistrationSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Optionally generate tokens immediately upon registration
        refresh = RefreshToken.for_user(user)
        # Using the custom get_token to include claims
        refresh = CustomTokenObtainPairSerializer.get_token(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)
        
        # Create session
        user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown Device')
        ip = request.META.get('REMOTE_ADDR')
        import jwt
        decoded = jwt.decode(refresh_token, options={"verify_signature": False})
        jti = decoded.get('jti')
        
        UserSession.objects.create(
            user=user,
            device_info=user_agent,
            ip_address=ip,
            refresh_jti=jti
        )
        
        res = Response({
            'success': True,
            'message': 'User registered successfully',
            'data': {
                'user': UserMeSerializer(user).data,
                'access': access_token,
                'refresh': refresh_token,
            },
            'errors': None
        }, status=status.HTTP_201_CREATED)

        res.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE'],
            access_token,
            max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
            httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
        )
        res.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'],
            refresh_token,
            max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
            httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
        )
        return res


class UserMeView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        serializer = UserMeSerializer(request.user)
        return Response({
            'success': True,
            'message': 'User profile retrieved',
            'data': serializer.data,
            'errors': None
        })

class LogoutView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        try:
            refresh_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
                
                # Invalidate session
                import jwt
                decoded = jwt.decode(refresh_token, options={"verify_signature": False})
                jti = decoded.get('jti')
                UserSession.objects.filter(refresh_jti=jti).update(is_active=False)
                
            res = Response({
                'success': True,
                'message': 'Logged out successfully'
            })
            res.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
            res.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
            return res
        except TokenError:
            res = Response({'success': False, 'message': 'Invalid token'}, status=status.HTTP_400_BAD_REQUEST)
            res.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
            res.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
            return res

class SessionListView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        sessions = UserSession.objects.filter(user=request.user, is_active=True).values(
            'id', 'device_info', 'ip_address', 'last_activity', 'login_time'
        )
        return Response({
            'success': True,
            'data': list(sessions)
        })

class RevokeSessionView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request, session_id):
        try:
            session = UserSession.objects.get(id=session_id, user=request.user)
            session.is_active = False
            session.save()
            
            # Since we can't easily fetch the unexpired RefreshToken object without the token string in simplejwt out of the box,
            # Blacklisting it relies on the custom JWT authentication class rejecting inactive sessions.
            return Response({'success': True, 'message': 'Session revoked'})
        except UserSession.DoesNotExist:
            return Response({'success': False, 'message': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)

from apps.government.models import Agency, Profile, Role
from django.contrib.auth import update_session_auth_hash

class ChangePasswordView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        user = request.user
        old_password = request.data.get('old_password')
        new_password = request.data.get('new_password')

        if not old_password or not new_password:
            return Response({'success': False, 'message': 'Both old and new password are required'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(old_password):
            return Response({'success': False, 'message': 'Incorrect old password'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()
        
        # Keep the user logged in after changing password
        update_session_auth_hash(request, user)

        return Response({'success': True, 'message': 'Password updated successfully'})

class UserOnboardingView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        user = request.user
        data = request.data
        
        # Mark as verified
        user.is_verified = True
        user.save()
        
        # Ensure Agency Head role exists with full permissions
        agency_head_role, _ = Role.objects.get_or_create(
            name="Agency Head",
            defaults={
                "permissions": [
                    "admin",
                    "projects.view", "projects.create", "projects.edit", "projects.delete",
                    "applications.view", "applications.create", "applications.approve", "applications.reject",
                    "inspections.view", "inspections.create", "inspections.update", "inspections.delete",
                    "analytics.view_industry", "all.delete", "permits.create", "permits.read", "permits.update", "permits.delete"
                ]
            }
        )
        
        # Handle Government Profile creation/update
        department_name = data.get('department', 'Default Agency')
        
        # Try to find an existing profile or create one
        if not hasattr(user, 'government_profile'):
            agency = Agency.objects.create(
                name=f"{department_name} - {user.id}",
                code=f"AG-{str(user.id)[:8]}",
                country=data.get('country'),
                state_region=data.get('stateRegion'),
                city=data.get('city'),
                department_name=department_name,
                primary_role=data.get('primaryRole', 'Agency Head'),
                jurisdiction_level=data.get('jurisdictionLevel'),
                project_scale_focus=data.get('projectScaleFocus'),
                collaboration_preference=data.get('collaborationPreference')
            )
            Profile.objects.create(
                user=user,
                agency=agency,
                role=agency_head_role
            )
        else:
            profile = user.government_profile
            if not profile.role:
                profile.role = agency_head_role
                profile.save()
            agency = profile.agency
            if agency:
                agency.country = data.get('country', agency.country)
                agency.state_region = data.get('stateRegion', agency.state_region)
                agency.city = data.get('city', agency.city)
                agency.department_name = data.get('department', agency.department_name)
                agency.primary_role = data.get('primaryRole', agency.primary_role or 'Agency Head')
                agency.jurisdiction_level = data.get('jurisdictionLevel', agency.jurisdiction_level)
                agency.project_scale_focus = data.get('projectScaleFocus', agency.project_scale_focus)
                agency.collaboration_preference = data.get('collaborationPreference', agency.collaboration_preference)
                agency.save()
        
        # Refresh user instance from DB to serialize latest profile/role state
        user.refresh_from_db()
        serializer = UserMeSerializer(user)
        return Response({
            'success': True,
            'message': 'Onboarding completed successfully',
            'data': serializer.data,
            'errors': None
        })
