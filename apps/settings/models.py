from django.db import models
from django.conf import settings
import uuid

def generate_dev_id():
    return f"T-S1-{uuid.uuid4().hex[:4].upper()}"

def generate_log_id():
    return f"log-{uuid.uuid4().hex[:5]}"

def generate_tpl_id():
    return f"TPL-{uuid.uuid4().hex[:3].upper()}"

def generate_wf_id():
    return f"WF-{uuid.uuid4().hex[:2].upper()}"


# ==========================================
# 1. INTEGRATION & TELEMETRY MODELS
# ==========================================

class TersusDevice(models.Model):
    """Tersus GNSS RTK base stations, rovers, and sensor telemetry."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_id = models.CharField(max_length=100, default=generate_dev_id, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    device_type = models.CharField(max_length=100, default='RTK Receiver')
    status = models.CharField(max_length=50, default='Active')
    battery_level = models.CharField(max_length=50, default='98%')
    ip_address = models.CharField(max_length=100, blank=True, null=True)
    latitude = models.FloatField(default=6.5244)
    longitude = models.FloatField(default=3.3792)
    firmware_version = models.CharField(max_length=50, default='v2.4.1')
    last_sync = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.device_id}) - {self.status}"


class BIMIntegration(models.Model):
    """External BIM and design platforms (Autodesk, Procore, Trimble, Bentley)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=50, default='Connected')
    client_id = models.CharField(max_length=255, blank=True, null=True)
    synced_models_count = models.IntegerField(default=0)
    webhook_url = models.CharField(max_length=500, blank=True, null=True)
    icon_code = models.CharField(max_length=10, default='A')
    last_sync = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['provider']

    def __str__(self):
        return f"{self.provider} [{self.status}]"


class DocumentSystemIntegration(models.Model):
    """Document storage and DMS connectors (Cloudflare R2, SharePoint, Google Drive)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    system_type = models.CharField(max_length=100, default='Cloud Storage')
    status = models.CharField(max_length=50, default='Active')
    bucket_or_drive_name = models.CharField(max_length=255, default='nexucondocument')
    endpoint_url = models.CharField(
        max_length=500,
        default='https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument'
    )
    synced_files_count = models.IntegerField(default=0)
    last_sync = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.system_type}) - {self.status}"


class GovernmentAPIIntegration(models.Model):
    """Inter-agency bridges (CAC, LASRRA, e-GIS, FMW, LIRS, NIBSS)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    api_key_identifier = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    endpoint_url = models.CharField(max_length=500)
    status = models.CharField(max_length=50, default='connected')
    data_flow_direction = models.CharField(max_length=50, default='Bidirectional')
    last_sync = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.status}]"


class APIKeyCredential(models.Model):
    """External applications connecting via OAuth/API tokens."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    key_prefix = models.CharField(max_length=50, default='nx_live_')
    hashed_key = models.CharField(max_length=255)
    app_type = models.CharField(max_length=100, default='OAuth 2.0 App')
    volume_tier = models.CharField(max_length=100, default='High (450k/day)')
    status = models.CharField(max_length=50, default='Healthy')
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.key_prefix}...) - {self.status}"


class IntegrationLog(models.Model):
    """Sanitized, append-only integration activity log."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    log_reference = models.CharField(max_length=100, default=generate_log_id, db_index=True)
    service_name = models.CharField(max_length=255)
    event_name = models.CharField(max_length=255)
    status = models.CharField(max_length=50, default='Success')
    payload_size = models.CharField(max_length=50, default='2.4 MB')
    http_status_code = models.IntegerField(default=200)
    details = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.status}] {self.service_name} - {self.event_name} ({self.log_reference})"


# ==========================================
# 2. USER INVITATIONS & STAFF MANAGEMENT
# ==========================================

class UserInvitation(models.Model):
    """Internal staff invitations sent by Agency Administrators."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    name = models.CharField(max_length=255)
    department = models.CharField(max_length=255, default='Urban Planning')
    role = models.CharField(max_length=100, default='Reviewer')
    token = models.CharField(max_length=100, unique=True, default=uuid.uuid4)
    status = models.CharField(
        max_length=50,
        choices=[('Pending', 'Pending'), ('Accepted', 'Accepted'), ('Revoked', 'Revoked'), ('Expired', 'Expired')],
        default='Pending'
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_staff_invitations'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Invite for {self.email} ({self.role}) - {self.status}"


# ==========================================
# 3. RBAC & PERMISSION MATRIX
# ==========================================

class CustomRole(models.Model):
    """Configurable government roles (System Administrator, City Planner, Lead Inspector, Reviewer, etc.)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, null=True)
    role_type = models.CharField(max_length=50, default='Custom Role') # System Default, Custom Role
    is_system_default = models.BooleanField(default=False)
    active_users_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.role_type})"


