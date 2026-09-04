from django.db import models
from django.conf import settings
import uuid

class District(models.Model):
    """
    Zonal / District Jurisdiction in the State hierarchy
    (State Ministry/Agency -> HQ Directorate -> District -> District Office -> Project).

    Districts scope what a user can see (multi-tenant isolation) and are the
    aggregation unit for the HQ Command risk matrix and heatmap. Boundary is
    stored as a GeoJSON-style polygon in JSON — real boundaries are supplied
    by the Survey/GIS client input (plan §7), never invented.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=30, unique=True)
    state_region = models.CharField(max_length=100, blank=True, default='')
    description = models.TextField(blank=True, default='')
    # GeoJSON Polygon (lon/lat rings) of the district boundary, if provided.
    boundary_polygon = models.JSONField(null=True, blank=True)
    office_address = models.TextField(blank=True, default='')
    lead_officer_name = models.CharField(max_length=255, blank=True, default='')
    lead_officer_email = models.EmailField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"


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
    district = models.ForeignKey(
        District, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='staff', help_text="Zonal/District jurisdiction the user is scoped to",
    )
    # HQ-level staff (state-wide scope) have is_state_hq=True and no district.
    is_state_hq = models.BooleanField(
        default=False, help_text="State Headquarters Directorate — state-wide visibility",
    )
    approval_limit = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, help_text="Delegation of authority limit in NGN")
    is_active_staff = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} - {self.agency.code if self.agency else 'No Agency'}"
