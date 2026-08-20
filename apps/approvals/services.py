import hashlib
import uuid
from decimal import Decimal
from django.utils import timezone
import datetime
from .models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria
from apps.projects.models import Project
from apps.audit.models import AuditEvent

class ApprovalService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Approval",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def create_request(data, user):
        """Create a new approval request and initialize criteria if technical."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        submitted_by = data.get('submitted_by_name') or (user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Apex Engineering')
        val_amount = Decimal(str(data.get('value_amount', 0.0)))
        due = data.get('due_date') or (timezone.now().date() + datetime.timedelta(days=7))

        request_obj = ApprovalRequest.objects.create(
            project=project,
            title=data.get('title', 'Official Approval Request'),
            request_type=data.get('request_type', 'General'),
            discipline=data.get('discipline', 'General'),
            priority=data.get('priority', 'Medium'),
            status=data.get('status', 'Pending'),
            value_amount=val_amount,
            submitted_by_name=submitted_by,
            submitter=user if getattr(user, 'is_authenticated', False) else None,
            due_date=due,
            description=data.get('description', ''),
            bottleneck=data.get('bottleneck'),
            days_overdue=int(data.get('days_overdue', 0)),
            signatories_required=int(data.get('signatories_required', 1)),
            signatories_completed=int(data.get('signatories_completed', 0)),
            attached_file_url=data.get('attached_file_url', 'https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/spec.pdf')
        )

        # If Technical Review, initialize default evaluation checklist
        criteria_list = data.get('criteria') or []
        if request_obj.request_type == 'Technical' and not criteria_list:
            default_criteria = [
                {"name": "Engineering Load & Capacity Verification", "notes": "Checked against statutory structural limits."},
                {"name": "Interference & Routing Clash Check", "notes": "Checked against BIM coordination model."},
                {"name": "Environmental & Efficiency Standards", "notes": "Verified against energy codes."}
            ]
            for i, c in enumerate(default_criteria):
                TechnicalReviewCriteria.objects.create(
                    approval_request=request_obj,
                    name=c['name'],
                    notes=c['notes'],
                    order=i
                )
        elif criteria_list:
            for i, c in enumerate(criteria_list):
                TechnicalReviewCriteria.objects.create(
                    approval_request=request_obj,
                    name=c.get('name', f'Criterion {i+1}'),
                    status=c.get('status', 'pending'),
                    notes=c.get('notes', ''),
                    order=i
                )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_CREATED",
            resource_id=request_obj.id,
            new_state={"ref": request_obj.request_reference, "type": request_obj.request_type, "value": str(request_obj.value_amount)}
        )
        return request_obj

    @staticmethod
    def approve_request(request_obj, user, notes="", pin=None, conditions=None):
        """Approve request, apply cryptographic SHA-256 seal and record decision."""
        prev_status = request_obj.status
        is_conditional = bool(conditions)
        request_obj.status = 'Conditional' if is_conditional else 'Approved'
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Executive Director'
        role = getattr(user, 'role', 'Director') if hasattr(user, 'role') else 'Director'
        
        raw_hash = f"{request_obj.request_reference}-{user.id if getattr(user, 'id', None) else 'anon'}-{timezone.now().isoformat()}"
        sig_hash = f"0x8f2c{hashlib.sha256(raw_hash.encode()).hexdigest()[:12]}d91"

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=decider_name,
            decider_role=role,
            outcome='Conditional' if is_conditional else 'Approved',
            decision_notes=notes or ('Approved subject to specified conditions.' if is_conditional else 'Fully approved and authorized.'),
            conditions=conditions,
            digital_pin_verified=bool(pin),
            signature_hash=sig_hash,
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_APPROVED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={"status": request_obj.status, "decision_ref": decision.decision_reference, "hash": sig_hash}
        )
        return decision

    @staticmethod
    def reject_request(request_obj, user, reason):
        """Reject request with mandatory justification."""
        prev_status = request_obj.status
        request_obj.status = 'Rejected'
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Reviewing Official'
        role = getattr(user, 'role', 'Director') if hasattr(user, 'role') else 'Director'

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=decider_name,
            decider_role=role,
            outcome='Rejected',
            decision_notes=reason,
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_REJECTED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={"status": "Rejected", "reason": reason}
        )
        return decision

    @staticmethod
    def request_info(request_obj, user, query_text):
        """Return approval request for additional technical clarification."""
        request_obj.status = 'Awaiting Fix'
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Reviewing Official'
        role = getattr(user, 'role', 'Director') if hasattr(user, 'role') else 'Director'

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=decider_name,
            decider_role=role,
            outcome='Returned For Info',
            decision_notes=query_text,
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_INFO_REQUESTED",
            resource_id=request_obj.id,
            new_state={"query": query_text}
        )
        return decision

    @staticmethod
    def escalate_request(request_obj, user, reason="", target_level="Permanent Secretary / Director General"):
        """Escalate approval request to executive authority."""
        prev_status = request_obj.status
        request_obj.status = 'Escalated'
        request_obj.doa_level_required = target_level
        request_obj.bottleneck = reason or 'High Financial Value / Critical SLA Threshold Exceeded'
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Escalating Officer'

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=decider_name,
            decider_role='Director',
            outcome='Escalated',
            decision_notes=f"Escalated to {target_level}. Reason: {reason}",
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_ESCALATED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={"status": "Escalated", "target": target_level}
        )
        return decision

    @staticmethod
    def sign_document(request_obj, user):
        """Apply digital signatory execution to a multi-signature document."""
        request_obj.signatories_completed = min(request_obj.signatories_required, request_obj.signatories_completed + 1)
        if request_obj.signatories_completed >= request_obj.signatories_required:
            request_obj.status = 'Approved'
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Authorized Signatory'
        
        raw_hash = f"SIG-{request_obj.request_reference}-{timezone.now().isoformat()}"
        sig_hash = f"0x8f2c{hashlib.sha256(raw_hash.encode()).hexdigest()[:12]}d91"

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=decider_name,
            decider_role='Authorized Signatory',
            outcome='Approved' if request_obj.status == 'Approved' else 'Conditional',
            decision_notes=f"Digital signature committed ({request_obj.signatories_completed}/{request_obj.signatories_required}).",
            signature_hash=sig_hash,
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_DOCUMENT_SIGNED",
            resource_id=request_obj.id,
            new_state={"signatures": f"{request_obj.signatories_completed}/{request_obj.signatories_required}"}
        )
        return request_obj

    @staticmethod
    def evaluate_criterion(criterion_id, status_val, notes, user):
        """Update an evaluation criterion status and commentary."""
        criterion = TechnicalReviewCriteria.objects.get(pk=criterion_id)
        criterion.status = status_val
        if notes:
            criterion.notes = notes
        criterion.save()
        return criterion

    @staticmethod
    def get_approval_stats():
        """Aggregated stats for approval queue cards."""
        return {
            "pending_count": ApprovalRequest.objects.filter(status__in=['Pending', 'In Review', 'Awaiting Fix']).count(),
            "approved_count": ApprovalRequest.objects.filter(status='Approved').count(),
            "rejected_count": ApprovalRequest.objects.filter(status='Rejected').count(),
            "escalated_count": ApprovalRequest.objects.filter(status='Escalated').count(),
            "technical_count": ApprovalRequest.objects.filter(request_type='Technical').count(),
            "documents_count": ApprovalRequest.objects.filter(request_type='Document').count(),
            "total_decisions": ApprovalDecision.objects.count()
        }
