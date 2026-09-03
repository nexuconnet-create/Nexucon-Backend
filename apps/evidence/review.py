"""
Human-in-the-Loop Review & Statutory NCR Loop (implementation plan §5 Week 5).

The AI never declares a project compliant or non-compliant: every
CorrelationFinding requires a qualified human decision. This service
implements the statutory loop:

    Accept / Reject / Modify AI Finding
      -> Request Supplementary Evidence / Live Stream
      -> Trigger Immediate Statutory Inspection
      -> Issue Formal Non-Conformance (NCR)
      -> Assign Corrective Action & Re-inspection
      -> Formal Director Review & Sign-off

Every decision is written to the immutable audit ledger (apps.audit) and to
the hash-chained FindingRevision history.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime

from django.utils import timezone

from .correlation import CorrelationEngine
from .models import CorrelationFinding, FindingRevision

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    """Raised for invalid review transitions."""


def record_audit(user, action, resource, resource_id, *, severity='Normal',
                 new_state=None, previous_state=None, metadata=None, session_id=None):
    """Append an immutable audit event for a human decision (plan §5 W5 QA:
    'Human decision audit logging & hash verification')."""
    from apps.audit.models import AuditEvent

    return AuditEvent.objects.create(
        user=user,
        user_name=(user.get_full_name() if user and user.is_authenticated else 'Unauthenticated'),
        user_email=(user.email if user and user.is_authenticated else None),
        action=action,
        resource_type=resource,
        resource_id=str(resource_id),
        session_id=session_id,
        severity=severity,
        previous_state=previous_state,
        new_state=new_state,
        metadata=metadata or {},
    )


def decision_hash(finding_revision: FindingRevision, user, decision: str) -> str:
    """
    Cryptographic verification hash over a human decision: binds the finding
    revision, reviewer identity, decision and timestamp.
    """
    payload = json.dumps({
        'revision': finding_revision.revision_number,
        'revision_hash': finding_revision.revision_hash,
        'reviewer': str(user.id) if user else 'anonymous',
        'decision': decision,
        'recorded_at': finding_revision.recorded_at.isoformat(),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class HumanReviewService:
    """Human-in-the-Loop review & verification API backend (plan §5 W5)."""

    DECISIONS = ('accept', 'reject', 'modify', 'escalate')

    @classmethod
    def review(cls, finding: CorrelationFinding, user, decision: str, notes: str = '',
               modifications: dict = None):
        """
        Record a human decision on an AI finding:
          accept   -> status 'accepted'
          reject   -> status 'rejected'
          modify   -> status 'modified' (applies reviewer modifications)
          escalate -> handled by issue_ncr / trigger_inspection
        """
        if decision not in cls.DECISIONS:
            raise ReviewError(f"decision must be one of {cls.DECISIONS}")
        if finding.status not in ('pending_review', 'modified'):
            raise ReviewError(
                f"Finding {finding.finding_reference} already resolved as '{finding.status}'."
            )

        previous_state = {
            'status': finding.status,
            'risk_level': finding.risk_level,
            'risk_score': finding.risk_score,
            'title': finding.title,
        }

        if decision == 'accept':
            finding.status = 'accepted'
        elif decision == 'reject':
            finding.status = 'rejected'
        elif decision == 'modify':
            modifications = modifications or {}
            for field in ('title', 'description', 'risk_level', 'risk_score',
                          'structural_element_id', 'bim_guid'):
                if field in modifications:
                    setattr(finding, field, modifications[field])
            finding.status = 'modified'

        finding.reviewed_by = user
        finding.reviewed_at = timezone.now()
        finding.review_notes = notes or ''
        finding.save()

        revision = CorrelationEngine._add_revision(
            finding, 'decision', changed_by=user, notes=f"{decision}: {notes}",
        )
        digest = decision_hash(revision, user, decision)
        revision.snapshot['decision_hash'] = digest
        # Re-seal the hash chain: _add_revision hashed the snapshot before the
        # decision digest was added, so the revision hash is recomputed over
        # the final sealed snapshot or verify_chain() would always fail.
        revision.revision_hash = revision.compute_hash(revision.previous_hash)
        revision.save()

        record_audit(
            user, f'evidence.finding.{decision}', 'CorrelationFinding', finding.id,
            severity='High' if decision != 'reject' else 'Normal',
            previous_state=previous_state,
            new_state={'status': finding.status, 'risk_level': finding.risk_level},
            metadata={'decision_hash': digest, 'notes': notes},
        )
        return finding, digest

    @classmethod
    def request_supplementary_evidence(cls, finding: CorrelationFinding, user,
                                       request_type: str = 'supplementary', notes: str = ''):
        """
        Action: Request Supplementary Evidence / Live Stream. Keeps the
        finding pending and records the request in the audit trail + revision
        history so the field team sees an open request.
        """
        REQUEST_TYPES = ('supplementary', 'live_stream', 'retest_ndt')
        if request_type not in REQUEST_TYPES:
            raise ReviewError(f"request_type must be one of {REQUEST_TYPES}")

        finding.status = 'pending_review' if finding.status == 'pending_review' else finding.status
        revision = CorrelationEngine._add_revision(
            finding, 'new_evidence', changed_by=user,
            notes=f"Supplementary evidence requested ({request_type}): {notes}",
        )
        record_audit(
            user, 'evidence.finding.supplementary_request', 'CorrelationFinding', finding.id,
            severity='Normal',
            new_state={'request_type': request_type, 'notes': notes},
        )
        return finding, revision

    @classmethod
    def trigger_inspection(cls, finding: CorrelationFinding, user,
                           inspection_type: str = 'Structural Review',
                           priority: str = 'High', notes: str = ''):
        """
        Action: Trigger Immediate Statutory Inspection — creates a real
        Inspection record linked back to the AI finding.
        """
        from apps.inspections.models import Inspection

        if finding.linked_inspection_id:
            raise ReviewError(
                f"Finding {finding.finding_reference} already has a linked inspection."
            )

        inspection = Inspection.objects.create(
            project=finding.project,
            inspection_type=inspection_type,
            priority=priority,
            status='REQUESTED',
            requested_by_name=(user.get_full_name() if user.is_authenticated else 'HQ Review'),
            summary_notes=(
                f"Triggered by AI correlation finding {finding.finding_reference} "
                f"(risk {finding.risk_level}). {notes}".strip()
            ),
        )
        finding.linked_inspection = inspection
        finding.status = 'escalated'
        finding.reviewed_by = user
        finding.reviewed_at = timezone.now()
        finding.review_notes = notes or finding.review_notes
        finding.save()

        CorrelationEngine._add_revision(
            finding, 'escalated', changed_by=user,
            notes=f"Statutory inspection {inspection.inspection_reference} triggered.",
        )
        record_audit(
            user, 'evidence.finding.trigger_inspection', 'CorrelationFinding', finding.id,
            severity='High',
            new_state={'inspection_reference': inspection.inspection_reference},
        )
        return inspection

    @classmethod
    def issue_ncr(cls, finding: CorrelationFinding, user, *, title: str = None,
                  description: str = None, severity: str = None, category: str = 'Structural',
                  corrective_action: str = '', corrective_due_date=None):
        """
        Action: Issue Formal Non-Conformance (NCR) from an accepted/escalated
        AI finding — creates a statutory NCR + Corrective Action Plan and
        links them back to the finding.
        """
        from apps.compliance.models import NonConformanceReport, CorrectiveActionPlan

        if finding.status == 'rejected':
            raise ReviewError("Cannot issue an NCR from a rejected finding.")
        if finding.linked_ncr_id:
            raise ReviewError(
                f"Finding {finding.finding_reference} already has NCR "
                f"{finding.linked_ncr.ncr_reference}."
            )

        severity_map = {
            'critical': 'Critical', 'high': 'Major', 'medium': 'Major', 'low': 'Minor',
            'info': 'Minor',
        }
        ncr = NonConformanceReport.objects.create(
            project=finding.project,
            title=title or f"AI-corroborated non-conformance: {finding.title}",
            description=description or (
                f"Issued from AI correlation finding {finding.finding_reference} "
                f"(risk {finding.risk_level}"
                f"{f', score {finding.risk_score}' if finding.risk_score is not None else ''}). "
                f"{finding.description}"
            ),
            severity=severity or severity_map.get(finding.risk_level, 'Major'),
            category=category,
            status='Open',
            source='SITE_MONITORING',
            source_reference=finding.finding_reference,
            reporter=user if user.is_authenticated else None,
            reported_by_name=(user.get_full_name() if user.is_authenticated else 'AI Evidence Review'),
        )

        capa = None
        if corrective_action:
            capa = CorrectiveActionPlan.objects.create(
                ncr=ncr,
                project=finding.project,
                title=f"Corrective action for {ncr.ncr_reference}",
                action_plan=corrective_action,
                priority='Critical' if ncr.severity == 'Critical' else 'High',
                status='todo',
                due_date=corrective_due_date,
            )

        finding.linked_ncr = ncr
        finding.status = 'escalated'
        finding.reviewed_by = user
        finding.reviewed_at = timezone.now()
        finding.save()

        CorrelationEngine._add_revision(
            finding, 'escalated', changed_by=user,
            notes=f"Formal NCR {ncr.ncr_reference} issued"
                  f"{' with corrective action ' + capa.capa_reference if capa else ''}.",
        )
        record_audit(
            user, 'evidence.finding.issue_ncr', 'CorrelationFinding', finding.id,
            severity='Critical' if ncr.severity == 'Critical' else 'High',
            new_state={'ncr_reference': ncr.ncr_reference, 'severity': ncr.severity},
        )
        return ncr, capa

    @classmethod
    def director_signoff(cls, finding: CorrelationFinding, user, decision: str, notes: str = ''):
        """
        Formal Director review & sign-off (plan §5 W5): the director confirms
        the reviewed finding into the official record with a cryptographic
        signature over the final revision.
        """
        if not user or not user.is_authenticated:
            raise ReviewError("Director sign-off requires an authenticated director.")
        if finding.status not in ('accepted', 'modified', 'escalated'):
            raise ReviewError(
                "Director sign-off requires the finding to have been accepted, modified or escalated first."
            )
        if decision not in ('approve', 'return'):
            raise ReviewError("decision must be 'approve' or 'return'.")

        revision = CorrelationEngine._add_revision(
            finding, 'decision', changed_by=user,
            notes=f"Director sign-off: {decision}. {notes}".strip(),
        )
        digest = decision_hash(revision, user, f"director_{decision}")
        revision.snapshot['director_signoff'] = {
            'decision': decision,
            'director': user.get_full_name() or user.email,
            'signoff_hash': digest,
            'signed_at': revision.recorded_at.isoformat(),
        }
        # Re-seal the hash chain over the signed snapshot (see review()).
        revision.revision_hash = revision.compute_hash(revision.previous_hash)
        revision.save()

        record_audit(
            user, 'evidence.finding.director_signoff', 'CorrelationFinding', finding.id,
            severity='Critical',
            new_state={'decision': decision, 'signoff_hash': digest},
        )
        return revision, digest
