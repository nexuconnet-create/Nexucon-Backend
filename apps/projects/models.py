from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
import uuid
import datetime
import logging

logger = logging.getLogger(__name__)

def generate_project_ref():
    return f"NXC-GOV-{datetime.datetime.now().year}-{uuid.uuid4().hex[:4].upper()}"


def resolve_inspector_user(wanted):
    """Resolve a free-text inspector name to an active user, or to nothing.

    Returns ``(user, candidate_count)``. A user comes back only when the string
    matches **exactly one** active user — by full name, falling back to email —
    because a name two people share cannot say which of them was meant.

    ``candidate_count`` is returned rather than folded into ``None`` so the
    caller can say *why* nothing resolved: nobody matched, or several did, and
    those are different problems for whoever has to fix the data.

    This is the single definition of the rule. `Project.sync_assigned_inspector`
    (runtime writes) and `backfill_project_assigned_inspector_user` (the one-off
    migration) both call it, so the backfill can never resolve something the
    live path would refuse, or vice versa.
    """
    wanted = (wanted or '').strip()
    if not wanted:
        return None, 0

    from django.contrib.auth import get_user_model
    User = get_user_model()
    candidates = [
        user for user in User.objects.filter(is_active=True).only(
            'id', 'email', 'first_name', 'last_name')
        if wanted in (f'{user.first_name} {user.last_name}'.strip(), user.email)
    ]
    if len(candidates) == 1:
        return candidates[0], 1
    return None, len(candidates)

