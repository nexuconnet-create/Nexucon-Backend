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

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = self.generate_key()
        super().save(*args, **kwargs)
