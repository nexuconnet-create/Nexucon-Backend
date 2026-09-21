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
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from apps.notifications.email_service import EmailService

User = get_user_model()


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
        # Check if an invited inspector is providing their invite code or temporary passcode
        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password', '')
        if email and password:
            from apps.settings.models import UserInvitation
            inv = UserInvitation.objects.filter(email__iexact=email).first()
            if inv and inv.status == 'Pending':
                norm_pw = password.strip().replace('-', '').upper()
                norm_code = (inv.invite_code or '').strip().replace('-', '').upper()
                is_invite_code = bool(norm_code and (norm_pw == norm_code or password.strip().upper() == norm_code))
                is_temp_pass = bool(inv.temporary_password and password.strip() == inv.temporary_password.strip())
                if is_invite_code or is_temp_pass:
                    return Response({
                        'success': False,
                        'requires_activation': True,
                        'message': 'Account activation required: You must establish your permanent password using your official invite credentials.',
                        'email': email,
                        'invite_code': inv.invite_code,
                        'data': {
                            'requires_activation': True,
                            'name': inv.name,
                            'role': inv.role,
                            'email': email,
                            'invite_code': inv.invite_code
                        }
                    }, status=status.HTTP_200_OK)

        response = super().post(request, *args, **kwargs)
        if response.status_code == 401 and email:
            from apps.settings.models import UserInvitation
            inv = UserInvitation.objects.filter(email__iexact=email).first()
            portal = request.headers.get('X-Portal-Type') or request.data.get('portal')
            is_inspector = bool(inv and 'inspector' in (inv.role or '').lower())
            if portal == 'inspector' or is_inspector:
                if not inv:
                    return Response({
                        'detail': 'Access Denied: This email has not been registered as an accredited inspector by the Agency Directorate. Access is strictly invite-based.',
                        'is_registered': False,
                        'code': 'NOT_REGISTERED'
                    }, status=status.HTTP_401_UNAUTHORIZED)
                elif inv.status == 'Pending':
                    return Response({
                        'detail': 'Access Restricted: Your inspector account is pending activation. Please input your official Invite Code or Temporary Password to activate your account.',
                        'is_registered': True,
                        'is_pending': True,
                        'code': 'PENDING_ACTIVATION'
                    }, status=status.HTTP_401_UNAUTHORIZED)
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
        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password')

        # Check if user already exists
        if email:
            existing_user = User.objects.filter(email__iexact=email).first()
            if existing_user:
                if existing_user.is_verified:
                    return Response({
                        'success': False,
                        'message': 'An account with this email address already exists. Please log in instead.',
                        'data': None,
                        'errors': {
                            'email': ['An account with this email address already exists. Please log in instead.']
                        }
                    }, status=status.HTTP_400_BAD_REQUEST)
                else:
                    # User exists but is NOT verified yet. Update credentials & issue fresh OTP
                    if password:
                        if len(password) < 8:
                            return Response({
                                'success': False,
                                'message': 'Password must be at least 8 characters long.',
                                'data': None,
                                'errors': {'password': ['Password must be at least 8 characters long.']}
                            }, status=status.HTTP_400_BAD_REQUEST)
                        existing_user.set_password(password)

                    full_name = (request.data.get('name') or '').strip()
                    if full_name and not (request.data.get('first_name') or request.data.get('last_name')):
                        parts = full_name.split(None, 1)
                        existing_user.first_name = parts[0]
                        existing_user.last_name = parts[1] if len(parts) > 1 else ''
                    else:
                        if request.data.get('first_name'):
                            existing_user.first_name = request.data.get('first_name')
                        if request.data.get('last_name'):
                            existing_user.last_name = request.data.get('last_name')
                    if request.data.get('phone_number'):
                        existing_user.phone_number = request.data.get('phone_number')
                    existing_user.save()
                    user = existing_user

                    # Check and link stakeholder if requested
                    st_type = (request.data.get('stakeholder_type') or '').lower().strip()
                    if st_type:
                        from apps.stakeholders.models import (
                            Developer, Contractor, Consultant, LicensedProfessional, generate_lic_id
                        )
                        c_name = (request.data.get('company_name') or '').strip()
                        u_full = f"{user.first_name} {user.last_name}".strip() or user.email
                        if st_type in ['client', 'developer'] and not Developer.objects.filter(user=user).exists():
                            Developer.objects.create(
                                user=user,
                                name=c_name or u_full,
                                status='Active',
                                primary_contact_name=u_full,
                                primary_contact_email=user.email,
                                primary_contact_phone=user.phone_number or '',
                                is_active=True,
                            )
                        elif st_type == 'contractor' and not Contractor.objects.filter(user=user).exists():
                            Contractor.objects.create(
                                user=user,
                                name=c_name or u_full,
                                company_name=c_name,
                                registration_number=request.data.get('registration_number', ''),
                                license_number=request.data.get('license_number', ''),
                                contractor_type='General Contractor',
                                status='Prequalified',
                                is_active=True,
                            )
                        elif st_type == 'professional' and not LicensedProfessional.objects.filter(user=user).exists():
                            LicensedProfessional.objects.create(
                                user=user,
                                license_id=request.data.get('license_number') or generate_lic_id(),
                                name=u_full,
                                role_title='Licensed Professional',
                                firm_name=c_name or 'Independent Practice',
                                license_authority=request.data.get('license_authority') or 'COREN',
                                license_status='Active',
                                is_verified=True,
                            )
                        elif st_type == 'consultant' and not Consultant.objects.filter(user=user).exists():
                            Consultant.objects.create(
                                user=user,
                                name=c_name or u_full,
                                company_name=c_name,
                                registration_number=request.data.get('registration_number', ''),
                                specialty='Advisory Consultant',
                                status='Active',
                                is_active=True,
                            )

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
                        'message': 'We have sent a 6-digit verification code to your email address.',
                        'data': {
                            'user': UserMeSerializer(user).data,
                            'email': user.email,
                            'requires_verification': True,
                            'is_verified': False,
                        },
                        'errors': None
                    }, status=status.HTTP_200_OK)

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


