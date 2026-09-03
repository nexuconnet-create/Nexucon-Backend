from django.db import models
from django.contrib.auth.models import AbstractUser
import uuid

class User(AbstractUser):
    """
    Custom User model for Nexucon platform.
    Using email as the primary authentication field.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    is_verified = models.BooleanField(default=False, help_text="Designates whether the user's email has been verified.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    class Meta:
        db_table = 'accounts_user'
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        
    def __str__(self):
        return f"{self.email} ({self.get_full_name()})"


class UserSession(models.Model):
    """
    Tracks active user sessions and devices for security monitoring.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    device_info = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    last_activity = models.DateTimeField(auto_now=True)
    login_time = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    
    # Store the refresh token family or jti if needed to revoke specific token chains
    refresh_jti = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    class Meta:
        db_table = 'accounts_user_session'
        verbose_name = 'User Session'
        verbose_name_plural = 'User Sessions'
        ordering = ['-last_activity']

    def __str__(self):
        return f"{self.user.email} - {self.device_info} ({'Active' if self.is_active else 'Revoked'})"

import secrets
from django.utils import timezone
from django.conf import settings

class ApiKey(models.Model):
    """
    Machine-to-machine API key, used by scanners and integrations that cannot
    run the interactive JWT login flow.
    """
    PREFIX = 'nex_live_'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_keys',
        null=True, blank=True,
    )
    name = models.CharField(max_length=150, blank=True, default='Default key')
    key = models.CharField(max_length=100, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'accounts_apikey'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.masked_key})"

    @classmethod
    def generate_key(cls) -> str:
        return f"{cls.PREFIX}{secrets.token_urlsafe(18)}"

    @property
    def masked_key(self) -> str:
        if not self.key:
            return ''
        return f"{self.PREFIX}{'•' * 8}{self.key[-4:]}"

    def revoke(self) -> None:
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=['is_active', 'revoked_at'])


class TwoFactorSecret(models.Model):
    """
    TOTP two-factor authentication secret for a user (plan §5 Week 6 mobile
    2FA). `is_enabled` only becomes True after the user proves possession of
    the secret by verifying a live code.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='two_factor')
    secret = models.CharField(max_length=64, help_text="Base32 TOTP secret")
    is_enabled = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_counter = models.BigIntegerField(
        null=True, blank=True,
        help_text="TOTP step counter of the last accepted code (replay protection)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounts_two_factor_secret'

    def __str__(self):
        return f"2FA for {self.user.email} ({'enabled' if self.is_enabled else 'pending'})"

    def save(self, *args, **kwargs):
        # Auto-generate a base32 TOTP secret when none was supplied (the
        # enrollment endpoint always provides one, but keep the model safe
        # for direct ORM use). NOTE: this previously (incorrectly) assigned
        # `self.key` via `self.generate_key()` — attributes that only exist
        # on ApiKey — which made every save raise AttributeError.
        if not self.secret:
            from .two_factor import generate_secret
            self.secret = generate_secret()
        super().save(*args, **kwargs)
