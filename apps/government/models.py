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


class Inspector(models.Model):
    """
    Inspector accreditation (Inspector PWA Module 1: the inspector's identity
    card — badge number, directorate, accreditation standing).

    This is deliberately NOT part of ``Profile``. A Profile is an *account*:
    it says which agency a login belongs to and what it may do. An
    accreditation is a *credential*: it is issued to a named person by a named
    authority, it has a number that appears on a physical badge, and it
    lapses. The two have different lifecycles and different issuers, and
    merging them would mean an account edit could silently alter an
    accredited identity.

    ``full_name`` is a deliberate **snapshot**, not a proxy for
    ``user.get_full_name()``. A badge is issued to a name. If the holder later
    edits their profile, the accredited identity on record must not change
    underneath the inspections they signed — otherwise a signature made as
    "M. Ross" would appear to have been made by whoever the account is called
    today.

    **No rows are seeded for existing users.** That is the entire point of
    this model: the field it replaces (``InspectorDashboardView``'s badge
    number) was *fabricated* from a UUID slice, so every inspector appeared to
    hold a credential that had never been issued. Seeding badge numbers here
    would reproduce exactly that defect in a more respectable-looking place.
    Until an accreditation is entered, the API returns 404 with an honest
    reason and the UI must render an absent badge.
    """

    STATUS_ACTIVE = 'ACTIVE'
    STATUS_SUSPENDED = 'SUSPENDED'
    STATUS_REVOKED = 'REVOKED'
    STATUS_EXPIRED = 'EXPIRED'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_SUSPENDED, 'Suspended'),
        (STATUS_REVOKED, 'Revoked'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='inspector_accreditation',
    )

    badge_number = models.CharField(
        max_length=50, unique=True,
        help_text='The number printed on the inspector\'s physical badge',
    )
    full_name = models.CharField(
        max_length=255,
        help_text=('The name the badge was issued to. A snapshot — it does not '
                   'follow later edits to the user account.'),
    )
    # Free text, not a FK to District. The spec's "directorate" is the
    # organisational unit that issued the accreditation; the platform's
    # `District` is a geographic jurisdiction used for data scoping. They are
    # different concepts and forcing one to be the other would invent an
    # equivalence that is not true of the real organisation.
    directorate = models.CharField(max_length=255, blank=True, default='')

    accreditation_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True,
    )
    accreditation_expiry = models.DateField(
        null=True, blank=True,
        help_text='Null means no expiry is recorded — not that it never expires',
    )

    issued_by = models.CharField(max_length=255, blank=True, default='')
    issued_at = models.DateTimeField(null=True, blank=True)

    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True, default='')

    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['badge_number']
        verbose_name = 'Inspector accreditation'
        verbose_name_plural = 'Inspector accreditations'

    def __str__(self):
        return f"{self.badge_number} — {self.full_name}"

    @property
    def effective_status(self):
        """The status as it stands *now*, with expiry applied on read.

        Derived at read time and never written back. A nightly job that flipped
        the stored value would create a second source of truth: the column
        would disagree with the date for up to a day, and any query that ran
        in between would report a lapsed badge as active. The stored
        ``accreditation_status`` records what an administrator decided
        (ACTIVE / SUSPENDED / REVOKED); the expiry is a fact about time.
        """
        from django.utils import timezone

        if self.accreditation_status != self.STATUS_ACTIVE:
            return self.accreditation_status
        if (self.accreditation_expiry
                and self.accreditation_expiry < timezone.localdate()):
            return self.STATUS_EXPIRED
        return self.STATUS_ACTIVE

    @property
    def is_valid(self):
        """May this person sign field work today?"""
        return self.effective_status == self.STATUS_ACTIVE