from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.template.loader import render_to_string
import urllib.parse
import logging

logger = logging.getLogger(__name__)


class PasswordResetRequestView(APIView):
    """
    POST /api/v1/auth/password-reset/
    Generates a secure recovery token and dispatches an email via Resend.
    """
    permission_classes = (AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        if not email:
            return Response({
                'success': False,
                'message': 'Official email address is required.'
            }, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email).first()
        if user and user.is_active:
            token = default_token_generator.make_token(user)
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            otp_token = EmailVerificationToken.generate_token(email, user=user, duration_minutes=60)

            origin = request.headers.get('origin') or request.headers.get('referer') or ''
            if 'stakeholder' in origin:
                base_url = 'https://stakeholder.nexucon.net'
            elif 'inspector' in origin:
                base_url = 'https://inspector.nexucon.net'
            else:
                base_url = getattr(settings, 'FRONTEND_URL', 'https://nexucon.net').rstrip('/')

            reset_url = f"{base_url}/stakeholder/reset-password?email={urllib.parse.quote(user.email)}&token={token}&uid={uidb64}"

            context = {
                'name': user.get_full_name() or user.first_name or 'Stakeholder Official',
                'email': user.email,
                'reset_url': reset_url,
                'otp_code': otp_token.code,
            }
            try:
                html_body = render_to_string('emails/password_reset.html', context)
                dispatch_res = EmailService.send_email(
                    to_email=user.email,
                    subject='Reset Your Nexucon Password',
                    html_content=html_body,
                )
                logger.info(f"Password reset email dispatched to {user.email}: {dispatch_res}")
            except Exception as e:
                logger.error(f"Failed to dispatch password reset email to {user.email}: {e}")

        # Always return generic success to prevent email enumeration
        return Response({
            'success': True,
            'message': 'If your email is registered in the system, password reset instructions have been dispatched.'
        }, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """
    POST /api/v1/auth/password-reset-confirm/
    Validates the token or 6-digit code and establishes a new password.
    """
    permission_classes = (AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        token = (request.data.get('token') or '').strip()
        code = (request.data.get('code') or '').strip()
        uidb64 = (request.data.get('uid') or '').strip()
        new_password = request.data.get('new_password', '')
        confirm_password = request.data.get('confirm_password', '')

        if not new_password:
            return Response({'success': False, 'message': 'New password is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response({'success': False, 'message': 'Password must be at least 8 characters long.'}, status=status.HTTP_400_BAD_REQUEST)
        if confirm_password and new_password != confirm_password:
            return Response({'success': False, 'message': 'Passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)

        user = None
        if uidb64:
            try:
                uid = force_str(urlsafe_base64_decode(uidb64))
                user = User.objects.filter(pk=uid).first()
            except Exception:
                user = None

        if not user and email:
            user = User.objects.filter(email__iexact=email).first()

        if not user or not user.is_active:
            return Response({'success': False, 'message': 'Invalid recovery request or user not found.'}, status=status.HTTP_400_BAD_REQUEST)

        is_token_valid = False
        if token and default_token_generator.check_token(user, token):
            is_token_valid = True
        elif code:
            ev_token = EmailVerificationToken.objects.filter(
                email__iexact=user.email,
                code=code,
                is_used=False,
                expires_at__gte=timezone.now()
            ).first()
            if ev_token:
                is_token_valid = True
                ev_token.is_used = True
                ev_token.save(update_fields=['is_used'])

        if not is_token_valid:
            return Response({
                'success': False,
                'message': 'Invalid or expired password reset token / code. Please request a new recovery link.'
            }, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()

        # Invalidate remaining unused verification tokens for this user
        EmailVerificationToken.objects.filter(email__iexact=user.email, is_used=False).update(is_used=True)

        return Response({
            'success': True,
            'message': 'Password has been successfully updated. You may now sign in with your new credentials.'
        }, status=status.HTTP_200_OK)


class UserOnboardingView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        user = request.user
        data = request.data
        
        # Mark as verified and onboarded
        user.is_verified = True
        user.is_onboarded = True
        user.save(update_fields=['is_verified', 'is_onboarded'])

        # Determine onboarding stream: Stakeholder vs Government
        portal = (request.headers.get('X-Portal-Type') or data.get('portal') or data.get('portal_type') or '').lower()
        stakeholder_type = (data.get('stakeholder_type') or data.get('stakeholderRole') or '').lower()

        from apps.stakeholders.models import (
            Developer, Contractor, Consultant, LicensedProfessional, generate_lic_id
        )

        has_stakeholder_record = (
            Developer.objects.filter(user=user).exists() or
            Contractor.objects.filter(user=user).exists() or
            Consultant.objects.filter(user=user).exists() or
            LicensedProfessional.objects.filter(user=user).exists()
        )

        is_stakeholder = bool(
            portal == 'stakeholder' or
            stakeholder_type or
            has_stakeholder_record or
            data.get('companyName') or
            data.get('company_name') or
            data.get('registration_number')
        )

        if is_stakeholder:
            company_name = (data.get('company_name') or data.get('companyName') or data.get('firmName') or '').strip()
            registration_number = (data.get('registration_number') or data.get('registrationNumber') or '').strip()
            license_authority = (data.get('license_authority') or data.get('licenseAuthority') or 'COREN').strip()
            license_number = (data.get('license_number') or data.get('licenseNumber') or '').strip()
            country = (data.get('country') or '').strip()
            state_region = (data.get('state_region') or data.get('stateRegion') or '').strip()
            city = (data.get('city') or '').strip()
            office_address = (data.get('office_address') or data.get('officeAddress') or '').strip()
            project_scale_focus = (data.get('project_scale_focus') or data.get('projectScaleFocus') or '').strip()
            project_ref = (data.get('project_reference_code') or data.get('projectReferenceCode') or data.get('project_reference') or '').strip()
            contact_name = (data.get('contact_name') or data.get('fullName') or user.get_full_name()).strip() or user.email
            phone = (data.get('phone') or data.get('phone_number') or user.phone_number or '').strip()

            hq_loc = office_address or f"{city}, {state_region}, {country}".strip(', ') or 'Nigeria'

            # If stakeholder_type wasn't explicitly passed, detect from existing records
            if not stakeholder_type:
                if Developer.objects.filter(user=user).exists():
                    stakeholder_type = 'developer'
                elif Contractor.objects.filter(user=user).exists():
                    stakeholder_type = 'contractor'
                elif Consultant.objects.filter(user=user).exists():
                    stakeholder_type = 'consultant'
                elif LicensedProfessional.objects.filter(user=user).exists():
                    stakeholder_type = 'professional'
                else:
                    stakeholder_type = 'client'

            if stakeholder_type in ['client', 'developer']:
                dev = Developer.objects.filter(user=user).first()
                if not dev:
                    dev = Developer.objects.create(
                        user=user,
                        name=company_name or contact_name,
                        status='Active',
                        hq_location=hq_loc,
                        primary_contact_name=contact_name,
                        primary_contact_email=user.email,
                        primary_contact_phone=phone,
                        is_active=True,
                    )
                else:
                    if company_name:
                        dev.name = company_name
                    dev.hq_location = hq_loc
                    dev.primary_contact_name = contact_name
                    dev.primary_contact_phone = phone
                    dev.status = 'Active'
                    dev.is_active = True
                    dev.save()

            elif stakeholder_type == 'contractor':
                con = Contractor.objects.filter(user=user).first()
                if not con:
                    con = Contractor.objects.create(
                        user=user,
                        name=company_name or contact_name,
                        company_name=company_name,
                        registration_number=registration_number,
                        license_number=license_number,
                        contractor_type='General Contractor',
                        status='Active',
                        license_status='Active' if license_number else 'Pending',
                        specialties=[project_scale_focus] if project_scale_focus else [],
                        is_active=True,
                    )
                else:
                    if company_name:
                        con.name = company_name
                        con.company_name = company_name
                    if registration_number:
                        con.registration_number = registration_number
                    if license_number:
                        con.license_number = license_number
                        con.license_status = 'Active'
                    if project_scale_focus:
                        con.specialties = [project_scale_focus]
                    con.status = 'Active'
                    con.is_active = True
                    con.save()

            elif stakeholder_type == 'professional':
                lic = LicensedProfessional.objects.filter(user=user).first()
                if not lic:
                    lic = LicensedProfessional.objects.create(
                        user=user,
                        license_id=license_number or generate_lic_id(),
                        name=contact_name,
                        role_title='Licensed Professional',
                        firm_name=company_name or 'Independent Practice',
                        license_authority=license_authority,
                        license_status='Active',
                        is_verified=True,
                    )
                else:
                    if contact_name:
                        lic.name = contact_name
                    if company_name:
                        lic.firm_name = company_name
                    if license_authority:
                        lic.license_authority = license_authority
                    if license_number:
                        lic.license_id = license_number
                    lic.license_status = 'Active'
                    lic.is_verified = True
                    lic.save()

            elif stakeholder_type == 'consultant':
                cns = Consultant.objects.filter(user=user).first()
                if not cns:
                    cns = Consultant.objects.create(
                        user=user,
                        name=company_name or contact_name,
                        company_name=company_name,
                        registration_number=registration_number,
                        specialty=f"Consultant - {project_scale_focus.capitalize()}" if project_scale_focus else 'Advisory Consultant',
                        status='Active',
                        hq_location=hq_loc,
                        is_active=True,
                    )
                else:
                    if company_name:
                        cns.name = company_name
                        cns.company_name = company_name
                    if registration_number:
                        cns.registration_number = registration_number
                    if project_scale_focus:
                        cns.specialty = f"Consultant - {project_scale_focus.capitalize()}"
                    cns.hq_location = hq_loc
                    cns.status = 'Active'
                    cns.is_active = True
                    cns.save()

            # Refresh user instance from DB to serialize latest profile/role state
            user.refresh_from_db()
            serializer = UserMeSerializer(user)
            return Response({
                'success': True,
                'message': 'Stakeholder onboarding completed successfully',
                'data': serializer.data,
                'errors': None
            })

        # Default Government Agency Head Onboarding
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
