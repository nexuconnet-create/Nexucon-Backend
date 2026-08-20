import datetime
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import Inspection, Finding, StopWorkOrder, Checklist
from apps.projects.models import Project
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
    def create_inspection_request(data, user):
        """Create a new inspection request."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)
        
        inspection_type = data.get('inspection_type', 'Foundation Inspection')
        
        # Load default checklist if template exists
        default_checklist = [
            {"id": "chk_1", "item": "Excavation and soil bearing capacity verified", "status": "PENDING", "notes": ""},
            {"id": "chk_2", "item": "Reinforcement steel bar diameter and spacing matches approved drawings", "status": "PENDING", "notes": ""},
            {"id": "chk_3", "item": "Concrete cover blocks and formwork rigidity verified", "status": "PENDING", "notes": ""},
            {"id": "chk_4", "item": "Site safety barricades and PPE compliance verified", "status": "PENDING", "notes": ""}
        ]

        inspection = Inspection.objects.create(
            project=project,
            permit_id=data.get('permit_id') or data.get('permit'),
            inspection_type=inspection_type,
            status='REQUESTED',
            priority=data.get('priority', 'Normal'),
            requested_by_name=data.get('requested_by_name') or (user.get_full_name() or user.email),
            requested_at=timezone.now(),
            scheduled_date=data.get('scheduled_date'),
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
    def assign_and_schedule(inspection, inspector_user, scheduled_date, actor):
        """Assign field inspector and set schedule date/time."""
        previous_inspector = inspection.inspector_name
        inspection.inspector = inspector_user
        inspection.inspector_name = inspector_user.get_full_name() or inspector_user.email
        inspection.scheduled_date = scheduled_date
        inspection.status = 'SCHEDULED'
        inspection.save()

        InspectionService.log_audit(
            user=actor,
            action="INSPECTION_SCHEDULED",
            resource_id=inspection.id,
            previous_state={"inspector": previous_inspector, "status": "REQUESTED"},
            new_state={"inspector": inspection.inspector_name, "scheduled_date": str(scheduled_date), "status": "SCHEDULED"}
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
            resolution_deadline=data.get('resolution_deadline'),
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
    def issue_stop_work(project, reason, severity, actor, inspection=None, finding=None):
        """Issue Stop-Work Order and suspend project."""
        swo = StopWorkOrder.objects.create(
            project=project,
            inspection=inspection,
            finding=finding,
            reason=reason,
            severity=severity or 'CRITICAL',
            issued_by_name=actor.get_full_name() or actor.email,
            issued_at=timezone.now(),
            status='ACTIVE'
        )

        # Suspend project
        project.status = 'SUSPENDED'
        project.save()

        if inspection:
            inspection.status = 'FAILED'
            inspection.outcome = 'FAILED'
            inspection.save()

        InspectionService.log_audit(
            user=actor,
            action="STOP_WORK_ORDER_ISSUED",
            resource_id=swo.id,
            new_state={"order_number": swo.order_number, "project": project.name, "reason": reason}
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
        active_swos = StopWorkOrder.objects.filter(project=project, status='ACTIVE').exclude(id=swo.id)
        if not active_swos.exists():
            project.status = 'ACTIVE'
            project.save()

        InspectionService.log_audit(
            user=actor,
            action="STOP_WORK_ORDER_LIFTED",
            resource_id=swo.id,
            new_state={"order_number": swo.order_number, "justification": justification}
        )
        return swo

    @staticmethod
    def create_reinspection(original_inspection, scheduled_date, actor):
        """Auto-create a Re-Inspection for an inspection that failed or required verification."""
        reinspection = Inspection.objects.create(
            project=original_inspection.project,
            permit=original_inspection.permit,
            inspector=original_inspection.inspector,
            inspector_name=original_inspection.inspector_name,
            inspection_type='Re-Inspection',
            status='SCHEDULED',
            priority='High',
            requested_by_name=actor.get_full_name() or actor.email,
            requested_at=timezone.now(),
            scheduled_date=scheduled_date,
            summary_notes=f"Re-inspection for {original_inspection.inspection_reference} regarding unresolved findings.",
            parent_inspection=original_inspection,
            checklist_results=original_inspection.checklist_results
        )

        InspectionService.log_audit(
            user=actor,
            action="RE_INSPECTION_CREATED",
            resource_id=reinspection.id,
            new_state={"reference": reinspection.inspection_reference, "parent": original_inspection.inspection_reference}
        )
        return reinspection
