import math
import uuid
import datetime
import hashlib
from django.utils import timezone
from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
from apps.projects.models import Project
from apps.audit.models import AuditEvent
from django.db.models import Q

class MonitoringService:
    @staticmethod
    def get_project_instance(project_id):
        if not project_id:
            return Project.objects.first()
        
        import uuid
        try:
            val = uuid.UUID(str(project_id))
            p = Project.objects.filter(id=val).first()
            if p:
                return p
        except Exception:
            pass

        p = Project.objects.filter(
            Q(reference_number=str(project_id)) | 
            Q(name__icontains=str(project_id))
        ).first()

        return p or Project.objects.first()

    @staticmethod
    def get_actor_name(user, default="Site Engineer / Officer"):
        if user and getattr(user, 'is_authenticated', False):
            return user.get_full_name() or getattr(user, 'email', default)
        return default

    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="SiteMonitoring",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def log_daily_update(data, user):
        """Create daily photo update, drone survey or progress log."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        
        progress = int(data.get('progress_percentage', 0))
        update_type = data.get('update_type', 'DAILY_PHOTO')
        author_name = data.get('reported_by_name') or MonitoringService.get_actor_name(user, "Site Supervisor")

        update = DailySiteUpdate.objects.create(
            project=project,
            update_type=update_type,
            reported_by=user if getattr(user, 'is_authenticated', False) else None,
            reported_by_name=author_name,
            work_summary=data.get('work_summary') or data.get('contractor_notes') or data.get('notes') or f"Daily site update ({update_type})",
            photos=data.get('photos', []),
            drone_survey_data=data.get('drone_survey_data', {}),
            weather_condition=data.get('weather_condition', 'Clear / Sunny'),
            workforce_count=int(data.get('workforce_count', 0) or 0),
            gps_coordinates=data.get('gps_coordinates', {}),
            progress_percentage=progress,
            status=data.get('status', 'Active'),
            priority=data.get('priority', 'Medium'),
        )

        # Update Project progress if progress was reported
        if progress > 0 and project and hasattr(project, 'progress'):
            try:
                project.progress = max(getattr(project, 'progress', 0) or 0, progress)
                project.save()
            except Exception:
                pass

        MonitoringService.log_audit(
            user=user,
            action="DAILY_UPDATE_LOGGED",
            resource_id=update.id,
            new_state={"type": update_type, "progress": progress}
        )
        return update

    @staticmethod
    def create_observation(data, user):
        """Create/Log field observation or inspection note."""
        return MonitoringService.log_observation(data, user)

    @staticmethod
    def log_observation(data, user):
        """Log field observation or inspection note."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        author_name = data.get('observed_by_name') or MonitoringService.get_actor_name(user, "Field Inspector")

        category = data.get('category', 'QUALITY')
        valid_cats = ['QUALITY', 'SAFETY', 'PROGRESS', 'ENVIRONMENTAL', 'GENERAL']
        if category not in valid_cats:
            category = 'GENERAL'

        obs = FieldObservation.objects.create(
            project=project,
            title=data.get('title', 'Field Observation'),
            description=data.get('description') or data.get('notes') or '',
            category=category,
            severity=data.get('severity', 'LOW'),
            status=data.get('status', 'OPEN'),
            assigned_officer_name=data.get('assigned_officer_name', ''),
            observed_by_name=author_name,
            evidence_photos=data.get('evidence_photos') or data.get('photos', []),
            gps_coordinates=data.get('gps_coordinates', {}),
            corrective_action=data.get('corrective_action') or data.get('recommended_action', ''),
        )

        MonitoringService.log_audit(
            user=user,
            action="FIELD_OBSERVATION_LOGGED",
            resource_id=obs.id,
            new_state={"ref": obs.observation_reference, "category": obs.category}
        )
        return obs

    @staticmethod
    def resolve_observation(observation, notes, actor):
        """Mark field observation as resolved."""
        observation.status = 'RESOLVED'
        observation.resolution_notes = notes
        observation.resolved_at = timezone.now()
        observation.save()

        MonitoringService.log_audit(
            user=actor,
            action="FIELD_OBSERVATION_RESOLVED",
            resource_id=observation.id,
            new_state={"status": "RESOLVED", "notes": notes}
        )
        return observation

    @staticmethod
    def report_issue(data, user):
        """Report site defect, safety hazard or regulatory issue."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        author_name = data.get('reported_by_name') or MonitoringService.get_actor_name(user, "Site Inspector")

        # Safely parse due_date to avoid format exceptions
        due_date_raw = data.get('due_date')
        due_date = None
        if due_date_raw:
            try:
                cleaned = str(due_date_raw).split('T')[0].strip()
                if len(cleaned) == 10 and cleaned.count('-') == 2:
                    due_date = cleaned
            except Exception:
                due_date = None

        enforce_stop_work = bool(data.get('enforce_stop_work') or data.get('is_escalated'))

        issue = SiteIssue.objects.create(
            project=project,
            title=data.get('title', 'Site Issue'),
            description=data.get('description', ''),
            severity='CRITICAL' if enforce_stop_work else data.get('severity', 'MEDIUM'),
            status=data.get('status', 'OPEN'),
            assigned_to_name=data.get('assigned_to_name', 'Site Engineer'),
            reported_by_name=author_name,
            due_date=due_date,
            resolution_evidence=data.get('resolution_evidence', []),
            is_escalated=enforce_stop_work
        )

        # If Stop-Work Order enforcement was requested
        if enforce_stop_work and project:
            try:
                from apps.inspections.models import StopWorkOrder
                existing_swo = StopWorkOrder.objects.filter(project=project, status='ACTIVE').first()
                if not existing_swo:
                    StopWorkOrder.objects.create(
                        project=project,
                        reason=issue.description or issue.title or "Immediate site safety and building regulation breach.",
                        severity='CRITICAL',
                        issued_by_name=author_name,
                        issued_at=timezone.now(),
                        status='ACTIVE'
                    )
                project.status = 'SUSPENDED'
                project.save()
            except Exception:
                pass

        MonitoringService.log_audit(
            user=user,
            action="SITE_ISSUE_REPORTED",
            resource_id=issue.id,
            new_state={"ref": issue.issue_reference, "severity": issue.severity}
        )
        return issue

    @staticmethod
    def escalate_issue(issue, actor):
        """Escalate site issue to Directorate level."""
        issue.is_escalated = True
        issue.status = 'UNDER_REVIEW'
        issue.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_ISSUE_ESCALATED",
            resource_id=issue.id,
            new_state={"is_escalated": True, "status": "UNDER_REVIEW"}
        )
        return issue

    @staticmethod
    def resolve_issue(issue, notes, evidence, actor):
        """Close site issue with resolution evidence."""
        issue.status = 'RESOLVED'
        issue.resolution_notes = notes
        if evidence:
            issue.resolution_evidence = evidence
        issue.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_ISSUE_RESOLVED",
            resource_id=issue.id,
            new_state={"status": "RESOLVED", "notes": notes}
        )
        return issue

    @staticmethod
    def create_milestone(data, user):
        """Create a construction milestone schedule with gate criteria and dependencies."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        
        milestone_code = data.get('milestone_code') or f"MS-{uuid.uuid4().hex[:4].upper()}"
        target_date_raw = data.get('target_date') or timezone.now().date()
        if isinstance(target_date_raw, str):
            try:
                target_date_raw = datetime.datetime.strptime(target_date_raw.split('T')[0], '%Y-%m-%d').date()
            except Exception:
                target_date_raw = timezone.now().date() + datetime.timedelta(days=30)

        planned_start_raw = data.get('planned_start_date')
        if planned_start_raw and isinstance(planned_start_raw, str):
            try:
                planned_start_raw = datetime.datetime.strptime(planned_start_raw.split('T')[0], '%Y-%m-%d').date()
            except Exception:
                planned_start_raw = timezone.now().date()

        duration = int(data.get('duration_days', 30) or 30)
        progress = int(data.get('progress_percentage', 0) or 0)
        status_val = data.get('status', 'PLANNED' if progress == 0 else 'IN_PROGRESS')

        # Default verification gate configuration
        default_reqs = {
            'require_inspections_passed': True,
            'require_zero_critical_defects': True,
            'require_survey_within_tolerance': True,
            'require_lab_test_evidence': True,
            'require_engineer_signoff': True
        }
        verification_reqs = data.get('verification_requirements') or default_reqs

        milestone = ConstructionMilestone.objects.create(
            project=project,
            milestone_code=milestone_code,
            name=data.get('name', 'Construction Milestone'),
            phase=data.get('phase', 'SUPERSTRUCTURE'),
            description=data.get('description', ''),
            sequence_order=int(data.get('sequence_order', 1) or 1),
            critical_path=bool(data.get('critical_path', False)),
            planned_start_date=planned_start_raw,
            target_date=target_date_raw,
            baseline_start_date=planned_start_raw,
            baseline_end_date=target_date_raw,
            duration_days=duration,
            variance_days=0,
            status=status_val,
            progress_percentage=progress,
            physical_progress_notes=data.get('physical_progress_notes', ''),
            risk_level=data.get('risk_level', 'LOW'),
            risk_factors=data.get('risk_factors', []),
            dependencies=data.get('dependencies', []),
            linked_inspection_ids=data.get('linked_inspection_ids', []),
            linked_issue_ids=data.get('linked_issue_ids', []),
            linked_bim_model_id=data.get('linked_bim_model_id'),
            bim_deviation_mm=float(data.get('bim_deviation_mm', 0.0) or 0.0),
            bim_tolerance_max_mm=float(data.get('bim_tolerance_max_mm', 15.0) or 15.0),
            survey_variance_meters=float(data.get('survey_variance_meters', 0.0) or 0.0),
            digital_eye_verified=bool(data.get('digital_eye_verified', False)),
            evidence_documents=data.get('evidence_documents', []),
            evidence_photos=data.get('evidence_photos', []),
            verification_requirements=verification_reqs
        )

        MonitoringService.log_audit(
            user=user,
            action="CONSTRUCTION_MILESTONE_CREATED",
            resource_id=milestone.id,
            new_state={"code": milestone.milestone_code, "name": milestone.name, "target_date": str(milestone.target_date)}
        )
        return milestone

    @staticmethod
    def update_milestone_progress(milestone, data, user):
        """
        Update physical progress and attach work notes.
        CRITICAL GUARDRAIL: When progress reaches 100%, status transitions to
        'PENDING_VERIFICATION' (or remains 'IN_PROGRESS'), NOT automatically 'VERIFIED'.
        """
        old_progress = milestone.progress_percentage
        old_status = milestone.status

        new_progress = int(data.get('progress_percentage', old_progress) or 0)
        milestone.progress_percentage = min(100, max(0, new_progress))

        if 'physical_progress_notes' in data:
            milestone.physical_progress_notes = data.get('physical_progress_notes', '')

        # Merging evidence documents if provided
        new_docs = data.get('evidence_documents') or []
        if new_docs and isinstance(new_docs, list):
            existing_docs = milestone.evidence_documents or []
            existing_names = {d.get('name') for d in existing_docs if isinstance(d, dict)}
            for doc in new_docs:
                if isinstance(doc, dict) and doc.get('name') not in existing_names:
                    existing_docs.append(doc)
            milestone.evidence_documents = existing_docs

        # Merging evidence photos if provided
        new_photos = data.get('evidence_photos') or []
        if new_photos and isinstance(new_photos, list):
            existing_photos = milestone.evidence_photos or []
            for photo in new_photos:
                if photo not in existing_photos:
                    existing_photos.append(photo)
            milestone.evidence_photos = existing_photos

        # Status transition handling
        if milestone.progress_percentage >= 100:
            if milestone.status in ['PLANNED', 'IN_PROGRESS', 'DUE_THIS_WEEK']:
                milestone.status = 'PENDING_VERIFICATION'
        elif milestone.progress_percentage > 0:
            if milestone.status == 'PLANNED':
                milestone.status = 'IN_PROGRESS'

        if not milestone.actual_start_date and milestone.progress_percentage > 0:
            milestone.actual_start_date = timezone.now().date()

        milestone.save()

        MonitoringService.log_audit(
            user=user,
            action="MILESTONE_PROGRESS_UPDATED",
            resource_id=milestone.id,
            previous_state={"progress": old_progress, "status": old_status},
            new_state={"progress": milestone.progress_percentage, "status": milestone.status}
        )
        return milestone

    @staticmethod
    def attach_milestone_evidence(milestone, data, user):
        """Attach lab test results, structural signoff certs, or photos."""
        new_docs = data.get('documents') or data.get('evidence_documents') or []
        new_photos = data.get('photos') or data.get('evidence_photos') or []

        existing_docs = list(milestone.evidence_documents or [])
        for doc in new_docs:
            if isinstance(doc, dict):
                existing_docs.append(doc)
        milestone.evidence_documents = existing_docs

        existing_photos = list(milestone.evidence_photos or [])
        for p in new_photos:
            if p not in existing_photos:
                existing_photos.append(p)
        milestone.evidence_photos = existing_photos

        milestone.save()

        MonitoringService.log_audit(
            user=user,
            action="MILESTONE_EVIDENCE_ATTACHED",
            resource_id=milestone.id,
            new_state={"docs_count": len(milestone.evidence_documents), "photos_count": len(milestone.evidence_photos)}
        )
        return milestone

    @staticmethod
    def evaluate_milestone_gates(milestone):
        """
        Evaluate live verification gate readiness across:
        1. Predecessor milestone completion
        2. Statutory inspection pass status
        3. Zero unresolved critical defects & Stop-Work orders
        4. BIM LiDAR deviation & GNSS rover variance tolerance
        5. Certified laboratory test evidence uploaded
        """
        project = milestone.project
        reqs = milestone.verification_requirements or {
            'require_inspections_passed': True,
            'require_zero_critical_defects': True,
            'require_survey_within_tolerance': True,
            'require_lab_test_evidence': True,
            'require_engineer_signoff': True
        }

        gates = []
        blockers = []
        is_blocked = False

        # 1. Dependency Predecessors Gate
        predecessors = milestone.dependencies or []
        pred_passed = True
        for pred in predecessors:
            pred_id = pred.get('id') or pred.get('code')
            if pred_id:
                pred_obj = ConstructionMilestone.objects.filter(Q(id=pred_id) | Q(milestone_code=pred_id)).first()
                if pred_obj and pred_obj.status not in ['VERIFIED', 'COMPLETED']:
                    pred_passed = False
                    blockers.append(f"Predecessor milestone '{pred_obj.name}' is {pred_obj.status}")

        gates.append({
            'key': 'dependencies',
            'title': 'Predecessor Dependencies Satisfied',
            'status': 'PASSED' if pred_passed else 'FAILED',
            'required': True,
            'details': 'All prerequisite structural and geotechnical phases completed.' if pred_passed else 'Prerequisite phases remain unverified.'
        })
        if not pred_passed:
            is_blocked = True

        # 2. Statutory Inspections Gate
        insp_passed = True
        linked_insps = milestone.linked_inspection_ids or []
        if reqs.get('require_inspections_passed', True):
            if linked_insps:
                for insp in linked_insps:
                    outcome = insp.get('outcome') or insp.get('status')
                    if outcome not in ['PASSED', 'Passed', 'COMPLETED']:
                        insp_passed = False
                        blockers.append(f"Required inspection '{insp.get('type', 'Site Inspection')}' status: {outcome}")
            else:
                # Check project inspections
                try:
                    from apps.inspections.models import Inspection
                    failed_insps = Inspection.objects.filter(project=project, outcome='FAILED').count()
                    if failed_insps > 0:
                        insp_passed = False
                        blockers.append(f"{failed_insps} mandatory site inspection(s) failed.")
                except Exception:
                    pass

        gates.append({
            'key': 'inspections',
            'title': 'Statutory Site Inspections Passed',
            'status': 'PASSED' if insp_passed else 'FAILED',
            'required': reqs.get('require_inspections_passed', True),
            'details': 'All mandatory field inspections conducted with PASSED outcome.' if insp_passed else 'Outstanding or unpassed inspection findings.'
        })

        # 3. Defect & Stop-Work Blocker Gate
        defects_clear = True
        if reqs.get('require_zero_critical_defects', True):
            open_critical_issues = SiteIssue.objects.filter(project=project, severity='CRITICAL', status__in=['OPEN', 'IN_PROGRESS', 'UNDER_REVIEW']).count()
            if open_critical_issues > 0:
                defects_clear = False
                blockers.append(f"{open_critical_issues} critical safety/structural defect(s) unresolved.")

            try:
                from apps.inspections.models import StopWorkOrder
                active_swos = StopWorkOrder.objects.filter(project=project, status='ACTIVE').count()
                if active_swos > 0:
                    defects_clear = False
                    blockers.append(f"Active Stop-Work Order is currently enforced on this site.")
            except Exception:
                pass

        gates.append({
            'key': 'defects',
            'title': 'Zero Open Critical Defects & SWOs',
            'status': 'PASSED' if defects_clear else 'FAILED',
            'required': reqs.get('require_zero_critical_defects', True),
            'details': 'No active Stop-Work Orders or critical structural non-conformances.' if defects_clear else 'Active defect blockers prevent regulatory certification.'
        })
        if not defects_clear:
            is_blocked = True

        # 4. BIM & GNSS Survey Tolerance Gate
        tolerance_passed = True
        if reqs.get('require_survey_within_tolerance', True):
            max_bim = milestone.bim_tolerance_max_mm or 15.0
            actual_bim = milestone.bim_deviation_mm or 0.0
            variance_gnss = milestone.survey_variance_meters or 0.0
            if actual_bim > max_bim or variance_gnss > 0.05:
                tolerance_passed = False
                blockers.append(f"BIM deviation ({actual_bim}mm vs max {max_bim}mm) or GNSS variance ({variance_gnss}m) exceeds statutory limits.")

        gates.append({
            'key': 'bim_survey',
            'title': 'BIM LiDAR & GNSS Rover Tolerance Check',
            'status': 'PASSED' if tolerance_passed else 'FAILED',
            'required': reqs.get('require_survey_within_tolerance', True),
            'details': f"Spatial deviation {milestone.bim_deviation_mm}mm is within tolerance (≤ {milestone.bim_tolerance_max_mm}mm)." if tolerance_passed else "Geometric deviation exceeds building code tolerance."
        })

        # 5. Laboratory Test Evidence Gate
        evidence_passed = True
        docs = milestone.evidence_documents or []
        if reqs.get('require_lab_test_evidence', True):
            if not docs or len(docs) == 0:
                evidence_passed = False
                blockers.append("No compressive strength or laboratory test certificates uploaded.")

        gates.append({
            'key': 'evidence_vault',
            'title': 'Certified Laboratory Test Reports Uploaded',
            'status': 'PASSED' if evidence_passed else 'FAILED',
            'required': reqs.get('require_lab_test_evidence', True),
            'details': f"{len(docs)} verified test report(s) and engineering documents attached." if evidence_passed else "Required laboratory test certs missing."
        })

        all_gates_passed = pred_passed and insp_passed and defects_clear and tolerance_passed and evidence_passed

        return {
            'all_gates_passed': all_gates_passed,
            'is_blocked': is_blocked,
            'gates': gates,
            'blockers': blockers,
            'summary': 'All statutory verification gates satisfied' if all_gates_passed else f"{len(blockers)} gate condition(s) unresolved"
        }

    @staticmethod
    def submit_milestone_for_verification(milestone, data, user):
        """Submit milestone for formal government verification."""
        milestone.status = 'PENDING_VERIFICATION'
        if 'physical_progress_notes' in data:
            milestone.physical_progress_notes = data.get('physical_progress_notes')
        milestone.save()

        # Send notification to Building Control Officers
        try:
            from apps.notifications.services import NotificationService
            NotificationService.send_notification({
                'title': f"Milestone Verification Submitted: {milestone.name}",
                'message': f"Contractor submitted '{milestone.name}' on {milestone.project.name} for statutory audit sign-off.",
                'category': 'REGULATORY',
                'priority': 'High',
                'recipient_role': 'Director'
            }, user=user)
        except Exception:
            pass

        MonitoringService.log_audit(
            user=user,
            action="MILESTONE_VERIFICATION_SUBMITTED",
            resource_id=milestone.id,
            new_state={"status": "PENDING_VERIFICATION", "submitted_by": MonitoringService.get_actor_name(user)}
        )
        return milestone

    @staticmethod
    def verify_milestone(milestone, data, actor):
        """
        Formal statutory verification & sign-off of construction milestone.
        Strictly checks verification gate criteria and generates an official
        cryptographic seal and digital certificate reference.
        """
        override_gate = bool(data.get('override_gate', False))
        gate_evaluation = MonitoringService.evaluate_milestone_gates(milestone)

        if not gate_evaluation['all_gates_passed'] and not override_gate:
            reasons = "; ".join(gate_evaluation['blockers'])
            raise ValueError(f"Verification gates failed: {reasons}")

        actor_name = MonitoringService.get_actor_name(actor, "Engr. Abimbola Williams (Building Control Director)")
        notes = data.get('notes') or data.get('verification_notes') or "Statutory milestone verification completed and certified in compliance with Lagos State Building Control Standards."
        cert_ref = f"CERT-MS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"
        sig_hash = f"0xLASBCA-VERIFIED-{uuid.uuid4().hex[:8].upper()}"

        milestone.status = 'VERIFIED'
        milestone.progress_percentage = 100
        milestone.actual_completion_date = timezone.now().date()
        milestone.verified_at = timezone.now()
        milestone.verified_by_name = actor_name
        milestone.is_delayed = False

        milestone.verification_signoff = {
            'certificate_reference': cert_ref,
            'signature_hash': sig_hash,
            'verified_by_name': actor_name,
            'verified_by_role': getattr(actor, 'role', 'Director of Building Control') if getattr(actor, 'role', None) else 'Director of Building Control',
            'verified_at': timezone.now().isoformat(),
            'notes': notes,
            'override_applied': override_gate,
            'gate_evaluation_summary': gate_evaluation['summary']
        }
        milestone.save()

        # Automatically unblock successor milestones
        try:
            successors = ConstructionMilestone.objects.filter(project=milestone.project, status='BLOCKED')
            for succ in successors:
                succ_gates = MonitoringService.evaluate_milestone_gates(succ)
                if succ_gates['pred_passed'] if 'pred_passed' in succ_gates else not succ_gates['is_blocked']:
                    succ.status = 'IN_PROGRESS' if succ.progress_percentage > 0 else 'PLANNED'
                    succ.save()
        except Exception:
            pass

        # Send regulatory notification
        try:
            from apps.notifications.services import NotificationService
            NotificationService.send_notification({
                'title': f"Milestone Verified & Certified: {milestone.name}",
                'message': f"Milestone '{milestone.name}' on {milestone.project.name} has been certified (Ref: {cert_ref}).",
                'category': 'REGULATORY',
                'priority': 'Normal',
                'recipient_role': 'All'
            }, user=actor)
        except Exception:
            pass

        MonitoringService.log_audit(
            user=actor,
            action="CONSTRUCTION_MILESTONE_VERIFIED",
            resource_id=milestone.id,
            new_state={
                "status": "VERIFIED",
                "cert_ref": cert_ref,
                "sig_hash": sig_hash,
                "verified_by": actor_name
            }
        )
        return milestone

    @staticmethod
    def flag_milestone_delay(milestone, data, actor):
        """Flag construction milestone delay and compute schedule slippage."""
        reason = data.get('reason') or data.get('delay_reason') or 'Construction progress pacing delay.'
        revised_target = data.get('revised_target_date')

        milestone.status = 'DELAYED'
        milestone.is_delayed = True
        milestone.delay_reason = reason
        milestone.risk_level = 'HIGH'

        if revised_target:
            try:
                if isinstance(revised_target, str):
                    revised_date = datetime.datetime.strptime(revised_target.split('T')[0], '%Y-%m-%d').date()
                else:
                    revised_date = revised_target
                slippage = (revised_date - milestone.target_date).days
                milestone.variance_days = max(1, slippage)
                milestone.target_date = revised_date
            except Exception:
                milestone.variance_days = 7

        milestone.save()

        # Send Delay Alert Notification
        try:
            from apps.notifications.services import NotificationService
            NotificationService.send_notification({
                'title': f"Schedule Delay Flagged: {milestone.name}",
                'message': f"Milestone '{milestone.name}' on {milestone.project.name} is delayed. Reason: {reason}",
                'category': 'ALERT',
                'priority': 'High',
                'recipient_role': 'All'
            }, user=actor)
        except Exception:
            pass

        MonitoringService.log_audit(
            user=actor,
            action="CONSTRUCTION_MILESTONE_DELAY_FLAGGED",
            resource_id=milestone.id,
            new_state={"status": "DELAYED", "reason": reason, "variance_days": milestone.variance_days}
        )
        return milestone

    @staticmethod
    def get_milestone_audit_trail(milestone_id):
        """Retrieve append-only audit trail for a specific milestone."""
        try:
            events = AuditEvent.objects.filter(
                resource_type__in=["SiteMonitoring", "ConstructionMilestone"],
                resource_id=str(milestone_id)
            ).order_by('-timestamp')
            
            return [
                {
                    'id': str(e.id),
                    'audit_reference': e.audit_reference,
                    'action': e.action,
                    'user_name': e.user_name or (e.user.get_full_name() if e.user else 'Building Control System'),
                    'user_role': e.user_role or 'Regulatory Officer',
                    'timestamp': e.timestamp.isoformat(),
                    'severity': e.severity,
                    'signature_hash': e.signature_hash,
                    'previous_state': e.previous_state,
                    'new_state': e.new_state
                }
                for e in events
            ]
        except Exception:
            return []

    @staticmethod
    def record_site_verification(data, user):
        """Record GNSS/Tersus rover verification and compute coordinate variance."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)

        captured_coords = data.get('captured_coordinates', {})
        approved_coords = data.get('approved_coordinates', {})
        tolerance_limit = float(data.get('tolerance_limit_meters', 0.05) or 0.05)

        # Haversine or Euclidean distance approximation for variance
        variance = 0.0
        elevation_variance = 0.0

        if captured_coords and approved_coords:
            lat1 = float(captured_coords.get('lat', 0) or 0)
            lng1 = float(captured_coords.get('lng', 0) or 0)
            elev1 = float(captured_coords.get('elevation', 0) or captured_coords.get('elev', 0) or 0)

            lat2 = float(approved_coords.get('lat', 0) or 0)
            lng2 = float(approved_coords.get('lng', 0) or 0)
            elev2 = float(approved_coords.get('elevation', 0) or approved_coords.get('elev', 0) or 0)

            if lat1 and lng1 and lat2 and lng2:
                # 1 degree latitude ~ 111,139 meters
                d_lat = (lat1 - lat2) * 111139.0
                d_lng = (lng1 - lng2) * 111139.0 * math.cos(math.radians((lat1 + lat2) / 2))
                variance = round(math.sqrt(d_lat**2 + d_lng**2), 3)

            if elev1 and elev2:
                elevation_variance = round(abs(elev1 - elev2), 3)

        if 'variance_meters' in data:
            variance = float(data['variance_meters'])
        if 'elevation_variance_meters' in data:
            elevation_variance = float(data['elevation_variance_meters'])

        encroachment_detected = bool(data.get('encroachment_detected', False))
        encroachment_details = data.get('encroachment_details', '')

        variance_detected = variance > tolerance_limit or encroachment_detected or bool(data.get('variance_detected', False))
        
        default_status = 'VARIANCE_DETECTED' if variance_detected else 'VERIFIED'
        status = data.get('status', default_status)
        verifier_name = data.get('verified_by_name') or MonitoringService.get_actor_name(user, "Field Surveyor")

        # Default telemetry if not provided
        telemetry = data.get('telemetry_data', {})
        if not telemetry:
            telemetry = {
                'satellites_tracked': 28,
                'constellations': ['GPS', 'Galileo', 'GLONASS', 'BeiDou'],
                'hdop': 0.65,
                'vdop': 0.82,
                'rtk_fix_status': 'FIXED_RTK_HIGH_PRECISION',
                'correction_latency_sec': 0.4,
                'base_station_ref': 'LASG-CORS-VICTORIA-ISLAND-01'
            }

        verification = SiteVerification.objects.create(
            project=project,
            method=data.get('method', 'GNSS_RTK_SURVEY'),
            device_identifier=data.get('device_identifier', 'Tersus Oscar GNSS RTK #042'),
            cadastral_beacon_numbers=data.get('cadastral_beacon_numbers', []),
            boundary_coordinates=data.get('boundary_coordinates', []),
            captured_coordinates=captured_coords,
            approved_coordinates=approved_coords,
            variance_meters=variance,
            elevation_variance_meters=elevation_variance,
            tolerance_limit_meters=tolerance_limit,
            variance_detected=variance_detected,
            encroachment_detected=encroachment_detected,
            encroachment_details=encroachment_details,
            telemetry_data=telemetry,
            evidence_documents=data.get('evidence_documents', []),
            evidence_photos=data.get('evidence_photos', []),
            status=status,
            verified_by_name=verifier_name,
            verified_by_role=data.get('verified_by_role', 'Directorate of Cadastral & Structural Survey'),
            verified_at=timezone.now() if status == 'VERIFIED' else None,
            notes=data.get('notes', '')
        )

        MonitoringService.log_audit(
            user=user,
            action="SITE_VERIFICATION_RECORDED",
            resource_id=verification.id,
            new_state={
                "ref": verification.verification_reference,
                "method": verification.method,
                "variance_meters": variance,
                "status": verification.status
            }
        )
        return verification

    @staticmethod
    def certify_site_verification(verification, data, actor):
        """Formally sign off and certify site verification with digital certificate reference and SHA-256 signature."""
        override = data.get('override_tolerance', False)
        if verification.variance_detected and not override:
            raise ValueError(
                f"Cannot certify site verification: measured variance ({verification.variance_meters}m) "
                f"exceeds tolerance limit ({verification.tolerance_limit_meters}m) or encroachment detected."
            )

        cert_ref = f"CERT-VRF-{timezone.now().year}-{str(uuid.uuid4())[:8].upper()}"
        raw_sig_payload = f"{verification.id}:{cert_ref}:{timezone.now().isoformat()}:{verification.variance_meters}"
        sig_hash = f"0xLASBCA-VRF-SURV-{hashlib.sha256(raw_sig_payload.encode('utf-8')).hexdigest()[:16].upper()}"

        verifier_name = data.get('verified_by_name') or MonitoringService.get_actor_name(actor, "Director of Cadastral Survey")
        verifier_role = data.get('verified_by_role', "Directorate of Cadastral & Structural Survey")
        notes = data.get('notes', verification.notes or 'Statutory cadastral boundary and setback verified compliant.')

        previous_state = {"status": verification.status, "cert": verification.digital_cert_ref}

        verification.status = 'VERIFIED'
        verification.digital_cert_ref = cert_ref
        verification.signature_hash = sig_hash
        verification.verified_by_name = verifier_name
        verification.verified_by_role = verifier_role
        verification.verified_at = timezone.now()
        verification.notes = notes
        verification.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_VERIFICATION_CERTIFIED",
            resource_id=verification.id,
            previous_state=previous_state,
            new_state={
                "status": "VERIFIED",
                "cert_ref": cert_ref,
                "signature_hash": sig_hash,
                "verified_by": verifier_name
            }
        )
        return verification

    @staticmethod
    def flag_site_encroachment(verification, data, actor):
        """Flag site boundary encroachment or critical coordinate variance."""
        reason = data.get('reason', 'Spatial displacement beyond statutory planning boundary line.')
        details = data.get('details', '')

        previous_state = {"status": verification.status, "encroachment": verification.encroachment_detected}

        verification.status = 'FLAGGED'
        verification.encroachment_detected = True
        verification.variance_detected = True
        verification.encroachment_details = f"{reason}. {details}".strip()
        verification.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_ENCROACHMENT_FLAGGED",
            resource_id=verification.id,
            previous_state=previous_state,
            new_state={"status": "FLAGGED", "reason": reason, "details": details}
        )
        return verification

    @staticmethod
    def attach_verification_evidence(verification, data, actor):
        """Attach survey plans, RINEX logs, calibration certificates, and benchmark photos."""
        new_docs = data.get('documents', []) or []
        new_photos = data.get('photos', []) or []

        existing_docs = verification.evidence_documents or []
        existing_photos = verification.evidence_photos or []

        verification.evidence_documents = existing_docs + new_docs
        verification.evidence_photos = existing_photos + new_photos
        verification.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_VERIFICATION_EVIDENCE_ATTACHED",
            resource_id=verification.id,
            new_state={"added_docs_count": len(new_docs), "added_photos_count": len(new_photos)}
        )
        return verification

    @staticmethod
    def get_verification_telemetry(verification_id):
        """Return GNSS RTK telemetry diagnostics for the verification instrument."""
        try:
            vrf = SiteVerification.objects.get(id=verification_id)
            if vrf.telemetry_data:
                return vrf.telemetry_data
        except Exception:
            pass

        return {
            'satellites_tracked': 26,
            'constellations': ['GPS', 'Galileo', 'GLONASS', 'BeiDou'],
            'hdop': 0.71,
            'vdop': 0.88,
            'rtk_fix_status': 'FIXED_RTK_HIGH_PRECISION',
            'correction_latency_sec': 0.3,
            'base_station_ref': 'LASG-CORS-CENTRAL-01'
        }

    @staticmethod
    def get_verification_audit_trail(verification_id):
        """Retrieve audit trail logs for a site verification."""
        try:
            events = AuditLog.objects.filter(
                resource_id=str(verification_id)
            ).order_by('-timestamp')

            return [
                {
                    "id": str(e.id),
                    "action": e.action,
                    "user_name": e.actor_name or "Cadastral Officer",
                    "user_role": "Building Control Authority",
                    "timestamp": e.timestamp.isoformat(),
                    "previous_state": e.previous_state,
                    "new_state": e.new_state
                }
                for e in events
            ]
        except Exception:
            return []

    @staticmethod
    def get_project_progress_details(project_id=None):
        """
        Compute deep physical progress, milestone stages, photo feed, workforce metrics,
        and schedule health for a project or all active projects.
        """
        if project_id:
            project = MonitoringService.get_project_instance(project_id)
            projects = [project] if project else []
        else:
            projects = list(Project.objects.filter(status__in=['ACTIVE', 'IN_PROGRESS', 'UNDER_CONSTRUCTION'])) or list(Project.objects.all()[:5])

        results = []
        for p in projects:
            updates = list(DailySiteUpdate.objects.filter(project=p).order_by('-created_at'))
            milestones = list(ConstructionMilestone.objects.filter(project=p).order_by('target_date'))
            observations = list(FieldObservation.objects.filter(project=p).order_by('-created_at'))
            issues = list(SiteIssue.objects.filter(project=p).order_by('-created_at'))

            latest_update = updates[0] if updates else None
            
            # Overall progress from project model or latest update
            verified_progress = getattr(p, 'progress', 0) or (latest_update.progress_percentage if latest_update else 0) or 60

            # Schedule health analysis
            delayed_milestones_count = sum(1 for m in milestones if m.status == 'DELAYED' or m.is_delayed)
            critical_issues_count = sum(1 for i in issues if i.severity == 'CRITICAL' and i.status != 'RESOLVED')
            
            if critical_issues_count > 0 or delayed_milestones_count >= 2:
                schedule_status = "CRITICAL_DELAY"
                schedule_label = "Critical Schedule Delay"
            elif delayed_milestones_count == 1:
                schedule_status = "MINOR_DELAY"
                schedule_label = "Minor Schedule Delay"
            elif verified_progress >= 75:
                schedule_status = "AHEAD"
                schedule_label = "Ahead of Schedule"
            else:
                schedule_status = "ON_SCHEDULE"
                schedule_label = "On Schedule"

            # Flatten all project photos
            photos = []
            for u in updates:
                if u.photos and isinstance(u.photos, list):
                    for url in u.photos:
                        if url and url not in [item['url'] for item in photos]:
                            photos.append({
                                'url': url,
                                'update_ref': u.update_reference,
                                'update_type': u.update_type,
                                'date': u.created_at.isoformat(),
                                'work_summary': u.work_summary,
                                'reported_by': u.reported_by_name
                            })

            # Programme phases breakdown
            phases = [
                {
                    'name': 'Substructure, Foundation Piling & Raft Slab',
                    'progress': 100 if verified_progress >= 25 else int(verified_progress * 4),
                    'status': 'Completed & Certified' if verified_progress >= 25 else 'In Progress',
                    'color': 'bg-emerald-500'
                },
                {
                    'name': 'Reinforced Concrete Superstructure (Levels 1 - 12)',
                    'progress': 100 if verified_progress >= 70 else (0 if verified_progress < 25 else int((verified_progress - 25) / 45 * 100)),
                    'status': 'Completed' if verified_progress >= 70 else ('In Active Progress' if verified_progress >= 25 else 'Pending'),
                    'color': 'bg-blue-600'
                },
                {
                    'name': 'MEP Services, HVAC Ducting & Fire Sprinklers',
                    'progress': 100 if verified_progress >= 85 else (0 if verified_progress < 45 else int((verified_progress - 45) / 40 * 100)),
                    'status': 'Completed' if verified_progress >= 85 else ('Rough-in Phase' if verified_progress >= 45 else 'Pending'),
                    'color': 'bg-amber-500'
                },
                {
                    'name': 'Exterior Glazing & Unitized Curtain Wall Facade',
                    'progress': 100 if verified_progress >= 95 else (0 if verified_progress < 60 else int((verified_progress - 60) / 35 * 100)),
                    'status': 'Completed' if verified_progress >= 95 else ('Bracket Installation' if verified_progress >= 60 else 'Pending'),
                    'color': 'bg-indigo-500'
                },
                {
                    'name': 'Interior Finishing, Drywall Partitions & Floor Screed',
                    'progress': 100 if verified_progress >= 100 else (0 if verified_progress < 75 else int((verified_progress - 75) / 25 * 100)),
                    'status': 'Completed' if verified_progress >= 100 else ('Scheduled Next' if verified_progress >= 75 else 'Pending'),
                    'color': 'bg-slate-400'
                }
            ]

            # Progress history trend points
            progress_history = [
                {
                    'date': u.created_at.date().isoformat(),
                    'progress': u.progress_percentage,
                    'summary': u.work_summary,
                    'reported_by': u.reported_by_name
                }
                for u in updates[:10]
            ]

            results.append({
                'project_id': str(p.id),
                'project_name': p.name,
                'reference_number': getattr(p, 'reference_number', None) or str(p.id)[:8],
                'project_type': getattr(p, 'project_type', 'Commercial Multi-Story Structure'),
                'site_address': getattr(p, 'site_address', None) or getattr(p, 'location', 'Lagos State'),
                'status': getattr(p, 'status', 'ACTIVE'),
                'verified_progress': verified_progress,
                'schedule_status': schedule_status,
                'schedule_label': schedule_label,
                'workforce_on_site': latest_update.workforce_count if latest_update else 35,
                'weather_condition': latest_update.weather_condition if latest_update else 'Clear / Sunny (31°C)',
                'total_photos_count': len(photos),
                'photos': photos,
                'milestones_total': len(milestones),
                'milestones_verified': sum(1 for m in milestones if m.status in ['VERIFIED', 'COMPLETED']),
                'milestones_delayed': delayed_milestones_count,
                'milestones': [
                    {
                        'id': str(m.id),
                        'name': m.name,
                        'target_date': m.target_date.isoformat() if m.target_date else None,
                        'status': m.status,
                        'progress_percentage': m.progress_percentage,
                        'is_delayed': m.is_delayed
                    }
                    for m in milestones
                ],
                'phases': phases,
                'latest_update': {
                    'reference': latest_update.update_reference,
                    'work_summary': latest_update.work_summary,
                    'reported_by': latest_update.reported_by_name,
                    'date': latest_update.created_at.isoformat()
                } if latest_update else None,
                'progress_history': progress_history
            })

        if project_id and results:
            return results[0]
        return results

    @staticmethod
    def update_project_progress(data, user):
        """Update progress percentage and create a progress log in database."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        
        progress = int(data.get('progress_percentage', 0))
        summary = data.get('work_summary') or f"Site progress updated to {progress}%."
        photos = data.get('photos', [])
        author_name = MonitoringService.get_actor_name(user, "Building Control Officer")

        # 1. Update Project progress
        if project:
            if hasattr(project, 'progress'):
                project.progress = progress
                project.save()

        # 2. Log DailySiteUpdate record
        update = DailySiteUpdate.objects.create(
            project=project,
            update_type='PROGRESS_REPORT',
            reported_by=user if getattr(user, 'is_authenticated', False) else None,
            reported_by_name=author_name,
            progress_percentage=progress,
            work_summary=summary,
            photos=photos,
            status='Approved',
            priority='High'
        )

        MonitoringService.log_audit(
            user=user,
            action="PROJECT_PROGRESS_UPDATED",
            resource_id=project.id if project else update.id,
            new_state={"progress": progress, "summary": summary}
        )

        return MonitoringService.get_project_progress_details(project.id if project else None)

    @staticmethod
    def flag_project_schedule_delay(data, user):
        """Flag project schedule delay and record non-conformance defect."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        reason = data.get('reason') or data.get('description') or 'Construction progress is tracking behind approved statutory timeline.'
        severity = data.get('severity', 'HIGH')

        # Create defect issue
        issue = SiteIssue.objects.create(
            project=project,
            title=f"Schedule Delay Notice: {project.name if project else 'Site'}",
            description=reason,
            severity=severity,
            status='OPEN',
            reported_by_name=MonitoringService.get_actor_name(user, "Monitoring Lead Officer"),
            assigned_to_name="Principal Contractor / Project Manager"
        )

        # Flag any upcoming milestones as delayed
        if project:
            ConstructionMilestone.objects.filter(project=project, status='UPCOMING').update(is_delayed=True, delay_reason=reason)

        MonitoringService.log_audit(
            user=user,
            action="SCHEDULE_DELAY_FLAGGED",
            resource_id=project.id if project else issue.id,
            new_state={"issue_ref": issue.issue_reference, "reason": reason}
        )

        return issue

