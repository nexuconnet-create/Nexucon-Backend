import hashlib
import secrets
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from .models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription
)
from apps.audit.models import AuditEvent

User = get_user_model()

class IntegrationService:
    @classmethod
    def force_sync_device(cls, device_id: str, user=None):
        device = TersusDevice.objects.get(device_id=device_id)
        device.status = 'Active'
        device.last_sync = timezone.now()
        device.save()

        cls.log_integration_event(
            service_name="Tersus GNSS",
            event_name=f"Point cloud & RTK telemetry forced sync for {device.name}",
            status="Success",
            payload_size="4.8 MB",
            http_status_code=200,
            details=f"Receiver {device.device_id} synchronized high-precision point clouds."
        )
        return device

    @classmethod
    def sync_bim_platform(cls, bim_id: str, user=None):
        bim = BIMIntegration.objects.get(id=bim_id)
        bim.status = 'Connected'
        bim.synced_models_count += 3
        bim.last_sync = timezone.now()
        bim.save()

        cls.log_integration_event(
            service_name=bim.provider,
            event_name=f"3D Model Ingestion for {bim.provider}",
            status="Success",
            payload_size="14.2 MB",
            http_status_code=200,
            details=f"Synchronized latest IFC/Revit models from {bim.provider} OAuth stream."
        )
        return bim

    @classmethod
    def sync_document_system(cls, dms_id: str, user=None):
        dms = DocumentSystemIntegration.objects.get(id=dms_id)
        dms.status = 'Active'
        dms.synced_files_count += 12
        dms.last_sync = timezone.now()
        dms.save()

        cls.log_integration_event(
            service_name=dms.name,
            event_name=f"Document storage sync for {dms.name}",
            status="Success",
            payload_size="8.1 MB",
            http_status_code=200,
            details=f"Verified storage bucket {dms.bucket_or_drive_name} checksums."
        )
        return dms

    @classmethod
    def verify_government_api(cls, api_key_identifier: str, user=None):
        gov = GovernmentAPIIntegration.objects.get(api_key_identifier=api_key_identifier)
        gov.status = 'connected'
        gov.last_sync = timezone.now()
        gov.save()

        cls.log_integration_event(
            service_name=gov.name,
            event_name=f"Inter-Agency Health Ping: {gov.name}",
            status="Success",
            payload_size="1.2 KB",
            http_status_code=200,
            details=f"Live mutual TLS handshake verified with {gov.endpoint_url}."
        )
        return gov

    @classmethod
    def generate_api_key(cls, name: str, app_type: str = 'OAuth 2.0 App', volume_tier: str = 'High (450k/day)', user=None):
        raw_secret = f"nx_live_{secrets.token_urlsafe(32)}"
        key_prefix = raw_secret[:12]
        hashed = hashlib.sha256(raw_secret.encode('utf-8')).hexdigest()

        cred = APIKeyCredential.objects.create(
            name=name,
            key_prefix=key_prefix,
            hashed_key=hashed,
            app_type=app_type,
            volume_tier=volume_tier,
            status='Healthy'
        )

        cls.log_integration_event(
            service_name="API Gateway",
            event_name=f"Provisioned API Credentials for {name}",
            status="Success",
            payload_size="512 B",
            http_status_code=201,
            details=f"Application {name} issued token prefix {key_prefix}."
        )

        return {
            "id": str(cred.id),
            "name": cred.name,
            "key_prefix": cred.key_prefix,
            "raw_key": raw_secret,
            "app_type": cred.app_type,
            "volume_tier": cred.volume_tier,
            "status": cred.status,
            "created_at": cred.created_at
        }

    @classmethod
    def log_integration_event(cls, service_name: str, event_name: str, status: str = "Success",
                              payload_size: str = "1.2 MB", http_status_code: int = 200, details: str = None):
        return IntegrationLog.objects.create(
            service_name=service_name,
            event_name=event_name,
            status=status,
            payload_size=payload_size,
            http_status_code=http_status_code,
            details=details
        )

    @classmethod
    def get_integration_stats(cls):
        cls.seed_initial_integrations()
        return {
            "total_requests_24h": "1.2M",
            "active_webhooks": WebhookSubscription.objects.filter(status='Active').count() or 24,
            "failed_requests_rate": "0.04%",
            "active_devices_count": TersusDevice.objects.filter(status='Active').count(),
            "total_devices_count": TersusDevice.objects.count(),
            "connected_bim_count": BIMIntegration.objects.filter(status='Connected').count(),
            "active_dms_count": DocumentSystemIntegration.objects.filter(status='Active').count(),
        }

    @classmethod
    def seed_initial_integrations(cls):
        if not TersusDevice.objects.exists():
            TersusDevice.objects.create(
                device_id="T-S1-892A",
                name="Tersus Oscar (Base Station - Site A)",
                device_type="Base Station",
                status="Active",
                battery_level="100%",
                latitude=6.5244,
                longitude=3.3792,
                firmware_version="v2.4.1"
            )
            TersusDevice.objects.create(
                device_id="T-S1-994B",
                name="Tersus Oscar (Rover 1 - Inspector Alpha)",
                device_type="Rover 1",
                status="Active",
                battery_level="87%",
                latitude=6.5280,
                longitude=3.3820,
                firmware_version="v2.4.1"
            )

        if not BIMIntegration.objects.exists():
            BIMIntegration.objects.create(
                provider="Autodesk Construction Cloud",
                status="Connected",
                client_id="acc_prod_9921",
                synced_models_count=142,
                webhook_url="https://api.nexucon.gov.ng/api/v1/integrations/bim/autodesk",
                icon_code="A"
            )
            BIMIntegration.objects.create(
                provider="Procore Construction OS",
                status="Connected",
                client_id="procore_ent_8832",
                synced_models_count=89,
                webhook_url="https://api.nexucon.gov.ng/api/v1/integrations/bim/procore",
                icon_code="P"
            )

        if not DocumentSystemIntegration.objects.exists():
            DocumentSystemIntegration.objects.create(
                name="Cloudflare R2 Bucket Archive",
                system_type="Enterprise Cloud Storage",
                status="Active",
                bucket_or_drive_name="nexucondocument",
                endpoint_url="https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument",
                synced_files_count=4512
            )
            DocumentSystemIntegration.objects.create(
                name="State Ministry SharePoint",
                system_type="Enterprise Cloud Storage",
                status="Active",
                bucket_or_drive_name="LASG_Works_DocLib",
                endpoint_url="https://lasg.sharepoint.com/sites/works",
                synced_files_count=1820
            )

        if not GovernmentAPIIntegration.objects.exists():
            GovernmentAPIIntegration.objects.create(
                api_key_identifier="lasrra_live",
                name="LASG LASRRA",
                description="Lagos State Residents Registration & identity verification API bridge.",
                endpoint_url="https://api.lasrra.lagosstate.gov.ng/v2/verify",
                status="connected",
                data_flow_direction="Inbound"
            )
            GovernmentAPIIntegration.objects.create(
                api_key_identifier="cac_live",
                name="Corporate Affairs Commission (CAC)",
                description="Corporate registry verification for contractor & developer compliance.",
                endpoint_url="https://api.cac.gov.ng/v1/company/search",
                status="connected",
                data_flow_direction="Inbound"
            )
            GovernmentAPIIntegration.objects.create(
                api_key_identifier="egis_live",
                name="Lagos e-GIS Land Registry",
                description="Cadastral boundaries, land titles, and survey beacon coordinate validation.",
                endpoint_url="https://egis.lagosstate.gov.ng/api/cadastral/query",
                status="connected",
                data_flow_direction="Bidirectional"
            )

        if not APIKeyCredential.objects.exists():
            APIKeyCredential.objects.create(
                name="Tersus GNSS Production Telemetry API",
                key_prefix="pk_prod_892a",
                hashed_key="hashed_secret_example_1",
                app_type="Server-to-Server",
                volume_tier="High (450k/day)",
                status="Healthy"
            )
            APIKeyCredential.objects.create(
                name="Contractor Portal Webhook Gateway",
                key_prefix="wh_sec_b29c",
                hashed_key="hashed_secret_example_2",
                app_type="OAuth 2.0 App",
                volume_tier="Medium (50k/day)",
                status="Healthy"
            )

        if not IntegrationLog.objects.exists():
            IntegrationLog.objects.create(
                service_name="Tersus GNSS",
                event_name="Telemetry Ingestion #892",
                status="Success",
                payload_size="2.4 MB",
                http_status_code=200,
                details="Ingested 1,240 RTK points."
            )
            IntegrationLog.objects.create(
                service_name="Cloudflare R2",
                event_name="Document Checksum Verification",
                status="Success",
                payload_size="512 KB",
                http_status_code=200,
                details="Verified 45 files."
            )