class Project(models.Model):
    """
    Core project model for construction sites.
    """
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('PLANNING', 'Planning / Proposed'),
        ('APPROVED', 'Approved'),
        ('ACTIVE', 'Under Construction'),
        ('SUSPENDED', 'Suspended / Stop-Work'),
        ('COMPLETED', 'Completed'),
        ('ABANDONED', 'Abandoned'),
    )

    PROJECT_TYPE_CHOICES = (
        ('Residential', 'Residential'),
        ('Commercial', 'Commercial'),
        ('Industrial', 'Industrial'),
        ('Infrastructure', 'Infrastructure'),
        ('Mixed-Use', 'Mixed-Use'),
        ('Institutional', 'Institutional'),
        ('Renovation', 'Renovation / Redevelopment'),
        ('Other', 'Other'),
    )

    PRIORITY_CHOICES = (
        ('Low', 'Low'),
        ('Normal', 'Normal'),
        ('High', 'High'),
        ('Critical', 'Critical'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Zonal / District jurisdiction assignment (HQ -> District -> Project drill-down).
    district = models.ForeignKey(
        'government.District', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='projects', help_text="District office with jurisdiction over this project",
    )

    # 1. Project Information
    name = models.CharField(max_length=255)
    reference_number = models.CharField(max_length=50, unique=True, default=generate_project_ref)
    project_type = models.CharField(max_length=50, choices=PROJECT_TYPE_CHOICES, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    development_category = models.CharField(max_length=100, blank=True, null=True)
    estimated_project_value = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    number_of_floors = models.IntegerField(blank=True, null=True)
    start_date = models.DateField(null=True, blank=True)
    estimated_completion = models.DateField(null=True, blank=True)

    # 2. Developer / Project Owner
    developer_name = models.CharField(max_length=255, blank=True, null=True)
    developer_organization = models.CharField(max_length=255, blank=True, null=True)
    developer_reg_number = models.CharField(max_length=100, blank=True, null=True)
    developer_email = models.EmailField(blank=True, null=True)
    developer_phone = models.CharField(max_length=50, blank=True, null=True)
    developer_address = models.TextField(blank=True, null=True)
    developer_contact_person = models.CharField(max_length=255, blank=True, null=True)

    # 3. Project Location
    site_address = models.TextField(blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    lga = models.CharField(max_length=100, help_text="Local Government Area", blank=True, null=True)
    ward_area = models.CharField(max_length=100, blank=True, null=True)
    plot_number = models.CharField(max_length=100, blank=True, null=True)
    block_number = models.CharField(max_length=100, blank=True, null=True)
    land_title_reference = models.CharField(max_length=255, blank=True, null=True)

    # 5. Regulatory Information
    permit_number = models.CharField(max_length=100, blank=True, null=True)
    permit_status = models.CharField(max_length=100, blank=True, null=True)
    planning_approval_reference = models.CharField(max_length=100, blank=True, null=True)
    building_control_reference = models.CharField(max_length=100, blank=True, null=True)
    environmental_approval_reference = models.CharField(max_length=100, blank=True, null=True)
    existing_applications = models.TextField(blank=True, null=True)
    applicable_regulations = models.TextField(blank=True, null=True)
    regulatory_authority = models.CharField(max_length=255, blank=True, null=True)
    approval_date = models.DateField(null=True, blank=True)
    permit_expiry_date = models.DateField(null=True, blank=True)

    # 6. Project Scope & Development Details
    primary_use = models.CharField(max_length=255, blank=True, null=True)
    proposed_use = models.CharField(max_length=255, blank=True, null=True)
    site_area = models.DecimalField(max_digits=10, decimal_places=2, help_text="in sqm", blank=True, null=True)
    gross_floor_area = models.DecimalField(max_digits=10, decimal_places=2, help_text="in sqm", blank=True, null=True)
    building_height = models.DecimalField(max_digits=10, decimal_places=2, help_text="in meters", blank=True, null=True)
    number_of_units = models.IntegerField(blank=True, null=True)
    construction_method = models.CharField(max_length=255, blank=True, null=True)
    structural_system = models.CharField(max_length=255, blank=True, null=True)
    special_requirements = models.TextField(blank=True, null=True)

    # 8. Government Assignment
    assigned_department = models.CharField(max_length=255, blank=True, null=True)
    assigned_officer = models.CharField(max_length=255, blank=True, null=True)
    #: Display mirror only — `assigned_inspector_user` decides access.
    #:
    #: Kept as a CharField and never renamed, re-typed or dropped: it is written
    #: by existing endpoints and read by the frontend, and dropping it would be a
    #: breaking API change across live rows. It is now written *from* the
    #: foreign key (see `sync_assigned_inspector`) so the two cannot drift.
    assigned_inspector = models.CharField(max_length=255, blank=True, null=True)
    #: The inspector this project is assigned to, as a real relation.
    #:
    #: Added to close a privilege escalation: `common.permissions
    #: .scoped_projects` gave an Inspector every project whose free-text
    #: `assigned_inspector` equalled their own name, so access followed a string
    #: rather than a person — two users sharing a name saw each other's
    #: projects, and editing your profile name changed what you could reach.
    #: `SET_NULL`, not `CASCADE`: an inspector leaving must not delete the
    #: projects they were assigned.
    assigned_inspector_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_projects',
        help_text=(
            'The inspector assigned to this project. This is what grants '
            'access; `assigned_inspector` mirrors the name for display only.'
        ),
    )
    technical_reviewer = models.CharField(max_length=255, blank=True, null=True)
    compliance_officer = models.CharField(max_length=255, blank=True, null=True)
    project_priority = models.CharField(max_length=50, choices=PRIORITY_CHOICES, default='Normal')
    monitoring_category = models.CharField(max_length=255, blank=True, null=True)
    inspection_frequency = models.CharField(max_length=100, blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    
    # Client / cover-page information (from Tarsus)
    client_name = models.CharField(max_length=200, blank=True, default='', help_text="Name of the client / commissioning body")
    project_number = models.CharField(max_length=100, blank=True, default='', help_text="Internal or contract project reference number")
    client_contact = models.CharField(max_length=200, blank=True, default='', help_text="Primary client contact name or email")
    latitude = models.FloatField(null=True, blank=True, help_text="Project site latitude")
    longitude = models.FloatField(null=True, blank=True, help_text="Project site longitude")
    geofence_radius_m = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1)],
        help_text=(
            "Site check-in radius in metres. Null means this project has not "
            "recorded one and the platform default applies "
            "(DEFAULT_GEOFENCE_RADIUS_M). Deliberately not defaulted to 50: "
            "that is a platform policy, not an attribute of this project, and "
            "a column default would make every existing row claim a geofence "
            "nobody entered."
        ),
    )

    # 9. Monitoring Configuration
    enable_site_monitoring = models.BooleanField(default=False)
    enable_gnss = models.BooleanField(default=False)
    enable_bim = models.BooleanField(default=False)
    inspection_required = models.BooleanField(default=True)
    compliance_monitoring_required = models.BooleanField(default=True)
    progress_reporting_required = models.BooleanField(default=False)
    site_verification_required = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- Cold storage (8 Sep 2026 meeting: projects inactive for 3-6
    # months move to cold storage). A cold project keeps EVERY record —
    # statutory data is never deleted or hidden from a direct lookup; it
    # is only excluded from the default hot browsing lists until restored.
    cold_storage = models.BooleanField(
        default=False, db_index=True,
        help_text='Project moved to cold storage after a prolonged period '
                  'without activity (all records remain intact and directly '
                  'accessible)')
    cold_stored_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the project was moved to cold storage')

    def last_activity_at(self):
        """The most recent real activity on this project: its own update
        time or the newest record captured against it (tests, surveys,
        findings, analyses). Used by the cold-storage policy — never
        guessed."""
        from django.utils import timezone
        candidates = [self.updated_at, self.created_at]
        # Each tuple: (related_name, timestamp_field).
        for rel, ts in (
            ('pundit_tests', 'updated_at'),
            ('gpr_surveys', 'updated_at'),
            ('gnss_surveys', 'updated_at'),
            ('digital_eye_findings', 'created_at'),
            ('digital_eye_ai_analyses', 'analyzed_at'),
            ('milestones', 'created_at'),
            ('project_documents', 'uploaded_at'),
        ):
            try:
                latest = getattr(self, rel).order_by(f'-{ts}').values_list(
                    ts, flat=True).first()
            except AttributeError:
                # Related model lacks that accessor — skip, never crash.
                continue
            if latest is not None:
                candidates.append(latest)
        real = [c for c in candidates if c is not None]
        return max(real) if real else timezone.now()

    def sync_assigned_inspector(self):
        """Keep the display name and the assignment in step, one way only.

        Two directions, and they are deliberately not symmetric:

        **FK set** — the text is overwritten with that user's name. The foreign
        key is the truth, so a mirror that disagrees with it is a bug, not an
        alternative to respect.

        **FK empty but text present** — the text is resolved through
        `resolve_inspector_user`, which refuses when the name is shared or
        matches nobody. Both refusals are the point: guessing is the escalation
        this field was added to close.

        Returns the resolved user, or ``None``.
        """
        if self.assigned_inspector_user_id:
            user = self.assigned_inspector_user
            name = user.get_full_name() or user.email
            if self.assigned_inspector != name:
                self.assigned_inspector = name
            return user

        wanted = self.assigned_inspector
        if not (wanted or '').strip():
            return None

        user, candidates = resolve_inspector_user(wanted)
        if user is not None:
            self.assigned_inspector_user = user
            return user

        if candidates > 1:
            found = f'matches {candidates} active users'
            hazard = ('a name two people share cannot say which of them was '
                      'meant')
        else:
            found = 'matches no active user'
            hazard = 'the text names nobody the platform knows'
        logger.warning(
            'Project %s: assigned_inspector %r %s; no assignment made. Access '
            'follows the user, so %s — assign the project deliberately.',
            self.reference_number or self.pk, wanted.strip(), found, hazard,
        )
        return None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A brand-new instance starts with no assignment loaded, so whatever the
        # caller passed counts as a change and is resolved on the first save.
        self._loaded_assignment = (None, None)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # Read straight out of __dict__ rather than through the descriptors: a
        # field the query deferred would otherwise be fetched from the database
        # here, once per row, just to take this snapshot.
        instance._loaded_assignment = (
            instance.__dict__.get('assigned_inspector'),
            instance.__dict__.get('assigned_inspector_user_id'),
        )
        return instance

    def save(self, *args, **kwargs):
        """Resolve the assignment, but only when a caller actually touched it.

        The guard is load-bearing. Resolution is not free: the FK branch may
        fetch a user and the text branch scans the user table, and doing that on
        every `Project.save()` — including the many that only change a status —
        would put a query on a path that has nothing to do with assignment.
        """
        update_fields = kwargs.get('update_fields')
        loaded = getattr(self, '_loaded_assignment', (None, None))
        current = (self.assigned_inspector, self.assigned_inspector_user_id)
        named = set(update_fields or ())
        touched = current != loaded or bool(
            named & {'assigned_inspector', 'assigned_inspector_user'})

        if touched:
            self.sync_assigned_inspector()
            if update_fields is not None:
                # Resolution can write either column. A caller that named its
                # fields must not have that write silently dropped, so the pair
                # is added rather than trusted to be listed.
                kwargs['update_fields'] = list(
                    named | {'assigned_inspector', 'assigned_inspector_user'})

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.reference_number})"