class RolePermission(models.Model):
    """Permission entry mapping a role to a specific capability within a module."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(CustomRole, on_delete=models.CASCADE, related_name='permissions')
    module = models.CharField(max_length=100) # Permits & Approvals, Site Inspections, System & Audit
    permission_name = models.CharField(max_length=255)
    is_granted = models.BooleanField(default=False)

    class Meta:
        unique_together = ('role', 'module', 'permission_name')
        ordering = ['module', 'permission_name']

    def __str__(self):
        return f"{self.role.name} - {self.module}: {self.permission_name} = {self.is_granted}"


# ==========================================
# 4. APPROVAL WORKFLOWS & ROUTING CHAINS
# ==========================================

class ApprovalWorkflow(models.Model):
    """Approval pipelines for building permits and architectural clearances."""
    id = models.CharField(primary_key=True, max_length=50, default=generate_wf_id)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=50,
        choices=[('Active', 'Active'), ('System Enforced', 'System Enforced'), ('Draft', 'Draft'), ('Archived', 'Archived')],
        default='Active'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} [{self.status}]"


class WorkflowStep(models.Model):
    """Step node in an approval chain."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(ApprovalWorkflow, on_delete=models.CASCADE, related_name='steps')
    step_order = models.IntegerField(default=1)
    title = models.CharField(max_length=255)
    role = models.CharField(max_length=255)
    icon_name = models.CharField(max_length=100, default='ShieldCheck')
    is_system_enforced = models.BooleanField(default=False)

    class Meta:
        ordering = ['workflow', 'step_order']
        unique_together = ('workflow', 'step_order')

    def __str__(self):
        return f"{self.workflow.id} Step {self.step_order}: {self.title} ({self.role})"


# ==========================================
# 5. INSPECTION TEMPLATES & CHECKLIST BUILDER
# ==========================================

class InspectionTemplate(models.Model):
    """Standard inspection checklist templates."""
    id = models.CharField(primary_key=True, max_length=50, default=generate_tpl_id)
    name = models.CharField(max_length=255)
    department = models.CharField(max_length=100, default='Structural')
    status = models.CharField(
        max_length=50,
        choices=[('Active', 'Active'), ('Draft', 'Draft'), ('Archived', 'Archived')],
        default='Active'
    )
    version = models.CharField(max_length=50, default='v1.0')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.id}) - {self.department}"


class ChecklistItem(models.Model):
    """Checklist item in an inspection template."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(InspectionTemplate, on_delete=models.CASCADE, related_name='items')
    item_order = models.IntegerField(default=1)
    title = models.CharField(max_length=500)
    field_type = models.CharField(
        max_length=100,
        choices=[
            ('Number Input', 'Number Input'),
            ('Pass/Fail Toggle', 'Pass/Fail Toggle'),
            ('Photo Upload', 'Photo Upload'),
            ('Text Input', 'Text Input')
        ],
        default='Pass/Fail Toggle'
    )
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ['template', 'item_order']

    def __str__(self):
        return f"{self.template.id} Item {self.item_order}: {self.title}"


# ==========================================
# 6. COMPLIANCE STANDARDS & STATUTORY ACTS
# ==========================================

class ComplianceStandard(models.Model):
    """Configurable system thresholds for environmental limits, material tolerances, and SLAs."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=100) # Environmental Limits, Structural Tolerances, SLA Thresholds
    key = models.CharField(max_length=100, unique=True)
    label = models.CharField(max_length=255)
    num_value = models.FloatField(default=0.0)
    unit = models.CharField(max_length=50, blank=True, null=True)
    alert_level = models.CharField(max_length=50, default='Warning') # Info, Warning, Critical
    description = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'key']

    def __str__(self):
        return f"{self.label}: {self.num_value} {self.unit or ''}"


class StatutoryDocument(models.Model):
    """Statutory Instrument Reference acts and applicable building laws."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    connected_features = models.JSONField(default=list) # e.g. ["Zoning Controls", "Setbacks"]
    document_url = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f"{self.code} - {self.name}"


# ==========================================
# 7. NOTIFICATION PREFERENCES & ROUTING
# ==========================================

class NotificationRoutingRule(models.Model):
    """Escalation routing rules for critical events and SLA breaches."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trigger_event = models.CharField(max_length=255)
    primary_recipient = models.CharField(max_length=255)
    sla_timeline = models.CharField(max_length=100, default='Within 15 mins')
    escalation_target = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.trigger_event} -> {self.primary_recipient} -> {self.escalation_target}"


class NotificationPreferenceCategory(models.Model):
    """Multi-channel delivery rules (In-App, Email, SMS Text)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=100) # Critical Safety Incidents, Permits & Approvals, Field Inspections
    event_label = models.CharField(max_length=255)
    in_app = models.BooleanField(default=True)
    email = models.BooleanField(default=True)
    sms = models.BooleanField(default=False)
    is_locked = models.BooleanField(default=False) # If locked, critical safety alerts cannot be disabled

    class Meta:
        unique_together = ('category', 'event_label')
        ordering = ['category', 'event_label']

    def __str__(self):
        return f"[{self.category}] {self.event_label} (InApp:{self.in_app}, Email:{self.email}, SMS:{self.sms})"


# ==========================================
# 8. WEBHOOK SUBSCRIPTIONS
# ==========================================

class WebhookSubscription(models.Model):
    """Outgoing webhook subscriptions."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    target_url = models.CharField(max_length=500)
    events = models.JSONField(default=list) # e.g. ["permit.created", "permit.updated", "inspection.failed"]
    status = models.CharField(
        max_length=50,
        choices=[('Active', 'Active'), ('Paused', 'Paused'), ('Failing', 'Failing')],
        default='Active'
    )
    secret_token = models.CharField(max_length=255, default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.target_url}) - {self.status}"
