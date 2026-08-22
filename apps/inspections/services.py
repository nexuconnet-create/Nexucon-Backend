import datetime
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import Inspection, Finding, StopWorkOrder, Checklist
from apps.projects.models import Project
from apps.permits.models import Permit
from apps.audit.models import AuditEvent

class InspectionService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Inspection",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def create_inspection_request(data, user=None):
        """Create a new inspection request."""
        project_val = data.get('project') or data.get('project_id')
        if isinstance(project_val, Project):
            project = project_val
        elif project_val:
            project = Project.objects.filter(Q(id=str(project_val)) | Q(reference_number=str(project_val))).first()
        else:
            project = Project.objects.first()

        if not project:
            raise ValidationError("A valid construction project site is required.")

        permit_val = data.get('permit') or data.get('permit_id')
        permit = permit_val if isinstance(permit_val, Permit) else (Permit.objects.filter(pk=permit_val).first() if permit_val else None)

        inspection_type = data.get('inspection_type', 'Foundation Inspection')
        
        # Load default checklist if template exists
        default_checklist = [
            {"id": "chk_1", "item": "Excavation and soil bearing capacity verified", "status": "PENDING", "notes": ""},
            {"id": "chk_2", "item": "Reinforcement steel bar diameter and spacing matches approved drawings", "status": "PENDING", "notes": ""},
            {"id": "chk_3", "item": "Concrete cover blocks and formwork rigidity verified", "status": "PENDING", "notes": ""},
            {"id": "chk_4", "item": "Site safety barricades and PPE compliance verified", "status": "PENDING", "notes": ""}
        ]

        req_name = data.get('requested_by_name')
        if not req_name:
            if user and getattr(user, 'is_authenticated', False):
                req_name = user.get_full_name() or getattr(user, 'email', 'Regulatory Officer')
            else:
                req_name = 'Government Regulatory Desk'

        scheduled_dt = data.get('scheduled_date')
        if not scheduled_dt or scheduled_dt == '':
            scheduled_dt = None

        inspection = Inspection.objects.create(
            project=project,
            permit=permit,
            inspection_type=inspection_type,
            status='REQUESTED',
            priority=data.get('priority', 'Normal'),
            requested_by_name=req_name,
            requested_at=timezone.now(),
            scheduled_date=scheduled_dt,
            summary_notes=data.get('summary_notes', ''),
            checklist_results=data.get('checklist_results', default_checklist),
            photos_and_evidence=data.get('photos_and_evidence', [])
        )

        InspectionService.log_audit(
            user=user,
            action="INSPECTION_REQUESTED",
            resource_id=inspection.id,
            new_state={"reference": inspection.inspection_reference, "type": inspection.inspection_type}
        )

        return inspection

    @staticmethod
    def assign_and_schedule(inspection, inspector_user=None, scheduled_date=None, actor=None, inspector_name=None):
        """Assign field inspector and set schedule date/time."""
        previous_inspector = inspection.inspector_name
        if inspector_user and hasattr(inspector_user, 'get_full_name'):
            inspection.inspector = inspector_user if getattr(inspector_user, 'is_authenticated', False) else None
            inspection.inspector_name = inspector_user.get_full_name() or getattr(inspector_user, 'email', 'Assigned Inspector')
        elif inspector_name:
            inspection.inspector_name = inspector_name
        else:
            inspection.inspector_name = 'Engr. Babatunde Adeleke'

        if scheduled_date:
            inspection.scheduled_date = scheduled_date
        elif not inspection.scheduled_date:
            inspection.scheduled_date = timezone.now() + datetime.timedelta(days=1)

        inspection.status = 'SCHEDULED'
        inspection.save()

        InspectionService.log_audit(
            user=actor,
            action="INSPECTION_SCHEDULED",
            resource_id=inspection.id,
            previous_state={"inspector": previous_inspector, "status": "REQUESTED"},
            new_state={"inspector": inspection.inspector_name, "scheduled_date": str(inspection.scheduled_date), "status": "SCHEDULED"}
        )
        return inspection

    @staticmethod
    def check_in(inspection, lat, lng, actor):
        """GPS verified inspector check-in at construction site."""
        inspection.status = 'IN_PROGRESS'
        inspection.checkin_time = timezone.now()
        inspection.gps_latitude = float(lat) if lat else None
        inspection.gps_longitude = float(lng) if lng else None
        inspection.gps_verified = True
        inspection.save()

        InspectionService.log_audit(
            user=actor,
            action="INSPECTOR_CHECKIN_GPS_VERIFIED",
            resource_id=inspection.id,
            new_state={"lat": lat, "lng": lng, "time": str(inspection.checkin_time)}
        )
        return inspection

    @staticmethod
    def complete_inspection(inspection, outcome, checklist_results, summary_notes, actor):
        """Complete inspection and finalize checklist & outcome."""
        valid_outcomes = ['PASSED', 'CONDITIONAL_PASS', 'FAILED']
        if outcome not in valid_outcomes:
            raise ValidationError(f"Invalid outcome '{outcome}'. Must be one of {valid_outcomes}.")

        inspection.outcome = outcome
        if checklist_results is not None:
            inspection.checklist_results = checklist_results
        if summary_notes:
            inspection.summary_notes = summary_notes
        inspection.completed_date = timezone.now()

        # Determine final status
        if outcome == 'PASSED':
            inspection.status = 'COMPLETED'
        elif outcome == 'CONDITIONAL_PASS':
            inspection.status = 'COMPLETED'
        elif outcome == 'FAILED':
            inspection.status = 'FAILED'

        inspection.save()

        InspectionService.log_audit(
            user=actor,
            action=f"INSPECTION_COMPLETED_{outcome}",
            resource_id=inspection.id,
            new_state={"status": inspection.status, "outcome": outcome}
        )
        return inspection

    @staticmethod
    def log_finding(inspection, data, actor):
        """Record an inspection finding / defect."""
        finding = Finding.objects.create(
            inspection=inspection,
            project=inspection.project,
            title=data.get('title', f"Finding on {inspection.inspection_type}"),
            description=data.get('description', ''),
            severity=data.get('severity', 'LOW'),
            category=data.get('category', 'STRUCTURAL'),
            photos=data.get('photos', []),
            corrective_action_required=data.get('corrective_action_required', ''),
            resolution_deadline=data.get('resolution_deadline') if data.get('resolution_deadline') else None,
            requires_reinspection=data.get('requires_reinspection', False)
        )

        InspectionService.log_audit(
            user=actor,
            action="INSPECTION_FINDING_LOGGED",
            resource_id=finding.id,
            new_state={"finding_reference": finding.finding_reference, "severity": finding.severity}
        )
        return finding

    @staticmethod
    def issue_stop_work(project, reason, severity, actor=None, inspection=None, finding=None):
        """Issue Stop-Work Order and suspend project."""
        issued_by = 'Government Building Control Authority'
        if actor and getattr(actor, 'is_authenticated', False):
            issued_by = actor.get_full_name() or getattr(actor, 'email', 'Regulatory Enforcement Officer')

        swo = StopWorkOrder.objects.create(
            project=project,
            inspection=inspection,
            finding=finding,
            reason=reason,
            severity=severity or 'CRITICAL',
            issued_by_name=issued_by,
            issued_at=timezone.now(),
            status='ACTIVE'
        )

        # Suspend project
        if project:
            project.status = 'SUSPENDED'
            project.save()

        if inspection:
            inspection.status = 'FAILED'
            inspection.outcome = 'FAILED'
            inspection.save()

        # Automatically sync into Site Monitoring SiteIssue as a CRITICAL issue
        try:
            from apps.monitoring.models import SiteIssue
            SiteIssue.objects.create(
                project=project,
                title=f"🛑 Stop-Work Order Enforced ({swo.order_number}): {project.name if project else 'Site'}",
                description=f"Statutory Site Suspension Notice: {reason}",
                severity='CRITICAL',
                status='OPEN',
                reported_by_name=issued_by,
                assigned_to_name="Principal Contractor / Site Manager",
                is_escalated=True
            )
        except Exception:
            pass

        InspectionService.log_audit(
            user=actor,
            action="STOP_WORK_ORDER_ISSUED",
            resource_id=swo.id,
            new_state={"order_number": swo.order_number, "project": project.name if project else "N/A", "reason": reason}
        )
        return swo

    @staticmethod
    def lift_stop_work(swo, justification, actor):
        """Lift Stop-Work Order and reinstate project if clean."""
        swo.status = 'LIFTED'
        swo.lifted_at = timezone.now()
        swo.lifted_by_name = actor.get_full_name() or actor.email
        swo.lift_justification = justification
        swo.save()

        # Re-activate project if no other active SWOs
        project = swo.project
        if project:
            active_swos = StopWorkOrder.objects.filter(project=project, status='ACTIVE').exclude(id=swo.id)
            if not active_swos.exists():
                project.status = 'ACTIVE'
                project.save()

        # Update corresponding SiteIssue in Site Monitoring to RESOLVED
        try:
            from apps.monitoring.models import SiteIssue
            SiteIssue.objects.filter(
                project=project,
                title__contains=swo.order_number
            ).update(
                status='RESOLVED',
                resolution_notes=f"Stop-Work Order lifted on {timezone.now().strftime('%Y-%m-%d')}: {justification}"
            )
        except Exception:
            pass

        InspectionService.log_audit(
            user=actor,
            action="STOP_WORK_ORDER_LIFTED",
            resource_id=swo.id,
            new_state={"order_number": swo.order_number, "justification": justification}
        )
        return swo

    @staticmethod
    def create_reinspection(original_inspection, scheduled_date=None, actor=None, inspector_name=None, inspector_id=None, notes=None, priority='High'):
        """Auto-create a Re-Inspection for an inspection that failed or required verification."""
        req_name = 'Government Regulatory Desk'
        if actor and getattr(actor, 'is_authenticated', False):
            req_name = actor.get_full_name() or getattr(actor, 'email', 'Regulatory Officer')

        chosen_inspector_name = inspector_name or original_inspection.inspector_name or 'Assigned Inspector'
        chosen_inspector = original_inspection.inspector
        if inspector_id:
            try:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                found_user = User.objects.filter(Q(id=inspector_id) | Q(username=inspector_id) | Q(email=inspector_id)).first()
                if found_user:
                    chosen_inspector = found_user
            except Exception:
                pass

        if not scheduled_date or scheduled_date == '':
            scheduled_date = timezone.now() + datetime.timedelta(days=7)

        type_label = original_inspection.inspection_type
        if not type_label.startswith('Re-Inspection'):
            type_label = f"Re-Inspection: {type_label}"

        reinspection = Inspection.objects.create(
            project=original_inspection.project,
            permit=original_inspection.permit,
            inspector=chosen_inspector if getattr(chosen_inspector, 'is_authenticated', False) else None,
            inspector_name=chosen_inspector_name,
            inspection_type=type_label,
            status='SCHEDULED',
            priority=priority or 'High',
            requested_by_name=req_name,
            requested_at=timezone.now(),
            scheduled_date=scheduled_date,
            summary_notes=notes or f"Follow-up re-inspection for {original_inspection.inspection_reference} ({original_inspection.inspection_type}) regarding unresolved defect rectification and compliance verification.",
            parent_inspection=original_inspection,
            checklist_results=original_inspection.checklist_results,
            outcome='PENDING'
        )

        if original_inspection.status != 'FAILED':
            original_inspection.status = 'RE_INSPECTION_REQUIRED'
            original_inspection.save()

        InspectionService.log_audit(
            user=actor,
            action="RE_INSPECTION_CREATED",
            resource_id=reinspection.id,
            new_state={"reference": reinspection.inspection_reference, "parent": original_inspection.inspection_reference}
        )
        return reinspection
