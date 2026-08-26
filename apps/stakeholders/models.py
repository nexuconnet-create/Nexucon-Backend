from django.db import models
from django.conf import settings
import uuid

def generate_dev_id():
    return f"DEV-{uuid.uuid4().hex[:3].upper()}"

def generate_con_id():
    return f"CON-{uuid.uuid4().hex[:3].upper()}"

def generate_cns_id():
    return f"CNS-{uuid.uuid4().hex[:3].upper()}"

def generate_ins_id():
    return f"INS-{uuid.uuid4().hex[:3].upper()}"

def generate_lic_id():
    return f"LIC-{uuid.uuid4().hex[:4].upper()}"

def generate_mtg_id():
    return f"MTG-{uuid.uuid4().hex[:4].upper()}"

def generate_room_id():
    return f"room-{uuid.uuid4().hex[:8]}"


class BaseStakeholder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    registration_number = models.CharField(max_length=100, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_blacklisted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Developer(models.Model):
    """Master property developer and parent sponsor organization."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    developer_id = models.CharField(max_length=100, default=generate_dev_id, db_index=True)
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=50, default='Verified')
    active_projects_count = models.IntegerField(default=0)
    portfolio_value = models.CharField(max_length=100, default='$1.2B')
    hq_location = models.CharField(max_length=255, default='New York, NY')
    primary_contact_name = models.CharField(max_length=255, default='Michael Thorne')
    primary_contact_email = models.EmailField(blank=True, null=True)
    primary_contact_phone = models.CharField(max_length=50, blank=True, null=True)
    color_theme = models.CharField(max_length=50, default='bg-blue-600')
    is_active = models.BooleanField(default=True)
    is_blacklisted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.developer_id})"


class Contractor(BaseStakeholder):
    """General contractors and specialized subcontractors."""
    contractor_id = models.CharField(max_length=100, default=generate_con_id, db_index=True)
    name = models.CharField(max_length=255, default='Apex Construction Services')
    contractor_type = models.CharField(max_length=100, default='General Contractor')
    status = models.CharField(max_length=50, default='Prequalified')
    license_status = models.CharField(max_length=50, default='Valid')
    license_number = models.CharField(max_length=100, blank=True, null=True)
    compliance_score = models.IntegerField(default=90)
    active_permits = models.IntegerField(default=0)
    specialties = models.JSONField(default=list)
    color_theme = models.CharField(max_length=50, default='bg-blue-600')

    def __str__(self):
        return f"{self.name} [{self.contractor_type}] ({self.contractor_id})"


class Consultant(BaseStakeholder):
    """Specialized third-party advisory firms."""
    consultant_id = models.CharField(max_length=100, default=generate_cns_id, db_index=True)
    name = models.CharField(max_length=255, default='EcoBalance Partners')
    specialty = models.CharField(max_length=100, default='Environmental')
    status = models.CharField(max_length=50, default='Verified')
    active_roles_count = models.IntegerField(default=0)
    hq_location = models.CharField(max_length=255, default='Seattle, WA')
    description = models.TextField(blank=True, null=True)
    color_theme = models.CharField(max_length=100, default='bg-emerald-600 text-white')

    def __str__(self):
        return f"{self.name} ({self.specialty})"


class Inspector(BaseStakeholder):
    """Government and approved third-party field inspection officers."""
    inspector_id = models.CharField(max_length=100, default=generate_ins_id, db_index=True)
    name = models.CharField(max_length=255, default='Marcus Chen')
    role_title = models.CharField(max_length=100, default='Structural Inspector')
    inspector_type = models.CharField(max_length=100, default='Internal (Gov)')
    assigned_zone = models.CharField(max_length=100, default='Zone A (Downtown)')
    active_inspections = models.IntegerField(default=0)
    pass_rate = models.CharField(max_length=50, default='88%')
    ncrs_issued = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.name} - {self.assigned_zone} ({self.inspector_id})"


class LicensedProfessional(models.Model):
    """Architects, structural engineers, and MEP professionals with regulatory license verification."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license_id = models.CharField(max_length=100, default=generate_lic_id, db_index=True)
    name = models.CharField(max_length=255)
    role_title = models.CharField(max_length=100)
    firm_name = models.CharField(max_length=255)
    license_authority = models.CharField(max_length=100, default='COREN')
    license_status = models.CharField(max_length=50, default='Valid')
    expiry_date = models.CharField(max_length=100, default='Dec 31, 2027')
    active_projects_count = models.IntegerField(default=0)
    is_verified = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.license_id}) - {self.firm_name}"


