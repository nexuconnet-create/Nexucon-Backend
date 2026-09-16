import secrets
import logging
from datetime import timedelta
from django.utils import timezone
from django.core.cache import cache
from apps.notifications.email_service import EmailService

logger = logging.getLogger(__name__)

CODE_EXPIRATION_MINUTES = 15


def generate_verification_code() -> str:
    """Generate a secure 6-digit numeric verification code (100000 to 999999)."""
    return f"{secrets.randbelow(900000) + 100000}"


def send_verification_code_for_user(user=None, email: str = None, name: str = None) -> str:
    """
    Generate a 6-digit verification code, persist it in the database and cache,
    and dispatch an email to the user.
    """
    from .models import EmailVerificationCode, User as UserModel

    target_email = (email or (user.email if user else '')).strip().lower()
    if not target_email:
        raise ValueError("Email is required to generate a verification code.")

    if not user and target_email:
        user = UserModel.objects.filter(email__iexact=target_email).first()

    code = generate_verification_code()
    expires_at = timezone.now() + timedelta(minutes=CODE_EXPIRATION_MINUTES)

    # Invalidate previous unused codes for this email
    try:
        EmailVerificationCode.objects.filter(email__iexact=target_email, is_used=False).update(is_used=True)
        EmailVerificationCode.objects.create(
            user=user,
            email=target_email,
            code=code,
            expires_at=expires_at,
            is_used=False
        )
    except Exception as e:
        logger.warning(f"Could not persist verification code to DB: {e}")

    # Also store in cache for fast lookup / resilience
    try:
        cache.set(f"verify_email_{target_email}", code, timeout=CODE_EXPIRATION_MINUTES * 60)
    except Exception as e:
        logger.warning(f"Could not cache verification code: {e}")

    recipient_name = name or (user.get_full_name() if user else '') or 'User'
    try:
        EmailService.send_verification_otp_email(
            email=target_email,
            name=recipient_name,
            otp_code=code,
            expires_minutes=CODE_EXPIRATION_MINUTES
        )
    except Exception as e:
        logger.error(f"Failed to dispatch verification email to {target_email}: {e}")

    return code


def verify_code_for_email(email: str, code: str) -> bool:
    """
    Verify whether the provided 6-digit code matches the active code for the email.
    Marks the code as used if valid.
    """
    from .models import EmailVerificationCode

    target_email = email.strip().lower()
    code_str = str(code).strip()
    if not target_email or not code_str:
        return False

    # Check database first
    now = timezone.now()
    try:
        record = EmailVerificationCode.objects.filter(
            email__iexact=target_email,
            code=code_str,
            is_used=False,
            expires_at__gte=now
        ).order_by('-created_at').first()

        if record:
            record.is_used = True
            record.save(update_fields=['is_used'])
            try:
                cache.delete(f"verify_email_{target_email}")
            except Exception:
                pass
            return True
    except Exception as e:
        logger.warning(f"Database lookup failed for verification code: {e}")

    # Fallback to cache if DB was unreachable or before migration
    try:
        cached_code = cache.get(f"verify_email_{target_email}")
        if cached_code and str(cached_code).strip() == code_str:
            cache.delete(f"verify_email_{target_email}")
            return True
    except Exception as e:
        logger.warning(f"Cache lookup failed for verification code: {e}")

    return False
