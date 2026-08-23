from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from django.utils import timezone
from .models import UserSession
import jwt

class CookieJWTAuthentication(JWTAuthentication):
    """
    Custom authentication class that reads the JWT from HttpOnly cookies
    and verifies that the associated UserSession is still active.
    """
    def authenticate(self, request):
        print("DEBUG AUTH - Cookies received:", request.COOKIES)
        # Read from cookie instead of Authorization header
        header = self.get_header(request)
        if header is None:
            raw_token = request.COOKIES.get(settings.SIMPLE_JWT.get('AUTH_COOKIE', 'access_token')) or None
            print("DEBUG AUTH - raw_token from cookie:", bool(raw_token))
        else:
            raw_token = self.get_raw_token(header)
            print("DEBUG AUTH - raw_token from header:", bool(raw_token))

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