class ProjectStakeholderTeam(models.Model):
    """Cross-functional project stakeholder matrix."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_reference = models.CharField(max_length=100, default='PRJ-992', db_index=True)
    project_name = models.CharField(max_length=255, default='Nexus Tower (Phase 1)')
    location = models.CharField(max_length=255, default='Downtown Core')
    status = models.CharField(max_length=100, default='Active Construction')
    team_data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Team Matrix: {self.project_name} ({self.project_reference})"


class BlacklistRecord(models.Model):
    """Recurring offenders, safety violations, and license suspensions."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity_type = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=100)
    entity_name = models.CharField(max_length=255)
    reason = models.TextField()
    incident_count = models.IntegerField(default=1)
    status = models.CharField(max_length=50, default='Blacklisted')
    blacklisted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.entity_name} [{self.status}]: {self.reason}"


class StakeholderMeeting(models.Model):
    """
    Official Stakeholder Meetings and Call Rooms.
    NOTE: Creation and scheduling can ONLY be initiated by the Agency Head / Director General.
    """
    MEETING_TYPE_CHOICES = (
        ('Video Call', 'Live Video Conference'),
        ('Audio Call', 'Audio Conference Call'),
        ('In-Person Council', 'In-Person Council Session'),
    )

    STATUS_CHOICES = (
        ('Scheduled', 'Scheduled'),
        ('In Progress', 'In Progress / Call Live'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting_reference = models.CharField(max_length=100, default=generate_mtg_id, db_index=True)
    title = models.CharField(max_length=255)
    agenda = models.TextField()
    project_name = models.CharField(max_length=255, default='Central Metro Transit Hub')
    date = models.CharField(max_length=100, default='Oct 24, 2026')
    time_slot = models.CharField(max_length=100, default='10:00 AM - 11:30 AM')
    meeting_type = models.CharField(max_length=50, choices=MEETING_TYPE_CHOICES, default='Video Call')
    
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    initiator_name = models.CharField(max_length=255, default='Engr. Babatunde Sanwo')
    initiator_role = models.CharField(max_length=100, default='Agency Head / Director General')
    
    room_id = models.CharField(max_length=100, default=generate_room_id)
    google_meet_url = models.CharField(max_length=500, blank=True, null=True, default='')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Scheduled')
    participants = models.JSONField(default=list)
    minutes_notes = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.meeting_reference} [{self.meeting_type}]: {self.title}"


class StakeholderMessage(models.Model):
    """Real-time stakeholder messaging channels and direct communications."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    sender_name = models.CharField(max_length=255, default='Agency Officer')
    sender_role = models.CharField(max_length=100, default='Government Safety Directorate')
    
    channel_name = models.CharField(max_length=100, default='General Council', db_index=True)
    project_name = models.CharField(max_length=255, default='Central Metro Transit Hub')
    message_text = models.TextField(blank=True, default='')
    
    # File Attachments
    attachment_url = models.TextField(blank=True, null=True)
    attachment_name = models.CharField(max_length=255, blank=True, null=True)
    attachment_type = models.CharField(max_length=100, blank=True, null=True)
    attachment_size = models.CharField(max_length=50, blank=True, null=True)
    
    # Voice Notes
    voice_note_url = models.TextField(blank=True, null=True)
    voice_note_duration = models.IntegerField(default=0, help_text="Duration in seconds")
    
    is_urgent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        desc = self.message_text[:40] if self.message_text else ('[Voice Note]' if self.voice_note_url else '[Attachment]')
        return f"[{self.channel_name}] {self.sender_name}: {desc}"


class Certification(models.Model):
    """Professional certifications like COREN, CCPC."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='certifications', null=True, blank=True)
    authority = models.CharField(max_length=100, help_text="e.g., COREN, ARCON, CCPC")
    license_number = models.CharField(max_length=100, unique=True)
    issue_date = models.DateField()
    expiry_date = models.DateField()
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.authority} - {self.license_number}"


class TrainingRecord(models.Model):
    """Inspector training and certification tracking."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspector = models.ForeignKey(Inspector, on_delete=models.CASCADE, related_name='trainings')
    course_name = models.CharField(max_length=200)
    completion_date = models.DateField()
    certificate_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course_name}"


class MessageTranslation(models.Model):
    """
    Multilingual machine translation caching layer for stakeholder communications.
    Supports English (en), Yorùbá (yo), Igbo (ig), and Hausa (ha).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(StakeholderMessage, on_delete=models.CASCADE, related_name='translations')
    target_language = models.CharField(max_length=10, db_index=True)
    translated_content = models.TextField()
    provider = models.CharField(max_length=100, default='Google Cloud Translation')
    translation_version = models.CharField(max_length=50, default='v3.0')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('message', 'target_language')
        ordering = ['-created_at']

    def __str__(self):
        return f"Translation [{self.target_language}]: {self.message_id}"


class MeetingActionItem(models.Model):
    """Actionable items and deliverables assigned during official council sessions."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(StakeholderMeeting, on_delete=models.CASCADE, related_name='action_items')
    title = models.CharField(max_length=255)
    assignee_name = models.CharField(max_length=255, default='Project Lead')
    due_date = models.CharField(max_length=100, default='Within 5 Business Days')
    is_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.title} ({'Done' if self.is_completed else 'Pending'})"
