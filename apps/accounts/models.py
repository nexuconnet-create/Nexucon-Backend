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
