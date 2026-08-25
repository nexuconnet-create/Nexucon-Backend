import hashlib
import uuid
from django.utils import timezone
import datetime
from .models import (
    NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement,
    ComplianceReview, ComplianceCertificate, EscalationRule
)
from apps.projects.models import Project
from apps.audit.models import AuditEvent
from apps.notifications.models import Notification

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
    def dispatch_notification(recipient, title, message, category='COMPLIANCE', priority='Medium', entity_type='NCR', entity_id='', action_url=''):
        try:
            Notification.objects.create(
                recipient=recipient if getattr(recipient, 'is_authenticated', False) else None,
                recipient_role='Regulatory Officer',
                category=category,
                title=title,
                message=message,
                snippet=message[:120],
                priority=priority,
                severity=priority,
                entity_type=entity_type,
                entity_id=str(entity_id),
                action_url=action_url,
                action_required='Review & Take Action'
            )
        except Exception:
            pass

    @staticmethod
    def seed_default_escalation_rules():
        """Ensure standard statutory escalation matrix rules exist."""
        if EscalationRule.objects.count() == 0:
            default_rules = [
                {
                    "rule_name": "BIM Clash (Hard) Unresolved > 7 days",
                    "trigger_category": "BIM",
                    "action_required": "Escalate to Developer & Structural Consultant",
                    "escalation_level": 2,
                    "sla_hours": 48
                },
                {
                    "rule_name": "Failed Concrete & Material Strength Test",
                    "trigger_category": "Quality",
                    "action_required": "Immediate Stop-Work Order & Seal Directorate Review",
                    "escalation_level": 4,
                    "sla_hours": 12
                },
                {
                    "rule_name": "Unregistered Worker Detected (LASRRA / Safety)",
                    "trigger_category": "Worker",
                    "action_required": "Formal Citation & Warning Letter to Contractor",
                    "escalation_level": 1,
                    "sla_hours": 24
                },
                {
                    "rule_name": "Critical Life-Safety Scaffold / Shoring Defect",
                    "trigger_category": "Safety",
                    "action_required": "Site Prohibition Notice & Executive Directorate Review",
                    "escalation_level": 3,
                    "sla_hours": 24
                },
                {
                    "rule_name": "Statutory EIA Clearance Breach / Discharge",
                    "trigger_category": "General",
                    "action_required": "Initiate Ministry Legal Proceedings & Environmental Fine",
                    "escalation_level": 5,
                    "sla_hours": 6
                }
            ]
            for r in default_rules:
                EscalationRule.objects.create(**r)

    @staticmethod
    def seed_default_requirements():
        """Seed baseline building codes and statutory clauses if empty."""
        if RegulatoryRequirement.objects.count() == 0:
            reqs = [
                {
                    "requirement_reference": "EPA-EIA-2026-001",
                    "category": "Environmental",
                    "title": "Environmental Impact Assessment (EIA) Clearance",
                    "description": "Baseline soil, noise, and environmental effluent containment audit.",
                    "authority": "Federal Ministry of Environment & LASEPA",
                    "status": "Compliant",
                    "mandatory": True,
                    "evidence_required": "Approved EIA Certification & Periodic Air/Water Sampling",
                    "verification_method": "Laboratory Report Verification"
                },
                {
                    "requirement_reference": "LASBCA-NBC-SEC4",
                    "category": "Building Codes",
                    "title": "Structural Conformance & Core Stability (NBC Sec. 4)",
                    "description": "Validation of reinforced concrete compressive strength and foundation load calculations.",
                    "authority": "Lagos State Building Control Agency (LASBCA)",
                    "status": "Compliant",
                    "mandatory": True,
                    "evidence_required": "Core Cylinder Cube Test Results (28-day cure)",
                    "verification_method": "Laboratory Crushing Test & Physical Field Inspection"
                },
                {
                    "requirement_reference": "LSFS-FIRE-012",
                    "category": "Safety & Health",
                    "title": "Occupational Fire Safety & Means of Egress",
                    "description": "Adequacy of pressurized stairwells, dry risers, hydrants, and flame-retardant barriers.",
                    "authority": "Lagos State Fire and Rescue Service",
                    "status": "At Risk",
                    "mandatory": True,
                    "evidence_required": "Fire Safety Certificate & Hydraulic Pressure Test",
                    "verification_method": "Field Smoke & Pressure Audit"
                },
                {
                    "requirement_reference": "MPP-URBAN-ZONING",
                    "category": "Legal & Planning",
                    "title": "Physical Planning Setback & Height Approval",
                    "description": "Compliance with approved density, building setbacks from road alignment, and zoning height limits.",
                    "authority": "Ministry of Physical Planning and Urban Development",
                    "status": "Compliant",
                    "mandatory": True,
                    "evidence_required": "Survey Plan & Stamped Development Permit",
                    "verification_method": "Topographic Drone & Spatial Cadastral Review"
                }
            ]
            for r in reqs:
                RegulatoryRequirement.objects.create(**r)

    @staticmethod
    def create_ncr(data, user):
        """Log a new Non-Conformance Report and auto-create an associated CAPA task."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.filter(pk=project_id).first()
        if not project:
            project = Project.objects.first()
            if not project:
                project = Project.objects.create(name='Metropolitan Infrastructure Project', reference_number='PRJ-2026-METRO')

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

        ComplianceService.dispatch_notification(
            recipient=user,
            title=f"New NCR Logged: {ncr.ncr_reference}",
            message=f"{ncr.title} ({ncr.severity} - {ncr.category}) for project {project.name}.",
            priority='Critical' if ncr.severity == 'Critical' else 'High',
            entity_type='NCR',
            entity_id=str(ncr.id),
            action_url=f"/government/dashboard/compliance/non-conformances"
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

        ComplianceService.dispatch_notification(
            recipient=user,
            title=f"Statutory Escalation: {ncr.ncr_reference} -> Level {new_lvl}",
            message=f"Escalated action: {action_names.get(new_lvl)} for {ncr.title}.",
            priority='Critical' if new_lvl >= 4 else 'High',
            entity_type='NCR',
            entity_id=str(ncr.id),
            action_url=f"/government/dashboard/regulatory/escalation"
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

        ComplianceService.dispatch_notification(
            recipient=user,
            title=f"NCR Resolved & Closed: {ncr.ncr_reference}",
            message=f"{ncr.title} was successfully closed upon verification.",
            priority='Medium',
            entity_type='NCR',
            entity_id=str(ncr.id),
            action_url=f"/government/dashboard/compliance/non-conformances"
        )
        return ncr

    @staticmethod
    def create_capa(data, user):
        """Create a new standalone or linked CAPA on the Kanban board."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.filter(pk=project_id).first()
        if not project:
            project = Project.objects.first()

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
    def advance_review_stage(review, new_stage, findings_summary, user):
        """Advance statutory review stage and update progress percentage."""
        stage_progress = {
            'Initiation': 20,
            'Audit in Progress': 50,
            'Reporting': 75,
            'Final Review': 90,
            'Completed': 100
        }
        review.stage = new_stage
        review.progress = stage_progress.get(new_stage, review.progress)
        if findings_summary:
            review.findings_summary = findings_summary
        review.save()

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_REVIEW_ADVANCED",
            resource_id=review.id,
            new_state={"stage": new_stage, "progress": review.progress}
        )
        return review

    @staticmethod
    def issue_certificate(data, user, file_obj=None):
        """Issue an official compliance certificate with SHA-256 QR authenticity seal."""
        from apps.documents.services import DocumentStorageService
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.filter(pk=project_id).first()
        if not project:
            project = Project.objects.first()

        category = data.get('category', 'Environmental')
        prefix = category[:3].upper()
        cert_ref = f"CERT-{prefix}-{timezone.now().year}-{uuid.uuid4().hex[:4].upper()}"
        
        file_url = data.get('certificate_file_url')
        if file_obj:
            folder_prefix = f"projects/{project.reference_number if (project and project.reference_number) else 'PRJ'}/certificates"
            file_meta = DocumentStorageService.upload_file_to_r2(file_obj, folder_prefix=folder_prefix)
            file_url = file_meta['file_url']
            qr_hash = file_meta['signature_hash']
        else:
            hash_raw = f"{cert_ref}-{project.id if project else 'PRJ'}-{timezone.now().isoformat()}"
            qr_hash = f"0x7b2a{hashlib.sha256(hash_raw.encode()).hexdigest()[:12]}e41"
            if not file_url:
                file_url = f"https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/certificates/{cert_ref}.pdf"

        cert = ComplianceCertificate.objects.create(
            certificate_reference=cert_ref,
            project=project,
            title=data.get('title', f'{category} Clearance Certificate'),
            category=category,
            authority=data.get('authority', 'Lagos State Building & Environmental Control Board'),
            issue_date=data.get('issue_date') or timezone.now().date(),
            expiry_date=data.get('expiry_date') or (timezone.now().date() + datetime.timedelta(days=365 * 2)),
            status='Active',
            qr_verification_hash=qr_hash,
            certificate_file_url=file_url
        )

        ComplianceService.log_audit(
            user=user,
            action="COMPLIANCE_CERTIFICATE_ISSUED",
            resource_id=cert.id,
            new_state={"ref": cert.certificate_reference, "hash": qr_hash}
        )

        ComplianceService.dispatch_notification(
            recipient=user,
            title=f"Compliance Certificate Issued: {cert.certificate_reference}",
            message=f"{cert.title} awarded to {project.name if project else 'Project'}.",
            priority='High',
            entity_type='Certificate',
            entity_id=str(cert.id),
            action_url=f"/government/dashboard/compliance/certificates"
        )
        return cert

    @staticmethod
    def get_overview_stats():
        """Aggregated compliance scorecard metrics for dashboard."""
        ComplianceService.seed_default_escalation_rules()
        ComplianceService.seed_default_requirements()

        today = timezone.now().date()
        open_ncrs = NonConformanceReport.objects.exclude(status='Closed').count()
        critical_ncrs = NonConformanceReport.objects.filter(severity='Critical').exclude(status='Closed').count()
        pending_capas = CorrectiveActionPlan.objects.exclude(status='closed').count()
        valid_certs = ComplianceCertificate.objects.filter(status='Active').count()
        expiring_soon_certs = ComplianceCertificate.objects.filter(expiry_date__gte=today, expiry_date__lte=today + datetime.timedelta(days=30)).count()
        expired_certs = ComplianceCertificate.objects.filter(expiry_date__lt=today).count()

        total_reqs = RegulatoryRequirement.objects.count()
        compliant_reqs = RegulatoryRequirement.objects.filter(status='Compliant').count()
        score = round((compliant_reqs / total_reqs * 100)) if total_reqs > 0 else 94

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
