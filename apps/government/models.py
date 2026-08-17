from django.db import models
from django.conf import settings
import uuid

class Agency(models.Model):
    """
    Government Agencies (e.g., LASBCA, FMW, CAC).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True, null=True)
    
    # Onboarding Fields
    country = models.CharField(max_length=100, blank=True, null=True)
    state_region = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    department_name = models.CharField(max_length=255, blank=True, null=True)
    primary_role = models.CharField(max_length=100, blank=True, null=True)
    jurisdiction_level = models.CharField(max_length=100, blank=True, null=True)
    project_scale_focus = models.CharField(max_length=100, blank=True, null=True)
    collaboration_preference = models.CharField(max_length=100, blank=True, null=True)
    
    # Profile Settings Fields
    short_name = models.CharField(max_length=100, blank=True, null=True)
    official_email = models.EmailField(blank=True, null=True)
    main_phone = models.CharField(max_length=50, blank=True, null=True)
    physical_address = models.TextField(blank=True, null=True)
    timezone = models.CharField(max_length=100, blank=True, null=True)
    measurement_system = models.CharField(max_length=50, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Agency'
        verbose_name_plural = 'Agencies'

    def __str__(self):
        return f"{self.name} ({self.code})"


class Role(models.Model):
    """
    Custom RBAC Roles for Government Users (e.g., Inspector, Director).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    permissions = models.JSONField(default=list, help_text="List of permission strings (e.g., 'permits.approve')")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Profile(models.Model):
    """
    Government User Profile linking a User to an Agency and Role.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='government_profile')
    agency = models.ForeignKey(Agency, on_delete=models.SET_NULL, null=True, related_name='staff')
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True)
    approval_limit = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, help_text="Delegation of authority limit in NGN")
    is_active_staff = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} - {self.agency.code if self.agency else 'No Agency'}"