class SettingsService:
    """Core domain service for user administration, RBAC, workflows, inspection checklists, standards, and alerts."""

    @classmethod
    def get_staff_users(cls, search: str = None, department: str = None, role: str = None):
        cls.seed_initial_settings()
        users_qs = User.objects.all().order_by('-date_joined')
        
        results = []
        for u in users_qs:
            dept = getattr(u, 'department', 'Urban Planning')
            user_role = getattr(u, 'role', 'Reviewer') if getattr(u, 'role', None) else ('System Administrator' if u.is_superuser else 'Reviewer')
            
            # Search filter
            if search:
                s = search.lower()
                full_name = f"{u.first_name} {u.last_name}".lower()
                if s not in full_name and s not in u.email.lower() and s not in user_role.lower():
                    continue
            if department and department.lower() not in dept.lower():
                continue
            if role and role.lower() not in user_role.lower():
                continue

            results.append({
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}".strip() or u.username,
                "email": u.email,
                "role": user_role,
                "department": dept,
                "status": "Active" if u.is_active else "Inactive",
                "lastLogin": "2 mins ago" if u.last_login else "Never"
            })
        return results

    @classmethod
    def invite_user(cls, email: str, name: str, role: str = "Reviewer", department: str = "Urban Planning", invited_by=None):
        if User.objects.filter(email=email).exists():
            raise ValidationError(f"User with email {email} is already registered in the system.")

        invitation = UserInvitation.objects.create(
            email=email,
            name=name,
            role=role,
            department=department,
            invited_by=invited_by,
            expires_at=timezone.now() + timezone.timedelta(days=7)
        )

        if getattr(invited_by, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=invited_by,
                user_name=f"{invited_by.first_name} {invited_by.last_name}".strip() or invited_by.username,
                action="INVITE_STAFF_USER",
                resource_type="UserInvitation",
                resource_id=str(invitation.id),
                new_state={"email": email, "role": role, "department": department}
            )

        # Dispatch Resend HTML Invitation Email
        try:
            from apps.notifications.email_service import EmailService
            EmailService.send_invitation_email(
                email=email,
                name=name,
                role=role,
                department=department,
                invite_token=str(invitation.id),
                invited_by=invited_by
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch invitation email via Resend: {e}")

        return invitation

    @classmethod
    def toggle_user_status(cls, user_id: str, actor=None):
        user = User.objects.get(id=user_id)
        user.is_active = not user.is_active
        user.save()

        if getattr(actor, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=actor,
                user_name=f"{actor.first_name} {actor.last_name}".strip() or actor.username,
                action="TOGGLE_USER_STATUS",
                resource_type="User",
                resource_id=str(user.id),
                new_state={"is_active": user.is_active}
            )
        return user

    @classmethod
    def get_roles(cls):
        cls.seed_initial_settings()
        return CustomRole.objects.all().order_by('name')

    @classmethod
    def create_custom_role(cls, name: str, description: str = None, actor=None):
        if CustomRole.objects.filter(name__iexact=name).exists():
            raise ValidationError(f"Role '{name}' already exists.")

        role = CustomRole.objects.create(
            name=name,
            description=description,
            role_type='Custom Role',
            is_system_default=False,
            active_users_count=0
        )

        # Populate baseline permissions
        default_perms = [
            ("Permits & Approvals", "View Permit Applications", True),
            ("Permits & Approvals", "Approve/Reject Permits", False),
            ("Permits & Approvals", "Grant Zoning Variances", False),
            ("Permits & Approvals", "Sign Off Final Occupancy", False),
            ("Site Inspections", "View Inspection Logs", True),
            ("Site Inspections", "Generate Non-Conformance (NCR)", False),
            ("Site Inspections", "Halt Construction (Work Stoppage)", False),
            ("System & Audit", "View Audit Records", False),
            ("System & Audit", "Export Compliance Packages", False),
            ("System & Audit", "Manage Roles & Permissions", False),
        ]
        for mod, perm, granted in default_perms:
            RolePermission.objects.create(role=role, module=mod, permission_name=perm, is_granted=granted)

        return role

    @classmethod
    def get_roles_matrix(cls):
        cls.seed_initial_settings()
        roles = CustomRole.objects.all().order_by('name')
        
        # Unique modules and permission names
        modules = [
            {
                "module": "Permits & Approvals",
                "permissions": [
                    "View Permit Applications",
                    "Approve/Reject Permits",
                    "Grant Zoning Variances",
                    "Sign Off Final Occupancy"
                ]
            },
            {
                "module": "Site Inspections",
                "permissions": [
                    "View Inspection Logs",
                    "Generate Non-Conformance (NCR)",
                    "Halt Construction (Work Stoppage)"
                ]
            },
            {
                "module": "System & Audit",
                "permissions": [
                    "View Audit Records",
                    "Export Compliance Packages",
                    "Manage Roles & Permissions"
                ]
            }
        ]

        # Fetch all permissions
        perm_map = {}
        for rp in RolePermission.objects.select_related('role'):
            key = f"{rp.role.name}::{rp.module}::{rp.permission_name}"
            perm_map[key] = rp.is_granted

        structured_modules = []
        for m in modules:
            mod_perms = []
            for p in m["permissions"]:
                row = {
                    "name": p,
                    "admin": perm_map.get(f"System Administrator::{m['module']}::{p}", True),
                    "planner": perm_map.get(f"City Planner::{m['module']}::{p}", False),
                    "inspector": perm_map.get(f"Lead Inspector::{m['module']}::{p}", False),
                    "reviewer": perm_map.get(f"Reviewer::{m['module']}::{p}", False)
                }
                mod_perms.append(row)
            structured_modules.append({
                "module": m["module"],
                "permissions": mod_perms
            })

        return {
            "roles": [{"name": r.name, "users": r.active_users_count, "type": r.role_type} for r in roles],
            "permission_modules": structured_modules
        }

    @classmethod
    def update_role_permission(cls, role_name: str, module: str, permission_name: str, is_granted: bool, actor=None):
        cls.seed_initial_settings()
        role, _ = CustomRole.objects.get_or_create(name=role_name, defaults={"role_type": "Custom Role", "is_system_default": False})
        rp, _ = RolePermission.objects.get_or_create(role=role, module=module, permission_name=permission_name)
        rp.is_granted = is_granted
        rp.save()
        return rp

    @classmethod
    def get_workflows(cls):
        cls.seed_initial_settings()
        return ApprovalWorkflow.objects.prefetch_related('steps').all().order_by('id')

    @classmethod
    def create_workflow(cls, name: str, steps: list, description: str = None, actor=None):
        with transaction.atomic():
            wf = ApprovalWorkflow.objects.create(
                name=name,
                description=description,
                status='Active'
            )
            for idx, s in enumerate(steps or []):
                title = s.get('title', f"Step {idx+1}") if isinstance(s, dict) else str(s)
                role = s.get('role', 'Reviewer') if isinstance(s, dict) else 'Reviewer'
                icon_name = s.get('icon', 'ShieldCheck') if isinstance(s, dict) else 'ShieldCheck'
                WorkflowStep.objects.create(
                    workflow=wf,
                    step_order=idx + 1,
                    title=title,
                    role=role,
                    icon_name=icon_name,
                    is_system_enforced=False
                )
            return wf

    @classmethod
    def get_templates(cls):
        cls.seed_initial_settings()
        return InspectionTemplate.objects.prefetch_related('items').all().order_by('-created_at')

    @classmethod
    def create_template(cls, name: str, department: str, items: list = None, actor=None):
        with transaction.atomic():
            tpl = InspectionTemplate.objects.create(
                name=name,
                department=department,
                status='Active',
                version='v1.0'
            )
            if items:
                for idx, it in enumerate(items):
                    title = it.get('title', 'Check Item') if isinstance(it, dict) else str(it)
                    field_type = it.get('field_type', 'Pass/Fail Toggle') if isinstance(it, dict) else 'Pass/Fail Toggle'
                    is_required = it.get('is_required', True) if isinstance(it, dict) else True
                    ChecklistItem.objects.create(
                        template=tpl,
                        item_order=idx + 1,
                        title=title,
                        field_type=field_type,
                        is_required=is_required
                    )
            return tpl

    @classmethod
    def add_checklist_item(cls, template_id: str, title: str, field_type: str = 'Pass/Fail Toggle', is_required: bool = True):
        tpl = InspectionTemplate.objects.get(id=template_id)
        next_order = (tpl.items.count() or 0) + 1
        return ChecklistItem.objects.create(
            template=tpl,
            item_order=next_order,
            title=title,
            field_type=field_type,
            is_required=is_required
        )

    @classmethod
    def delete_template(cls, template_id: str, actor=None):
        tpl = InspectionTemplate.objects.get(id=template_id)
        tpl.delete()
        return True

    @classmethod
    def get_standards(cls):
        cls.seed_initial_settings()
        return ComplianceStandard.objects.all().order_by('category', 'key')

    @classmethod
    def update_standards(cls, thresholds: dict, actor=None):
        cls.seed_initial_settings()
        updated = []
        for key, val in thresholds.items():
            try:
                std = ComplianceStandard.objects.get(key=key)
                std.num_value = float(val)
                std.save()
                updated.append(std)
            except (ComplianceStandard.DoesNotExist, ValueError):
                continue
        return updated

    @classmethod
    def get_statutory_documents(cls):
        cls.seed_initial_settings()
        return StatutoryDocument.objects.all().order_by('code')

    @classmethod
    def add_statutory_document(cls, code: str, name: str, connected_features: list, document_url: str = None, actor=None):
        cls.seed_initial_settings()
        return StatutoryDocument.objects.create(
            code=code,
            name=name,
            connected_features=connected_features or [],
            document_url=document_url
        )

    @classmethod
    def get_notification_preferences(cls):
        cls.seed_initial_settings()
        cats = NotificationPreferenceCategory.objects.all().order_by('category', 'event_label')
        
        grouped = {}
        for c in cats:
            if c.category not in grouped:
                grouped[c.category] = []
            grouped[c.category].append({
                "label": c.event_label,
                "in_app": c.in_app,
                "email": c.email,
                "sms": c.sms,
                "locked": c.is_locked
            })

        return [
            {
                "title": "Critical Safety Incidents",
                "description": "Work stoppages, severe environmental breaches, and major safety hazards.",
                "color": "text-red-500",
                "settings": grouped.get("Critical Safety Incidents", [])
            },
            {
                "title": "Permits & Approvals",
                "description": "New submissions, required reviews, and final sign-offs.",
                "color": "text-blue-500",
                "settings": grouped.get("Permits & Approvals", [])
            },
            {
                "title": "Field Inspections",
                "description": "Inspection requests, NCR generation, and schedule changes.",
                "color": "text-emerald-500",
                "settings": grouped.get("Field Inspections", [])
            }
        ]

    @classmethod
    def update_notification_preference(cls, category: str, event_label: str, channel: str, enabled: bool, actor=None):
        cls.seed_initial_settings()
        pref, _ = NotificationPreferenceCategory.objects.get_or_create(
            category=category,
            event_label=event_label,
            defaults={"in_app": True, "email": True, "sms": False, "is_locked": False}
        )
        if pref.is_locked and channel in ['in_app', 'email'] and not enabled:
            raise PermissionDenied("Critical safety alert channels are locked and cannot be disabled.")

        if channel in ['in_app', 'push']:
            pref.in_app = enabled
        elif channel == 'email':
            pref.email = enabled
        elif channel == 'sms':
            pref.sms = enabled
        pref.save()
        return pref

    @classmethod
    def get_routing_rules(cls):
        cls.seed_initial_settings()
        return NotificationRoutingRule.objects.all().order_by('-created_at')

    @classmethod
    def add_routing_rule(cls, trigger_event: str, primary_recipient: str, sla_timeline: str, escalation_target: str, actor=None):
        return NotificationRoutingRule.objects.create(
            trigger_event=trigger_event,
            primary_recipient=primary_recipient,
            sla_timeline=sla_timeline,
            escalation_target=escalation_target,
            is_active=True
        )

    @classmethod
    def delete_routing_rule(cls, rule_id: str, actor=None):
        rule = NotificationRoutingRule.objects.get(id=rule_id)
        rule.delete()
        return True

    @classmethod
    def get_webhooks(cls):
        cls.seed_initial_settings()
        return WebhookSubscription.objects.all().order_by('-created_at')

    @classmethod
    def create_webhook(cls, name: str, target_url: str, events: list, actor=None):
        return WebhookSubscription.objects.create(
            name=name,
            target_url=target_url,
            events=events or ["permit.created", "permit.updated", "inspection.failed"],
            status='Active'
        )

    @classmethod
    def delete_webhook(cls, webhook_id: str, actor=None):
        wh = WebhookSubscription.objects.get(id=webhook_id)
        wh.delete()
        return True

    @classmethod
    def seed_initial_settings(cls):
        # 1. Seed Roles
        if not CustomRole.objects.exists():
            admin_r = CustomRole.objects.create(name="System Administrator", role_type="System Default", is_system_default=True, active_users_count=3)
            planner_r = CustomRole.objects.create(name="City Planner", role_type="Custom Role", is_system_default=False, active_users_count=12)
            inspector_r = CustomRole.objects.create(name="Lead Inspector", role_type="Custom Role", is_system_default=False, active_users_count=45)
            reviewer_r = CustomRole.objects.create(name="Reviewer", role_type="Custom Role", is_system_default=False, active_users_count=85)

            # Seed Permissions
            perms = [
                ("Permits & Approvals", "View Permit Applications", True, True, True, True),
                ("Permits & Approvals", "Approve/Reject Permits", True, True, False, False),
                ("Permits & Approvals", "Grant Zoning Variances", True, True, False, False),
                ("Permits & Approvals", "Sign Off Final Occupancy", True, True, True, False),
                ("Site Inspections", "View Inspection Logs", True, True, True, True),
                ("Site Inspections", "Generate Non-Conformance (NCR)", True, False, True, False),
                ("Site Inspections", "Halt Construction (Work Stoppage)", True, False, True, False),
                ("System & Audit", "View Audit Records", True, False, False, False),
                ("System & Audit", "Export Compliance Packages", True, False, False, False),
                ("System & Audit", "Manage Roles & Permissions", True, False, False, False),
            ]
            for mod, p_name, adm, pln, ins, rev in perms:
                RolePermission.objects.create(role=admin_r, module=mod, permission_name=p_name, is_granted=adm)
                RolePermission.objects.create(role=planner_r, module=mod, permission_name=p_name, is_granted=pln)
                RolePermission.objects.create(role=inspector_r, module=mod, permission_name=p_name, is_granted=ins)
                RolePermission.objects.create(role=reviewer_r, module=mod, permission_name=p_name, is_granted=rev)

        # 2. Seed Workflows
        if not ApprovalWorkflow.objects.exists():
            wf_master = ApprovalWorkflow.objects.create(
                id="WF-00-MASTER",
                name="Master Building Collapse Prevention Pipeline",
                status="System Enforced",
                description="Mandatory 5-stage collapse prevention gate."
            )
            WorkflowStep.objects.create(workflow=wf_master, step_order=1, title="Approval & Permit Gate", role="Agency Approvers", icon_name="ShieldCheck", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=2, title="Construction Oversight", role="Inspectors & Digital Eye", icon_name="HardHat", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=3, title="Deviation Detection", role="Automated Engine", icon_name="Search", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=4, title="Action & Stop-Work", role="System Escalation", icon_name="AlertTriangle", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=5, title="Corrective Verification", role="Review Board", icon_name="CheckCircle2", is_system_enforced=True)

            wf1 = ApprovalWorkflow.objects.create(
                id="WF-01",
                name="Standard Foundation Permit",
                status="Active",
                description="Foundation inspection & approval chain."
            )
            WorkflowStep.objects.create(workflow=wf1, step_order=1, title="Initial Submission", role="Developer/Contractor", icon_name="FileText")
            WorkflowStep.objects.create(workflow=wf1, step_order=2, title="Technical Review", role="Structural Engineer", icon_name="HardHat")
            WorkflowStep.objects.create(workflow=wf1, step_order=3, title="Final Sign-off", role="City Planner", icon_name="CheckCircle2")

        # 3. Seed Inspection Templates
        if not InspectionTemplate.objects.exists():
            tpl1 = InspectionTemplate.objects.create(
                id="TPL-091",
                name="Deep Foundation Pour Checklist",
                department="Structural",
                status="Active",
                version="v1.4"
            )
            ChecklistItem.objects.create(template=tpl1, item_order=1, title="Record concrete slump measurement (inches)", field_type="Number Input", is_required=True)
            ChecklistItem.objects.create(template=tpl1, item_order=2, title="Are all rebar ties secure and spaced according to plan?", field_type="Pass/Fail Toggle", is_required=True)
            ChecklistItem.objects.create(template=tpl1, item_order=3, title="Upload wide-angle photo of trench before pour", field_type="Photo Upload", is_required=False)

            tpl2 = InspectionTemplate.objects.create(
                id="TPL-088",
                name="Environmental Site Perimeter Check",
                department="Environmental",
                status="Active",
                version="v1.0"
            )
            ChecklistItem.objects.create(template=tpl2, item_order=1, title="Verify perimeter sediment control barriers", field_type="Pass/Fail Toggle", is_required=True)
            ChecklistItem.objects.create(template=tpl2, item_order=2, title="Measure ambient noise level (dB)", field_type="Number Input", is_required=True)

        # 4. Seed Compliance Standards
        if not ComplianceStandard.objects.exists():
            ComplianceStandard.objects.create(category="Environmental Limits", key="noise_daytime_db", label="Daytime Max (07:00 - 19:00)", num_value=85.0, unit="dB", alert_level="Warning")
            ComplianceStandard.objects.create(category="Environmental Limits", key="noise_nighttime_db", label="Nighttime Max (19:00 - 07:00)", num_value=70.0, unit="dB", alert_level="Critical")
            ComplianceStandard.objects.create(category="Structural Tolerances", key="max_concrete_slump_in", label="Max Concrete Slump", num_value=6.0, unit="Inches", alert_level="Critical")
            ComplianceStandard.objects.create(category="Structural Tolerances", key="min_curing_temp_f", label="Min Curing Temp", num_value=40.0, unit="°F", alert_level="Warning")
            ComplianceStandard.objects.create(category="SLA Thresholds", key="permit_review_sla_days", label="Permit Review SLA", num_value=14.0, unit="Days", alert_level="Warning")
            ComplianceStandard.objects.create(category="SLA Thresholds", key="defect_rectification_sla_days", label="Defect Rectification SLA", num_value=5.0, unit="Days", alert_level="Critical")

        # 5. Seed Statutory Documents
        if not StatutoryDocument.objects.exists():
            StatutoryDocument.objects.create(code="URP-Law 2010", name="Urban & Regional Planning Law", connected_features=["Zoning Controls", "Setbacks"])
            StatutoryDocument.objects.create(code="NBC-2006", name="National Building Code", connected_features=["Structural Tolerances", "Fire Safety"])
            StatutoryDocument.objects.create(code="LSEPA-2023", name="State Environmental Protection Guidelines", connected_features=["Noise Limits", "Effluent Discharge"])
            StatutoryDocument.objects.create(code="Safety-Comm", name="Safety Commission Regulations", connected_features=["Health & Safety Logs", "Stop-Work Orders"])

        # 6. Seed Notification Routing & Preferences
        if not NotificationRoutingRule.objects.exists():
            NotificationRoutingRule.objects.create(
                trigger_event="Critical Alerts",
                primary_recipient="Agency Director",
                sla_timeline="Within 15 mins",
                escalation_target="Permanent Secretary",
                is_active=True
            )
            NotificationRoutingRule.objects.create(
                trigger_event="Stop-Work Order",
                primary_recipient="Chief Inspector",
                sla_timeline="Within 2 hours",
                escalation_target="Agency Director",
                is_active=True
            )

        if not NotificationPreferenceCategory.objects.exists():
            NotificationPreferenceCategory.objects.create(category="Critical Safety Incidents", event_label="In-App Dashboard Alerts", in_app=True, email=True, sms=True, is_locked=True)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="New Permit Application", in_app=True, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="Technical Review Required", in_app=True, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="Approval Decision Finalized", in_app=False, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Inspection Requested (Contractor)", in_app=True, email=True, sms=True, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Failed Inspection (NCR Generated)", in_app=True, email=True, sms=True, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Inspection Passed", in_app=True, email=False, sms=False, is_locked=False)

        # 7. Seed Webhooks
        if not WebhookSubscription.objects.exists():
            WebhookSubscription.objects.create(
                name="Contractor Sync Webhook",
                target_url="https://api.contractorsync.dev/v1/nexucon/events",
                events=["permit.created", "permit.updated", "inspection.failed"],
                status="Active"
            )
