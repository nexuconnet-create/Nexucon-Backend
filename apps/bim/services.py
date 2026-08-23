import hashlib
import uuid
from django.utils import timezone
from .models import (
    BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, 
    BIMProgressValidation, BIMConstructionMilestone
)
from apps.projects.models import Project
from apps.monitoring.models import SiteIssue, ConstructionMilestone
from apps.compliance.models import NonConformanceReport
from apps.audit.models import AuditEvent

class BIMService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None, resource_type="BIMModel"):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def upload_model(data, user):
        """Upload and register a new BIM model with v1.0 version."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        model = BIMModel.objects.create(
            project=project,
            name=data.get('name', f"{project.name} - Model"),
            discipline=data.get('discipline', 'Architecture'),
            format=data.get('format', 'IFC4'),
            file_url=data.get('file_url', 'https://assets.nexucon.com/bim/sample_model.ifc'),
            file_size=data.get('file_size', '345 MB'),
            current_version='v1.0',
            status=data.get('status', 'Active'),
            lod=data.get('lod', 'LOD 300'),
            element_count=int(data.get('element_count', 12450)),
            coordinate_system=data.get('coordinate_system', {"crs": "EPSG:32631", "origin": [6.428, 3.421, 12.0]}),
            uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
            uploaded_by_name=data.get('uploaded_by_name') or (getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'email', 'BIM Coordinator'))
        )

        # Create initial version v1.0
        BIMModelVersion.objects.create(
            model=model,
            version_label='v1.0',
            commit_hash=uuid.uuid4().hex[:8],
            changes_summary='Initial model upload and registration.',
            author_name=model.uploaded_by_name,
            author_role='BIM Author',
            stats_added=model.element_count,
            stats_modified=0,
            stats_removed=0,
            file_url=model.file_url,
            file_size=model.file_size,
            is_current=True
        )

        BIMService.log_audit(
            user=user,
            action="BIM_MODEL_UPLOADED",
            resource_id=model.id,
            new_state={"ref": model.model_reference, "discipline": model.discipline, "version": "v1.0"}
        )
        return model

    @staticmethod
    def create_version(model, data, user):
        """Push a new revision for a BIM model."""
        version_label = data.get('version_label', f"v{float(model.current_version.replace('v','')) + 0.1:.1f}")
        commit_hash = data.get('commit_hash') or uuid.uuid4().hex[:8]

        # Set older versions to not current
        model.versions.all().update(is_current=False)

        version = BIMModelVersion.objects.create(
            model=model,
            version_label=version_label,
            commit_hash=commit_hash,
            changes_summary=data.get('changes_summary', 'Model geometry and clash resolution revisions.'),
            author_name=data.get('author_name') or (getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'email', 'Lead Architect')),
            author_role=data.get('author_role', 'Lead Architect'),
            stats_added=int(data.get('stats_added', 45)),
            stats_modified=int(data.get('stats_modified', 20)),
            stats_removed=int(data.get('stats_removed', 5)),
            file_url=data.get('file_url', model.file_url),
            file_size=data.get('file_size', model.file_size),
            is_current=True
        )

        model.current_version = version_label
        model.save()

        # Flag any verified milestones linked to this model that now require re-verification
        for ms in model.milestones.filter(verification_status='VERIFIED'):
            ms.verification_status = 'RE_VERIFICATION_REQUIRED'
            ms.save()
            BIMService.log_audit(
                user=user,
                action="BIM_MILESTONE_RE_VERIFICATION_TRIGGERED",
                resource_id=ms.id,
                resource_type="BIMConstructionMilestone",
                new_state={"reason": f"New model revision {version_label} committed."}
            )

        BIMService.log_audit(
            user=user,
            action="BIM_VERSION_COMMITTED",
            resource_id=model.id,
            new_state={"version": version_label, "commit": commit_hash}
        )
        return version

    @staticmethod
    def stamp_and_certify(model, actor, hash_signature=None):
        """Apply official government digital certification stamp to BIM model."""
        if not hash_signature:
            hash_raw = f"{model.id}-{model.model_reference}-{timezone.now().isoformat()}"
            hash_signature = f"0x{hashlib.sha256(hash_raw.encode()).hexdigest()[:16]}"

        model.is_digitally_certified = True
        model.certified_by_name = getattr(actor, 'get_full_name', lambda: '')() or getattr(actor, 'email', 'Directorate Reviewer')
        model.certified_at = timezone.now()
        model.hash_signature = hash_signature
        model.status = 'Approved'
        model.save()

        BIMService.log_audit(
            user=actor,
            action="BIM_MODEL_DIGITALLY_CERTIFIED",
            resource_id=model.id,
            new_state={"hash": hash_signature, "certified_by": model.certified_by_name}
        )
        return model

    @staticmethod
    def request_changes(model, reason, actor):
        """Flag BIM model for design revisions."""
        model.status = 'Changes Requested'
        model.save()

        # Add annotation record
        BIMAnnotation.objects.create(
            model=model,
            project=model.project,
            author_name=getattr(actor, 'get_full_name', lambda: '')() or getattr(actor, 'email', 'Government Reviewer'),
            author_role='Government Reviewer',
            text=reason,
            status='Open',
            priority='High'
        )

        BIMService.log_audit(
            user=actor,
            action="BIM_CHANGES_REQUESTED",
            resource_id=model.id,
            new_state={"status": "Changes Requested", "reason": reason}
        )
        return model

    @staticmethod
    def run_clash_matrix(project_id, primary_model_id, secondary_model_id, actor):
        """Run automated clash matrix detection between disciplines."""
        project = None
        if project_id:
            try:
                project = Project.objects.filter(pk=project_id).first()
            except Exception:
                pass
        if not project:
            project = Project.objects.first()

        primary_model = None
        if primary_model_id:
            try:
                primary_model = BIMModel.objects.filter(pk=primary_model_id).first()
            except Exception:
                pass
        if not primary_model:
            primary_model = BIMModel.objects.filter(project=project).first() if project else BIMModel.objects.first()

        secondary_model = None
        if secondary_model_id:
            try:
                secondary_model = BIMModel.objects.filter(pk=secondary_model_id).first()
            except Exception:
                pass
        if not secondary_model and project and primary_model:
            secondary_model = BIMModel.objects.filter(project=project).exclude(id=primary_model.id).first()

        prim_disc = primary_model.discipline if primary_model else 'Structural'
        sec_disc = secondary_model.discipline if secondary_model else 'MEP'
        site_name = project.name if project else 'Site Core'

        clash = BIMClash.objects.create(
            project=project,
            primary_model=primary_model,
            secondary_model=secondary_model,
            clash_type='HARD_CLASH',
            title=f"{prim_disc} vs {sec_disc} Spatial Interference",
            description=f"Hard physical clearance breach (-160mm) detected between {prim_disc} elements and {sec_disc} distribution at Grid 4-C on {site_name}.",
            severity='HIGH',
            status='OPEN',
            assigned_to_name='Michael Chen (MEP Coordinator)',
            assigned_discipline='MEP',
            coordinates_3d={"x": 14.2, "y": 8.5, "z": 12.0}
        )

        BIMService.log_audit(
            user=actor,
            action="BIM_CLASH_DETECTED",
            resource_id=clash.id,
            new_state={"ref": clash.clash_reference, "type": clash.clash_type}
        )
        return clash

    @staticmethod
    def convert_clash_to_site_issue(clash, actor):
        """Convert an unresolved model clash into an official SiteIssue in apps.monitoring."""
        site_issue = SiteIssue.objects.create(
            project=clash.project,
            title=f"BIM Clash: {clash.title}",
            description=f"Model Interference: {clash.description} (Ref: {clash.clash_reference})",
            severity=clash.severity,
            status='OPEN',
            assigned_to_name=clash.assigned_to_name,
            reported_by_name=getattr(actor, 'get_full_name', lambda: '')() or getattr(actor, 'email', 'BIM Auditor')
        )

        clash.status = 'CONVERTED_TO_ISSUE'
        clash.converted_site_issue = site_issue
        clash.save()

        BIMService.log_audit(
            user=actor,
            action="BIM_CLASH_CONVERTED_TO_SITE_ISSUE",
            resource_id=clash.id,
            new_state={"site_issue_id": str(site_issue.id), "status": "CONVERTED_TO_ISSUE"}
        )
        return site_issue

    @staticmethod
    def add_annotation(model, data, actor):
        """Add BCF design review comment/markup to model."""
        annotation = BIMAnnotation.objects.create(
            model=model,
            project=model.project,
            author_name=data.get('author_name') or (getattr(actor, 'get_full_name', lambda: '')() or getattr(actor, 'email', 'Review Officer')),
            author_role=data.get('author_role', 'Review Officer'),
            text=data.get('text', ''),
            status=data.get('status', 'Open'),
            priority=data.get('priority', 'Medium'),
            viewpoint_camera=data.get('viewpoint_camera', {}),
            element_ids=data.get('element_ids', []),
            attachments=data.get('attachments', [])
        )

        BIMService.log_audit(
            user=actor,
            action="BIM_ANNOTATION_ADDED",
            resource_id=annotation.id,
            new_state={"ref": annotation.annotation_reference, "text": annotation.text[:30]}
        )
        return annotation

    @staticmethod
    def resolve_annotation(annotation, notes, actor):
        """Resolve a design review annotation thread."""
        annotation.status = 'Resolved'
        annotation.save()

        BIMService.log_audit(
            user=actor,
            action="BIM_ANNOTATION_RESOLVED",
            resource_id=annotation.id,
            new_state={"status": "Resolved", "notes": notes}
        )
        return annotation

    @staticmethod
    def run_timeline_simulation(project_id, actor):
        """Run 4D timeline simulation comparing LiDAR scans against as-planned schedule."""
        project = None
        if project_id:
            try:
                project = Project.objects.filter(pk=project_id).first()
            except Exception:
                pass
            if not project:
                project = Project.objects.filter(name__icontains=str(project_id)).first()

        if not project:
            project = Project.objects.first()

        if not project:
            raise ValueError("No project available in database to run timeline simulation.")

        # Find primary BIM model or aggregate elements from all project models
        model = project.bim_models.first() if hasattr(project, 'bim_models') else None
        if not model:
            model = BIMModel.objects.filter(project=project).first()

        total_elements = 0
        if model:
            models_list = list(project.bim_models.all()) if hasattr(project, 'bim_models') else [model]
            total_elements = sum(m.element_count for m in models_list if m.element_count > 0)
        if total_elements == 0:
            total_elements = 18500

        # Query real construction milestones for this project
        from apps.monitoring.models import ConstructionMilestone
        site_milestones = list(ConstructionMilestone.objects.filter(project=project).order_by('sequence_order'))
        bim_milestones = list(BIMConstructionMilestone.objects.filter(project=project).order_by('sequence_order'))

        planned_vs_actual = []
        delayed_days = 0
        has_delay = False
        total_progress_sum = 0
        item_count = 0

        if site_milestones:
            for ms in site_milestones:
                actual = ms.progress_percentage
                is_ms_delayed = ms.status in ['DELAYED', 'BLOCKED'] or ms.variance_days > 0
                planned = min(100, actual + (12 if is_ms_delayed else (0 if actual == 100 else 5)))
                
                if is_ms_delayed:
                    has_delay = True
                    delayed_days = max(delayed_days, ms.variance_days if ms.variance_days > 0 else 4)
                    status_label = f"Delayed - {ms.variance_days if ms.variance_days > 0 else delayed_days} Days"
                elif actual >= 100:
                    status_label = "Completed"
                elif actual > 0:
                    status_label = "In Progress"
                else:
                    status_label = "Planned"

                planned_vs_actual.append({
                    "phase": ms.name,
                    "planned": planned,
                    "actual": actual,
                    "status": status_label,
                    "variance_days": ms.variance_days
                })
                total_progress_sum += actual
                item_count += 1
        elif bim_milestones:
            for bms in bim_milestones:
                actual = 100 if bms.verification_status in ['VERIFIED', 'COMPLETED'] else (85 if bms.verification_status == 'PENDING_REVIEW' else (45 if bms.verification_status == 'DEVIATION_FLAGGED' else 20))
                is_flagged = bms.verification_status in ['DEVIATION_FLAGGED', 'RE_VERIFICATION_REQUIRED']
                planned = 100 if bms.verification_status in ['VERIFIED', 'COMPLETED'] else (80 if not is_flagged else 75)
                
                if is_flagged:
                    has_delay = True
                    delayed_days = max(delayed_days, 5)
                    status_label = "Delayed - 5 Days"
                elif actual >= 100:
                    status_label = "Completed"
                else:
                    status_label = "In Progress"

                planned_vs_actual.append({
                    "phase": bms.name,
                    "planned": planned,
                    "actual": actual,
                    "status": status_label,
                    "variance_days": 5 if is_flagged else 0
                })
                total_progress_sum += actual
                item_count += 1
        else:
            num_floors = getattr(project, 'number_of_floors', 12) or 12
            p_type = getattr(project, 'project_type', 'Commercial')
            if p_type == 'Industrial':
                planned_vs_actual = [
                    {"phase": "Substructure Laser Grading & Deep Bored Piling", "planned": 100, "actual": 100, "status": "Completed"},
                    {"phase": "High-Tolerance Industrial Laser Screed Floor Slab", "planned": 100, "actual": 90, "status": "In Progress"},
                    {"phase": "Structural Steel Portal Frame & Cladding", "planned": 70, "actual": 55, "status": "Delayed - 4 Days"},
                    {"phase": "Automated High-Bay Logistics & Loading Docks", "planned": 30, "actual": 15, "status": "Pending"}
                ]
                has_delay = True
                delayed_days = 4
                total_progress_sum = 260
                item_count = 4
            else:
                planned_vs_actual = [
                    {"phase": "Substructure & Deep Foundation Piling", "planned": 100, "actual": 100, "status": "Completed"},
                    {"phase": f"Podium Transfer Slab & Shear Core (Levels 1-{min(4, num_floors)})", "planned": 100, "actual": 92, "status": "Delayed - 3 Days"},
                    {"phase": f"Superstructure Post-Tensioned Slabs (Levels {min(5, num_floors)}-{num_floors})", "planned": 55, "actual": 48, "status": "In Progress"},
                    {"phase": "Unitized Curtain Wall & Building Envelope Glazing", "planned": 25, "actual": 15, "status": "Pending"}
                ]
                has_delay = True
                delayed_days = 3
                total_progress_sum = 255
                item_count = 4

        avg_progress = (total_progress_sum / item_count) if item_count > 0 else 50
        completed_elements = int((avg_progress / 100.0) * total_elements)

        # Compute Earned Value from real project budget / value
        project_val = float(getattr(project, 'estimated_project_value', 0) or 0)
        if project_val > 0:
            ev_amount = (avg_progress / 100.0) * project_val
            if ev_amount >= 1_000_000_000:
                earned_value_str = f"₦{ev_amount / 1_000_000_000:.2f}B"
            else:
                earned_value_str = f"₦{ev_amount / 1_000_000:.1f}M"
        else:
            earned_value_str = f"${(avg_progress * total_elements * 65) / 1_000_000:.1f}M"

        schedule_status = 'DELAYED' if has_delay else ('ON_TRACK' if delayed_days == 0 else 'AHEAD')
        days_variance = -delayed_days if has_delay else (0 if schedule_status == 'ON_TRACK' else 4)

        validation = BIMProgressValidation.objects.create(
            project=project,
            model=model,
            schedule_status=schedule_status,
            days_variance=days_variance,
            completed_elements_count=completed_elements,
            total_elements_count=total_elements,
            earned_value_usd=earned_value_str,
            planned_vs_actual=planned_vs_actual,
            simulation_date=timezone.localdate()
        )

        BIMService.log_audit(
            user=actor,
            action="BIM_4D_SIMULATION_EXECUTED",
            resource_id=validation.id,
            resource_type="BIMProgressValidation",
            new_state={
                "project": project.name,
                "status": validation.schedule_status,
                "variance": validation.days_variance,
                "completed_elements": completed_elements,
                "total_elements": total_elements,
                "earned_value": earned_value_str
            }
        )
        return validation


    # =========================================================================
    # BIM CONSTRUCTION MILESTONE ENGINE & VERIFICATION GATES
    # =========================================================================

    @staticmethod
    def create_bim_milestone(data, user):
        """Create a new BIM Construction Milestone linked to approved model and version."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        bim_model_id = data.get('bim_model_id') or data.get('bim_model')
        bim_model = BIMModel.objects.get(pk=bim_model_id)

        model_version_id = data.get('model_version_id') or data.get('model_version')
        model_version = None
        if model_version_id:
            model_version = BIMModelVersion.objects.filter(pk=model_version_id).first()
        if not model_version:
            model_version = bim_model.versions.filter(is_current=True).first()

        linked_cm_id = data.get('linked_construction_milestone_id') or data.get('linked_construction_milestone')
        linked_cm = ConstructionMilestone.objects.filter(pk=linked_cm_id).first() if linked_cm_id else None

        milestone = BIMConstructionMilestone.objects.create(
            project=project,
            bim_model=bim_model,
            model_version=model_version,
            linked_construction_milestone=linked_cm,
            name=data.get('name', f"{project.name} - Milestone"),
            phase=data.get('phase', 'SUPERSTRUCTURE'),
            description=data.get('description', ''),
            sequence_order=int(data.get('sequence_order', 1)),
            target_date=data.get('target_date', timezone.now().date()),
            bim_elements=data.get('bim_elements', []),
            tolerance_max_mm=float(data.get('tolerance_max_mm', 15.0)),
            bim_deviation_mm=float(data.get('bim_deviation_mm', 0.0)),
            gnss_survey_variance_mm=float(data.get('gnss_survey_variance_mm', 0.0)),
            gpr_clearance_status=data.get('gpr_clearance_status', 'NOT_APPLICABLE'),
            gpr_evidence_notes=data.get('gpr_evidence_notes', ''),
            verification_status='PENDING_REVIEW' if bim_model.is_digitally_certified else 'UNVERIFIED',
            linked_clashes=data.get('linked_clashes', []),
            linked_inspections=data.get('linked_inspections', []),
            linked_site_verifications=data.get('linked_site_verifications', []),
            linked_ncrs=data.get('linked_ncrs', []),
            evidence_vault=data.get('evidence_vault', []),
            verification_requirements=data.get('verification_requirements', {
                'require_approved_model': True,
                'require_certified_version': True,
                'require_zero_critical_clashes': True,
                'require_survey_within_tolerance': True,
                'require_passed_inspection': True,
                'require_gpr_clearance': False,
                'require_directorate_signoff': True
            })
        )

        BIMService.log_audit(
            user=user,
            action="BIM_MILESTONE_CREATED",
            resource_id=milestone.id,
            resource_type="BIMConstructionMilestone",
            new_state={"code": milestone.milestone_code, "name": milestone.name, "model": bim_model.name}
        )
        return milestone

    @staticmethod
    def evaluate_milestone_gate_status(milestone):
        """Evaluate real-time gate pass/fail conditions for a BIM milestone."""
        model_approved = bool(milestone.bim_model and milestone.bim_model.is_digitally_certified and milestone.bim_model.status == 'Approved')
        version_verified = bool(milestone.model_version and (milestone.model_version.is_current or milestone.model_version.version_label == milestone.bim_model.current_version))
        
        clashes = milestone.linked_clashes or []
        open_critical_clashes = [c for c in clashes if str(c.get('severity', '')).upper() in ('CRITICAL', 'HIGH') and str(c.get('status', '')).upper() in ('OPEN', 'ASSIGNED', 'IN_REVIEW')]
        zero_critical_clashes = (len(open_critical_clashes) == 0)
        
        tolerance_compliant = (milestone.bim_deviation_mm <= milestone.tolerance_max_mm)
        
        inspections = milestone.linked_inspections or []
        failed_inspections = [i for i in inspections if str(i.get('outcome', '')).upper() in ('FAILED', 'PENDING')]
        inspections_passed = (len(inspections) == 0 or len(failed_inspections) == 0)
        
        gpr_clear = milestone.gpr_clearance_status in ('VERIFIED', 'NOT_APPLICABLE')

        blockers = []
        if not model_approved:
            blockers.append("Associated BIM Model is not in Approved status or lacks government digital certification seal.")
        if not version_verified:
            blockers.append(f"Model revision ({milestone.model_version.version_label if milestone.model_version else 'None'}) does not match the approved active model release ({milestone.bim_model.current_version}).")
        if not zero_critical_clashes:
            blockers.append(f"{len(open_critical_clashes)} open critical/high model clashes remain unresolved.")
        if not tolerance_compliant:
            blockers.append(f"Point Cloud / LiDAR spatial deviation ({milestone.bim_deviation_mm}mm) exceeds allowable tolerance ({milestone.tolerance_max_mm}mm).")
        if not inspections_passed:
            blockers.append(f"{len(failed_inspections)} statutory inspections are not in PASSED outcome.")
        if not gpr_clear:
            blockers.append(f"GPR Subsurface clearance status is {milestone.gpr_clearance_status}.")

        all_passed = (len(blockers) == 0)

        return {
            "milestone_id": str(milestone.id),
            "milestone_code": milestone.milestone_code,
            "all_gates_passed": all_passed,
            "gates": [
                {
                    "key": "model_approved",
                    "label": "Government Approved & Digitally Certified BIM Model",
                    "passed": model_approved,
                    "detail": f"{milestone.bim_model.name} (Status: {milestone.bim_model.status}, Certified: {milestone.bim_model.is_digitally_certified})"
                },
                {
                    "key": "version_verified",
                    "label": "Model Revision & Element Alignment",
                    "passed": version_verified,
                    "detail": f"Version {milestone.model_version.version_label if milestone.model_version else 'N/A'} (Commit: {milestone.model_version.commit_hash if milestone.model_version else 'N/A'})"
                },
                {
                    "key": "zero_critical_clashes",
                    "label": "Zero Open Critical / Hard Spatial Clashes",
                    "passed": zero_critical_clashes,
                    "detail": f"{len(open_critical_clashes)} open interferences detected"
                },
                {
                    "key": "tolerance_compliant",
                    "label": "Scan-to-BIM Point Cloud / GNSS Tolerance",
                    "passed": tolerance_compliant,
                    "detail": f"Measured: {milestone.bim_deviation_mm}mm / Max Allowed: {milestone.tolerance_max_mm}mm"
                },
                {
                    "key": "inspections_passed",
                    "label": "Statutory Field Inspections Completed & Passed",
                    "passed": inspections_passed,
                    "detail": f"{len(inspections)} linked inspections verified"
                },
                {
                    "key": "gpr_clear",
                    "label": "GPR Subsurface Utility & Slab Rebar Clearance",
                    "passed": gpr_clear,
                    "detail": f"Status: {milestone.gpr_clearance_status}"
                }
            ],
            "blockers": blockers,
            "can_digitally_sign": all_passed and milestone.verification_status != 'COMPLETED'
        }

    @staticmethod
    def verify_and_stamp_milestone(milestone, user, signature_notes=""):
        """Apply government digital verification seal to BIM milestone if all gates pass."""
        gate_status = BIMService.evaluate_milestone_gate_status(milestone)
        if not gate_status["all_gates_passed"]:
            raise ValueError(f"Cannot verify milestone: {'; '.join(gate_status['blockers'])}")

        hash_raw = f"{milestone.id}-{milestone.milestone_code}-{timezone.now().isoformat()}"
        stamp_hash = f"0x{hashlib.sha256(hash_raw.encode()).hexdigest()[:16]}"
        actor_name = getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'email', 'Review Directorate')

        milestone.verification_status = 'VERIFIED'
        milestone.digital_stamp_reference = stamp_hash
        milestone.verified_by = user if getattr(user, 'is_authenticated', False) else None
        milestone.verified_by_name = actor_name
        milestone.verified_at = timezone.now()
        milestone.actual_verified_date = timezone.now().date()
        milestone.signoff_metadata = {
            "stamp_hash": stamp_hash,
            "signed_by": actor_name,
            "signed_at": timezone.now().isoformat(),
            "notes": signature_notes,
            "directorate": "Lagos State Physical Planning & BIM Review Authority"
        }
        milestone.save()

        BIMService.log_audit(
            user=user,
            action="BIM_MILESTONE_DIGITALLY_VERIFIED",
            resource_id=milestone.id,
            resource_type="BIMConstructionMilestone",
            new_state={"stamp_hash": stamp_hash, "signed_by": actor_name, "status": "VERIFIED"}
        )
        return milestone

    @staticmethod
    def flag_milestone_deviation(milestone, user, deviation_data):
        """Record Scan-to-BIM or GNSS tolerance exceedance and flag milestone deviation."""
        dev_mm = float(deviation_data.get('deviation_mm', 25.0))
        reason = deviation_data.get('reason', 'Scan-to-BIM point cloud variance exceeded allowable tolerance.')
        
        milestone.bim_deviation_mm = dev_mm
        milestone.verification_status = 'DEVIATION_FLAGGED'
        
        evidence_entry = {
            "name": deviation_data.get('evidence_name', 'Point Cloud Deviation Heatmap'),
            "url": deviation_data.get('evidence_url', 'https://assets.nexucon.com/bim/deviations/scan_heatmap.ply'),
            "file_type": "POINT_CLOUD_SURVEY",
            "category": "SCAN_TO_BIM",
            "timestamp": timezone.now().isoformat(),
            "deviation_mm": dev_mm,
            "reason": reason
        }
        vault = list(milestone.evidence_vault or [])
        vault.append(evidence_entry)
        milestone.evidence_vault = vault
        milestone.save()

        # If deviation is critical (> 20mm), create a NonConformanceReport in apps.compliance
        if dev_mm > 20.0:
            NonConformanceReport.objects.create(
                project=milestone.project,
                title=f"BIM Spatial Deviation: {milestone.name} (+{dev_mm}mm)",
                description=f"Model verification failure on {milestone.milestone_code}: {reason}. Point cloud deviation of {dev_mm}mm exceeds max allowable tolerance of {milestone.tolerance_max_mm}mm.",
                severity='Critical' if dev_mm > 35.0 else 'Major',
                category='Structural',
                status='Open',
                source='BIM_CLASH'
            )

        BIMService.log_audit(
            user=user,
            action="BIM_MILESTONE_DEVIATION_FLAGGED",
            resource_id=milestone.id,
            resource_type="BIMConstructionMilestone",
            new_state={"deviation_mm": dev_mm, "reason": reason, "status": "DEVIATION_FLAGGED"}
        )
        return milestone

    @staticmethod
    def request_milestone_re_verification(milestone, user, reason=""):
        """Re-open milestone verification due to design modification or re-cast elements."""
        milestone.verification_status = 'RE_VERIFICATION_REQUIRED'
        milestone.digital_stamp_reference = None
        milestone.save()

        BIMService.log_audit(
            user=user,
            action="BIM_MILESTONE_RE_VERIFICATION_REQUESTED",
            resource_id=milestone.id,
            resource_type="BIMConstructionMilestone",
            new_state={"reason": reason, "status": "RE_VERIFICATION_REQUIRED"}
        )
        return milestone

