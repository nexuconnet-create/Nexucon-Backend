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
from .models import UserSession, EmailVerificationToken
from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from apps.notifications.email_service import EmailService


class CustomLoginView(TokenObtainPairView):
    permission_classes = (AllowAny,)
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        # --- Two-factor enforcement (plan §5 Week 6: mobile JWT + 2FA) -----
        from django.contrib.auth import get_user_model
        from .models import TwoFactorSecret
        from .two_factor import verify_code

        email = (request.data.get('email') or '').strip().lower()
        if email:
            User = get_user_model()
            user = User.objects.filter(email__iexact=email).first()
            if user and user.is_active:
                two_factor = getattr(user, 'two_factor', None)
                if two_factor and two_factor.is_enabled:
                    code = request.data.get('totp_code')
                    if not code:
                        return Response({
                            'success': False,
                            'message': 'Two-factor authentication code required.',
                            'data': {'mfa_required': True, 'mfa_method': 'totp'},
                            'errors': [{'field': 'totp_code', 'message': 'Provide the 6-digit code from your authenticator app.'}],
                        }, status=status.HTTP_400_BAD_REQUEST)
                    if not verify_code(two_factor.secret, str(code),
                                       last_used_counter=two_factor.last_used_counter):
                        return Response({
                            'success': False,
                            'message': 'Invalid or expired two-factor code.',
                            'data': {'mfa_required': True, 'mfa_method': 'totp'},
                            'errors': [{'field': 'totp_code', 'message': 'Invalid or expired code.'}],
                        }, status=status.HTTP_400_BAD_REQUEST)
                    # Mark the step used so the code cannot be replayed.
                    import time as _time
                    two_factor.last_used_counter = int(_time.time()) // 30
                    two_factor.save(update_fields=['last_used_counter', 'updated_at'])

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
        user.is_verified = False
        user.save(update_fields=['is_verified'])

        # Generate 6-digit email verification token
        otp_token = EmailVerificationToken.generate_token(email=user.email, user=user)

        # Dispatch verification email via Resend
        full_name = f"{user.first_name} {user.last_name}".strip() or user.username
        try:
            EmailService.send_verification_otp_email(
                email=user.email,
                name=full_name,
                otp_code=otp_token.code,
                expires_minutes=15
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send verification email to {user.email}: {e}")

        return Response({
            'success': True,
            'message': 'Registration initiated. We have sent a 6-digit verification code to your email address.',
            'data': {
                'user': UserMeSerializer(user).data,
                'email': user.email,
                'requires_verification': True,
                'is_verified': False,
            },
            'errors': None
        }, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    """
    POST /api/v1/auth/verify-email/
    Receives { email, code } to confirm email verification and complete registration.
    On success, issues JWT tokens and active UserSession.
    """
    permission_classes = (AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        code = str(request.data.get('code') or request.data.get('otp') or '').strip()

        if not email or not code:
            return Response({
                'success': False,
                'message': 'Email address and verification code are required.',
                'errors': [{'field': 'code', 'message': 'Email and 6-digit code are required.'}]
            }, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({
                'success': False,
                'message': 'No account associated with this email address.',
                'errors': [{'field': 'email', 'message': 'Account not found.'}]
            }, status=status.HTTP_404_NOT_FOUND)

        token = EmailVerificationToken.objects.filter(
            email__iexact=email,
            is_used=False
        ).order_by('-created_at').first()

        if not token or timezone.now() > token.expires_at:
            return Response({
                'success': False,
                'message': 'Verification code has expired or is invalid. Please request a new code.',
                'errors': [{'field': 'code', 'message': 'Expired or invalid code.'}]
            }, status=status.HTTP_400_BAD_REQUEST)

        if token.attempts >= 5:
            return Response({
                'success': False,
                'message': 'Too many failed verification attempts. Please request a new code.',
                'errors': [{'field': 'code', 'message': 'Maximum attempts exceeded.'}]
            }, status=status.HTTP_400_BAD_REQUEST)

        if token.code != code:
            token.attempts += 1
            token.save(update_fields=['attempts'])
            return Response({
                'success': False,
                'message': 'Invalid verification code. Please check and try again.',
                'errors': [{'field': 'code', 'message': 'Incorrect code.'}]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Code matched! Mark used & user verified
        token.is_used = True
        token.save(update_fields=['is_used'])

        user.is_verified = True
        user.save(update_fields=['is_verified'])

        # Generate JWT tokens
        refresh = CustomTokenObtainPairSerializer.get_token(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        # Create active user session
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
            'message': 'Email address verified successfully. Welcome to Nexucon!',
            'data': {
                'user': UserMeSerializer(user).data,
                'access': access_token,
                'refresh': refresh_token,
            },
            'errors': None
        }, status=status.HTTP_200_OK)

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


class ResendVerificationCodeView(APIView):
    """
    POST /api/v1/auth/resend-verification/
    Receives { email } to dispatch a new 6-digit OTP email.
    """
    permission_classes = (AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        if not email:
            return Response({
                'success': False,
                'message': 'Email address is required.',
                'errors': [{'field': 'email', 'message': 'Email is required.'}]
            }, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({
                'success': False,
                'message': 'No account associated with this email address.',
                'errors': [{'field': 'email', 'message': 'Account not found.'}]
            }, status=status.HTTP_404_NOT_FOUND)

        if user.is_verified:
            return Response({
                'success': True,
                'message': 'Your email address is already verified. You can proceed to log in.',
                'data': {'already_verified': True}
            })

        # Rate-limiting: minimum 30s between consecutive OTP dispatches
        last_token = EmailVerificationToken.objects.filter(
            email__iexact=email
        ).order_by('-created_at').first()
        if last_token and (timezone.now() - last_token.created_at).total_seconds() < 30:
            wait_sec = int(30 - (timezone.now() - last_token.created_at).total_seconds())
            return Response({
                'success': False,
                'message': f'Please wait {wait_sec} seconds before requesting a new code.',
                'errors': [{'field': 'rate_limit', 'message': f'Wait {wait_sec}s'}]
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        otp_token = EmailVerificationToken.generate_token(email=user.email, user=user)
        full_name = f"{user.first_name} {user.last_name}".strip() or user.username
        try:
            EmailService.send_verification_otp_email(
                email=user.email,
                name=full_name,
                otp_code=otp_token.code,
                expires_minutes=15
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to resend verification email to {user.email}: {e}")

        return Response({
            'success': True,
            'message': 'A new 6-digit verification code has been sent to your email address.',
            'data': {'email': user.email}
        })



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


from rest_framework import viewsets
from rest_framework.decorators import action
from .models import ApiKey
from .serializers import ApiKeySerializer

class ApiKeyViewSet(viewsets.ModelViewSet):
    """
    Manage API keys belonging to the signed-in user.

    The plaintext key is shown exactly once, in the create response; afterwards
    only a masked preview is available, so it cannot be re-read from the UI.
    """
    serializer_class = ApiKeySerializer
    permission_classes = (IsAuthenticated,)
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        return ApiKey.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        name = (request.data.get('name') or 'Default key')[:150]
        api_key = ApiKey.objects.create(user=request.user, name=name)
        data = ApiKeySerializer(api_key).data
        data['key'] = api_key.key  # shown once, never again
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='revoke')
    def revoke(self, request, pk=None):
        api_key = self.get_object()
        api_key.revoke()
        return Response(ApiKeySerializer(api_key).data)


# ---------------------------------------------------------------------------
# Two-factor authentication (TOTP) — plan §5 Week 6: mobile JWT + 2FA
# ---------------------------------------------------------------------------
from django.utils import timezone as django_timezone

from .models import TwoFactorSecret
from . import two_factor as tf


class TwoFactorStatusView(APIView):
    """GET /api/v1/auth/2fa/ — current 2FA status for the signed-in user."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        config = getattr(request.user, 'two_factor', None)
        return Response({
            'enabled': bool(config and config.is_enabled),
            'pending_setup': bool(config and not config.is_enabled),
        })


class TwoFactorSetupView(APIView):
    """
    POST /api/v1/auth/2fa/setup/ — start 2FA enrollment. Returns the base32
    secret and an otpauth:// provisioning URI. 2FA stays disabled until the
    user verifies a live code (TwoFactorVerifyView).
    """
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        secret = tf.generate_secret()
        config, _ = TwoFactorSecret.objects.update_or_create(
            user=request.user,
            defaults={'secret': secret, 'is_enabled': False, 'confirmed_at': None},
        )
        return Response({
            'secret': secret,
            'provisioning_uri': tf.provisioning_uri(
                secret, request.user.email, issuer='Nexucon'),
            'detail': 'Add the secret to your authenticator app, then verify a '
                      'code at /api/v1/auth/2fa/verify/ to enable 2FA.',
        }, status=status.HTTP_201_CREATED)


class TwoFactorVerifyView(APIView):
    """POST /api/v1/auth/2fa/verify/ {code} — confirm enrollment and enable 2FA."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        config = getattr(request.user, 'two_factor', None)
        if not config:
            return Response({'detail': 'No 2FA setup in progress — call /api/v1/auth/2fa/setup/ first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        code = str(request.data.get('code') or '')
        if not tf.verify_code(config.secret, code):
            return Response({'detail': 'Invalid or expired code.'},
                            status=status.HTTP_400_BAD_REQUEST)
        config.is_enabled = True
        config.confirmed_at = django_timezone.now()
        config.save(update_fields=['is_enabled', 'confirmed_at', 'updated_at'])
        return Response({'enabled': True})


class TwoFactorDisableView(APIView):
    """POST /api/v1/auth/2fa/disable/ {code} — disable 2FA (code required)."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        config = getattr(request.user, 'two_factor', None)
        if not config or not config.is_enabled:
            return Response({'detail': 'Two-factor authentication is not enabled.'},
                            status=status.HTTP_400_BAD_REQUEST)
        code = str(request.data.get('code') or '')
        if not tf.verify_code(config.secret, code,
                              last_used_counter=config.last_used_counter):
            return Response({'detail': 'Invalid or expired code.'},
                            status=status.HTTP_400_BAD_REQUEST)
        config.delete()
        return Response({'enabled': False})
