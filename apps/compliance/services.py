import hashlib
import uuid
from django.utils import timezone
import datetime
from .models import (
    NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement,
    ComplianceReview, ComplianceCertificate
)
from apps.projects.models import Project
from apps.audit.models import AuditEvent

class ComplianceService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Compliance",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def create_ncr(data, user):
        """Log a new Non-Conformance Report and auto-create an associated CAPA task."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        reported_by = data.get('reported_by_name') or (user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'J. Doe (Safety)')

        ncr = NonConformanceReport.objects.create(
            project=project,
            title=data.get('title', 'Site Non-Conformance Report'),
            description=data.get('description', 'Non-conformance observed during field audit.'),
            severity=data.get('severity', 'Major'),
            category=data.get('category', 'Safety'),
            status=data.get('status', 'Open'),
            reported_by_name=reported_by,
            reporter=user if getattr(user, 'is_authenticated', False) else None,
            assignee_name=data.get('assignee_name', 'Lead Contractor'),
            source=data.get('source', 'MANUAL'),
            source_reference=data.get('source_reference'),
            escalation_level=1,
            date_logged=timezone.now()
        )

        # Automatically create linked CAPA task
        capa_title = f"Corrective Action: {ncr.title}"
        CorrectiveActionPlan.objects.create(
            ncr=ncr,
            project=project,
            title=capa_title,
            action_plan=f"Investigate root cause and implement corrective measures for {ncr.ncr_reference}.",
            priority='Critical' if ncr.severity == 'Critical' else 'High',
            status='todo',
            assignee_name=ncr.assignee_name,
            due_date=timezone.now().date() + datetime.timedelta(days=7 if ncr.severity == 'Critical' else 14)
        )

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_NCR_LOGGED",
            resource_id=ncr.id,
            new_state={"ref": ncr.ncr_reference, "severity": ncr.severity, "category": ncr.category}
        )
        return ncr

    @staticmethod
    def escalate_ncr(ncr, user, target_level=None):
        """Advance the regulatory escalation level (Level 1 to 5)."""
        current_lvl = ncr.escalation_level
        new_lvl = target_level if target_level is not None else min(5, current_lvl + 1)
        
        ncr.escalation_level = new_lvl
        ncr.last_escalated_at = timezone.now()
        if new_lvl >= 4 and ncr.severity != 'Critical':
            ncr.severity = 'Critical'
        ncr.save()

        action_names = {
            1: "Reminder Sent (Auto)",
            2: "Warning Letter Issued (Auto)",
            3: "Escalated to Senior Officer",
            4: "Escalated to Directorate Board",
            5: "Legal Enforcement Proceedings Initiated"
        }

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_NCR_ESCALATED",
            resource_id=ncr.id,
            previous_state={"level": current_lvl},
            new_state={"level": new_lvl, "action": action_names.get(new_lvl)}
        )
        return ncr

    @staticmethod
    def close_ncr(ncr, resolution_notes, user):
        """Verify corrective evidence and close NCR."""
        ncr.status = 'Closed'
        ncr.resolved_at = timezone.now()
        ncr.resolution_notes = resolution_notes or 'Non-conformance rectified and verified by compliance auditor.'
        ncr.save()

        # Close all linked CAPAs
        ncr.capas.filter(status__in=['todo', 'in-progress', 'review']).update(
            status='closed',
            closed_at=timezone.now(),
            verification_notes='Closed upon parent NCR resolution.'
        )

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_NCR_CLOSED",
            resource_id=ncr.id,
            new_state={"status": "Closed", "notes": ncr.resolution_notes}
        )
        return ncr

    @staticmethod
    def create_capa(data, user):
        """Create a new standalone or linked CAPA on the Kanban board."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)
        
        ncr_id = data.get('ncr_id') or data.get('ncr')
        ncr = NonConformanceReport.objects.filter(pk=ncr_id).first() if ncr_id else None

        capa = CorrectiveActionPlan.objects.create(
            project=project,
            ncr=ncr,
            title=data.get('title', 'Corrective Action Plan'),
            action_plan=data.get('action_plan', ''),
            priority=data.get('priority', 'High'),
            status=data.get('status', 'todo'),
            assignee_name=data.get('assignee_name', 'HSE Officer'),
            due_date=data.get('due_date'),
            comments_count=int(data.get('comments_count', 0)),
            attachments_count=int(data.get('attachments_count', 0))
        )

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_CAPA_CREATED",
            resource_id=capa.id,
            new_state={"ref": capa.capa_reference, "title": capa.title, "priority": capa.priority}
        )
        return capa

    @staticmethod
    def transition_capa(capa, new_status, notes, user):
        """Transition CAPA across Kanban columns (todo -> in-progress -> review -> closed)."""
        prev_status = capa.status
        capa.status = new_status
        if notes:
            capa.verification_notes = notes
        if new_status == 'closed':
            capa.closed_at = timezone.now()
        capa.save()

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_CAPA_TRANSITIONED",
            resource_id=capa.id,
            previous_state={"status": prev_status},
            new_state={"status": new_status}
        )
        return capa

    @staticmethod
    def issue_certificate(data, user):
        """Issue an official compliance certificate with SHA-256 QR authenticity seal."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        cert_ref = f"CERT-{data.get('category', 'ENV')[:3].upper()}-{timezone.now().year}"
        hash_raw = f"{cert_ref}-{project.id}-{timezone.now().isoformat()}"
        qr_hash = f"0x7b2a{hashlib.sha256(hash_raw.encode()).hexdigest()[:12]}e41"

        cert = ComplianceCertificate.objects.create(
            certificate_reference=cert_ref,
            project=project,
            title=data.get('title', 'Environmental Clearance Certificate'),
            category=data.get('category', 'Environmental'),
            authority=data.get('authority', 'Environmental Protection Agency (EPA)'),
            issue_date=data.get('issue_date') or timezone.now().date(),
            expiry_date=data.get('expiry_date') or (timezone.now().date() + datetime.timedelta(days=365 * 2)),
            status='Active',
            qr_verification_hash=qr_hash,
            certificate_file_url=data.get('certificate_file_url', 'https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/cert.pdf')
        )

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_CERTIFICATE_ISSUED",
            resource_id=cert.id,
            new_state={"ref": cert.certificate_reference, "hash": qr_hash}
        )
        return cert

    @staticmethod
    def get_overview_stats():
        """Aggregated compliance scorecard metrics for dashboard."""
        today = timezone.now().date()
        open_ncrs = NonConformanceReport.objects.exclude(status='Closed').count()
        critical_ncrs = NonConformanceReport.objects.filter(severity='Critical').exclude(status='Closed').count()
        pending_capas = CorrectiveActionPlan.objects.exclude(status='closed').count()
        valid_certs = ComplianceCertificate.objects.filter(status='Active').count()
        expiring_soon_certs = ComplianceCertificate.objects.filter(expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=30)).count()
        expired_certs = ComplianceCertificate.objects.filter(expiry_date__lt=today).count()

        total_reqs = RegulatoryRequirement.objects.count()
        compliant_reqs = RegulatoryRequirement.objects.filter(status='Compliant').count()
        score = round((compliant_reqs / total_reqs * 100)) if total_reqs > 0 else 92

        return {
            "overall_score": f"{score}%",
            "open_ncrs_count": open_ncrs,
            "critical_ncrs_count": critical_ncrs,
            "pending_capas_count": pending_capas,
            "valid_certificates_count": valid_certs,
            "expiring_soon_certificates_count": expiring_soon_certs,
            "expired_certificates_count": expired_certs,
            "reviews_count": ComplianceReview.objects.count()
        }
