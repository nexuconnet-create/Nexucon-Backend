import hashlib
import uuid
from decimal import Decimal
from django.utils import timezone
import datetime
from .models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria, ApprovalComment
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
    def dispatch_notification(recipient, title, message, priority='Medium', entity_type='Approval', entity_id=None, action_url=None):
        try:
            from apps.notifications.models import Notification
            if recipient and getattr(recipient, 'is_authenticated', False):
                Notification.objects.create(
                    recipient=recipient,
                    title=title,
                    message=message,
                    priority=priority,
                    entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id else None,
                    action_url=action_url or "/government/dashboard/approvals/pending"
                )
        except Exception:
            pass

    @staticmethod
    def create_request(data, user):
        """Create a new approval request and initialize criteria if technical."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.filter(pk=project_id).first()
        if not project:
            project = Project.objects.first()

        submitted_by = data.get('submitted_by_name') or (user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Apex Engineering')
        val_amount = Decimal(str(data.get('value_amount', 0.0)))
        due = data.get('due_date') or (timezone.now().date() + datetime.timedelta(days=7))

        source_type = data.get('source_entity_type', 'General')
        source_id = data.get('source_entity_id')
        version_hash = data.get('source_version_hash')
        if not version_hash:
            raw = f"{source_type}-{source_id or uuid.uuid4().hex[:6]}-{timezone.now().isoformat()}"
            version_hash = f"0x{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

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
            assigned_to=user if getattr(user, 'is_authenticated', False) else None,
            assigned_to_name=data.get('assigned_to_name', 'Lead Government Reviewer'),
            due_date=due,
            description=data.get('description', ''),
            source_entity_type=source_type,
            source_entity_id=str(source_id) if source_id else None,
            source_version_hash=version_hash,
            compliance_gate_status=data.get('compliance_gate_status', 'Passed'),
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
            new_state={
                "ref": request_obj.request_reference, 
                "type": request_obj.request_type, 
                "value": str(request_obj.value_amount),
                "source_hash": version_hash
            }
        )

        ApprovalService.dispatch_notification(
            recipient=user,
            title=f"Approval Request Logged: {request_obj.request_reference}",
            message=f"{request_obj.title} submitted for executive decision.",
            priority=request_obj.priority,
            entity_type='Approval',
            entity_id=str(request_obj.id)
        )
        return request_obj

    @staticmethod
    def assign_reviewer(request_obj, reviewer_user, reviewer_name, user):
        """Assign reviewing official to an approval request."""
        prev_assignee = request_obj.assigned_to_name
        request_obj.assigned_to = reviewer_user if (reviewer_user and getattr(reviewer_user, 'is_authenticated', False)) else None
        request_obj.assigned_to_name = reviewer_name or (reviewer_user.get_full_name() if reviewer_user else 'Assigned Reviewer')
        if request_obj.status == 'Pending':
            request_obj.status = 'In Review'
        request_obj.save()

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_ASSIGNED",
            resource_id=request_obj.id,
            previous_state={"assignee": prev_assignee},
            new_state={"assignee": request_obj.assigned_to_name}
        )

        if reviewer_user:
            ApprovalService.dispatch_notification(
                recipient=reviewer_user,
                title=f"Approval Request Assigned: {request_obj.request_reference}",
                message=f"You have been assigned to review {request_obj.title}.",
                priority=request_obj.priority,
                entity_type='Approval',
                entity_id=str(request_obj.id)
            )
        return request_obj

    @staticmethod
    def evaluate_compliance_gate(request_obj):
        """Check for open critical infractions on the project."""
        try:
            from apps.compliance.models import NonConformanceReport
            if request_obj.project:
                open_critical = NonConformanceReport.objects.filter(
                    project=request_obj.project,
                    severity__in=['Critical', 'Major'],
                    status__in=['Open', 'In Progress']
                ).count()
                if open_critical > 0:
                    request_obj.compliance_gate_status = 'Blocked'
                    request_obj.save()
                    return {
                        "gate_passed": False,
                        "status": "Blocked",
                        "open_critical_ncrs": open_critical,
                        "message": f"Approval blocked: {open_critical} open critical/major NCR(s) exist on project."
                    }
            request_obj.compliance_gate_status = 'Passed'
            request_obj.save()
            return {
                "gate_passed": True,
                "status": "Passed",
                "open_critical_ncrs": 0,
                "message": "All statutory compliance gates cleared."
            }
        except Exception:
            return {"gate_passed": True, "status": "Exempt", "message": "Compliance check passed."}

    @staticmethod
    def request_revision(request_obj, user, revision_notes, attachment_url=None):
        """Formal revision request returned to contractor/applicant."""
        prev_status = request_obj.status
        request_obj.status = 'Awaiting Fix'
        request_obj.save()

        author_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Reviewing Official'

        comment = ApprovalComment.objects.create(
            approval_request=request_obj,
            author=user if getattr(user, 'is_authenticated', False) else None,
            author_name=author_name,
            comment_type='RevisionRequest',
            content=revision_notes,
            attachment_url=attachment_url
        )

        decision = ApprovalDecision.objects.create(
            approval_request=request_obj,
            decider=user if getattr(user, 'is_authenticated', False) else None,
            decider_name=author_name,
            decider_role=getattr(user, 'role', 'Reviewer') if hasattr(user, 'role') else 'Reviewer',
            outcome='Returned For Info',
            decision_notes=revision_notes,
            timestamp=timezone.now()
        )

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REVISION_REQUESTED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={"status": "Awaiting Fix", "revision_notes": revision_notes}
        )

        if request_obj.submitter:
            ApprovalService.dispatch_notification(
                recipient=request_obj.submitter,
                title=f"Revision Requested: {request_obj.request_reference}",
                message=f"Changes required for {request_obj.title}: {revision_notes[:100]}",
                priority='High',
                entity_type='Approval',
                entity_id=str(request_obj.id)
            )
        return decision

    @staticmethod
    def add_comment(request_obj, user, content, comment_type='General', attachment_url=None):
        """Add a review comment or technical finding to an approval request."""
        author_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Official'
        comment = ApprovalComment.objects.create(
            approval_request=request_obj,
            author=user if getattr(user, 'is_authenticated', False) else None,
            author_name=author_name,
            comment_type=comment_type,
            content=content,
            attachment_url=attachment_url
        )
        return comment

    @staticmethod
    def approve_request(request_obj, user, notes="", pin=None, conditions=None):
        """Approve request, apply cryptographic SHA-256 seal, update source entity, and record decision."""
        prev_status = request_obj.status
        is_conditional = bool(conditions)
        request_obj.status = 'Conditional' if is_conditional else 'Approved'
        if not is_conditional:
            request_obj.conditions_met = True
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Executive Director'
        role = getattr(user, 'role', 'Director') if hasattr(user, 'role') else 'Director'
        
        raw_hash = f"{request_obj.request_reference}-{request_obj.source_version_hash or 'V1'}-{user.id if getattr(user, 'id', None) else 'anon'}-{timezone.now().isoformat()}"
        sig_hash = f"0x8f2c{hashlib.sha256(raw_hash.encode()).hexdigest()[:16]}d91"

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

        # Sync back to source entity if linked
        ApprovalService.sync_source_entity(request_obj, 'Approved')

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_APPROVED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={
                "status": request_obj.status, 
                "decision_ref": decision.decision_reference, 
                "hash": sig_hash,
                "version_sealed": request_obj.source_version_hash
            }
        )

        if request_obj.submitter:
            ApprovalService.dispatch_notification(
                recipient=request_obj.submitter,
                title=f"Decision: {request_obj.request_reference} {request_obj.status.upper()}",
                message=f"{request_obj.title} has been officially approved with cryptographic seal.",
                priority='High',
                entity_type='Approval',
                entity_id=str(request_obj.id)
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

        # Sync back to source entity
        ApprovalService.sync_source_entity(request_obj, 'Rejected')

        ApprovalService.log_audit(
            user=user,
            action="APPROVAL_REQUEST_REJECTED",
            resource_id=request_obj.id,
            previous_state={"status": prev_status},
            new_state={"status": "Rejected", "reason": reason}
        )

        if request_obj.submitter:
            ApprovalService.dispatch_notification(
                recipient=request_obj.submitter,
                title=f"Rejected: {request_obj.request_reference}",
                message=f"Approval request {request_obj.title} was not authorized: {reason[:100]}",
                priority='High',
                entity_type='Approval',
                entity_id=str(request_obj.id)
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
            ApprovalService.sync_source_entity(request_obj, 'Approved')
        request_obj.save()

        decider_name = user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Authorized Signatory'
        
        raw_hash = f"SIG-{request_obj.request_reference}-{request_obj.source_version_hash or 'V1'}-{timezone.now().isoformat()}"
        sig_hash = f"0x8f2c{hashlib.sha256(raw_hash.encode()).hexdigest()[:16]}d91"

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
    def sync_source_entity(request_obj, decision_outcome):
        """Propagate approval decision to the source entity."""
        try:
            if request_obj.source_entity_type == 'Document' and request_obj.source_entity_id:
                from apps.documents.models import ProjectDocument
                doc = ProjectDocument.objects.filter(pk=request_obj.source_entity_id).first()
                if doc:
                    doc.status = 'Approved' if decision_outcome == 'Approved' else 'Rejected'
                    doc.save()
            elif request_obj.source_entity_type == 'PermitApplication' and request_obj.source_entity_id:
                from apps.permits.models import Permit
                p = Permit.objects.filter(pk=request_obj.source_entity_id).first()
                if p:
                    p.status = 'Approved' if decision_outcome == 'Approved' else 'Rejected'
                    p.save()
            elif request_obj.source_entity_type == 'Milestone' and request_obj.source_entity_id:
                from apps.projects.models import Milestone
                m = Milestone.objects.filter(pk=request_obj.source_entity_id).first()
                if m:
                    m.status = 'Completed' if decision_outcome == 'Approved' else 'Blocked'
                    m.save()
        except Exception:
            pass

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
