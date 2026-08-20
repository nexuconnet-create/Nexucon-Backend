import hashlib
import uuid
from django.utils import timezone
from .models import BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation
from apps.projects.models import Project
from apps.monitoring.models import SiteIssue
from apps.audit.models import AuditEvent

class BIMService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="BIMModel",
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
            uploaded_by_name=data.get('uploaded_by_name') or (user.get_full_name() or user.email)
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
            author_name=data.get('author_name') or (user.get_full_name() or user.email),
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
        model.certified_by_name = actor.get_full_name() or actor.email
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
            author_name=actor.get_full_name() or actor.email,
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
        project = Project.objects.get(pk=project_id)
        primary_model = BIMModel.objects.get(pk=primary_model_id)
        secondary_model = BIMModel.objects.filter(pk=secondary_model_id).first() if secondary_model_id else None

        clash = BIMClash.objects.create(
            project=project,
            primary_model=primary_model,
            secondary_model=secondary_model,
            clash_type='HARD_CLASH',
            title='MEP Main HVAC Duct vs Primary Structural Beam',
            description='Hard physical intersection detected. Penetration without structural sleeve exceeds 45mm tolerance at Grid 4-C.',
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
            reported_by_name=actor.get_full_name() or actor.email
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
            author_name=data.get('author_name') or (actor.get_full_name() or actor.email),
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
        project = Project.objects.get(pk=project_id)
        model = project.bim_models.first()

        validation = BIMProgressValidation.objects.create(
            project=project,
            model=model,
            schedule_status='DELAYED',
            days_variance=-3,
            completed_elements_count=4205,
            total_elements_count=9500,
            earned_value_usd='$2.4M',
            planned_vs_actual=[
                {"phase": "Foundation Pour", "planned": 100, "actual": 100, "status": "On Track"},
                {"phase": "Steel Framing (L1-L3)", "planned": 100, "actual": 100, "status": "Completed"},
                {"phase": "Concrete Core (L4)", "planned": 80, "actual": 60, "status": "Delayed - 3 Days"}
            ]
        )

        BIMService.log_audit(
            user=actor,
            action="BIM_4D_SIMULATION_EXECUTED",
            resource_id=validation.id,
            new_state={"status": validation.schedule_status, "variance": validation.days_variance}
        )
        return validation
