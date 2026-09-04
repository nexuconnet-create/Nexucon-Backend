from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.utils import timezone
from .models import UserSession, ApiKey
import jwt
import logging
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger(__name__)


class ApiKeyAuthentication:
    """
    Machine-to-machine authentication for scanners and integrations that
    cannot run the interactive JWT login flow. Reads the ApiKey sent in the
    `X-API-Key` header (or `Authorization: ApiKey <key>`) and authenticates as
    the user the key is bound to. Keys must be active and bound to a user —
    an unbound key is refused rather than granted anonymous access.
    """

    keyword = 'ApiKey'

    def authenticate(self, request):
        raw_key = request.META.get('HTTP_X_API_KEY')
        if not raw_key:
            header = request.META.get('HTTP_AUTHORIZATION', '')
            parts = header.split()
            if len(parts) == 2 and parts[0].lower() == self.keyword.lower():
                raw_key = parts[1]
        if not raw_key:
            return None

        api_key = ApiKey.objects.filter(key=raw_key).select_related('user').first()
        if api_key is None or not api_key.is_active or api_key.revoked_at is not None:
            raise AuthenticationFailed('Invalid or revoked API key.')
        if api_key.user is None or not api_key.user.is_active:
            raise AuthenticationFailed('API key is not bound to an active user.')

        # Throttle last_used_at updates to one per minute.
        now = timezone.now()
        if not api_key.last_used_at or (now - api_key.last_used_at).total_seconds() > 60:
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)

        return (api_key.user, api_key)

    def authenticate_header(self, request):
        return self.keyword


class CookieJWTAuthentication(JWTAuthentication):
    """
    Custom authentication class that reads the JWT from the Authorization
    header or, failing that, from the HttpOnly cookie, and verifies that the
    associated UserSession is still active.
    """
    def authenticate(self, request):
        # Authorization header first; fall back to the auth cookie.
        header = self.get_header(request)
        if header is not None:
            raw_token = self.get_raw_token(header)
        else:
            raw_token = request.COOKIES.get(settings.SIMPLE_JWT.get('AUTH_COOKIE', 'access_token')) or None

        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
            user = self.get_user(validated_token)
        except Exception:
            return None
        
        # Verify if session is still active
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT.get('AUTH_COOKIE_REFRESH', 'refresh_token'))
        if refresh_token:
            try:
                decoded = jwt.decode(refresh_token, options={"verify_signature": False})
                jti = decoded.get('jti')
                
                # Update last_activity and verify active status
                session = UserSession.objects.filter(refresh_jti=jti, user=user).first()
                if session:
                    if not session.is_active:
                        return None
                    
                    # Throttle database updates for last_activity to every 5 minutes to avoid overhead
                    now = timezone.now()
                    if (now - session.last_activity).total_seconds() > 300:
                        session.last_activity = now
                        session.save(update_fields=['last_activity'])
            except Exception as e:
                # If decoding fails or there's a problem, we might fall back to standard checks, 
                # but we shouldn't necessarily block unless we are strict about sessions.
                pass

        return user, validated_token