class ProjectProfessional(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='professionals')
    name = models.CharField(max_length=255)
    organization = models.CharField(max_length=255, blank=True, null=True)
    license_number = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    role = models.CharField(max_length=100) # e.g. Architect, Civil Engineer

    def __str__(self):
        return f"{self.name} - {self.role} ({self.project.name})"


class ProjectDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='project_documents')
    document_type = models.CharField(max_length=100) # e.g. Architectural Drawing, Title Deed
    file = models.FileField(upload_to='project_documents/', blank=True, null=True)
    name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class ProjectMilestone(models.Model):
    """
    Construction Milestones linked to the project schedule.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    title = models.CharField(max_length=200)
    target_date = models.DateField()
    is_completed = models.BooleanField(default=False)
    completion_date = models.DateField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.project.name} - {self.title}"


class BIMModel(models.Model):
    """Stores uploaded BIM reference files linked to projects, with version tracking."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='legacy_bim_models', null=True, blank=True)
    name = models.CharField(max_length=255)
    version = models.CharField(max_length=50, default='v1.0')
    file = models.FileField(upload_to='bim_models/', null=True, blank=True)
    file_url = models.URLField(max_length=500, blank=True)
    file_format = models.CharField(max_length=50, choices=[('ifc', 'IFC'), ('rvt', 'Revit'), ('nwd', 'Navisworks'), ('other', 'Other')])
    uploaded_by = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} v{self.version} ({self.project.name})"