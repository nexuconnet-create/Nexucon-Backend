import hashlib
import uuid
from django.utils import timezone
from .models import Document, Version, Approval, DocumentTemplate, DocumentFolder
from apps.projects.models import Project
from apps.audit.models import AuditEvent

class DocumentService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Document",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def upload_document(data, user):
        """Upload and register a new project or regulatory document with v1.0 version."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        folder_name = data.get('folder', '01_Architectural')
        document = Document.objects.create(
            project=project,
            folder=folder_name,
            title=data.get('title', f"{project.name} - Document"),
            document_type=data.get('document_type', 'DRAWING'),
            discipline=data.get('discipline', 'Architecture'),
            status=data.get('status', 'APPROVED'),
            current_version='v1.0',
            file_url=data.get('file_url', 'https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf'),
            file_size=data.get('file_size', '12.4 MB'),
            file_format=data.get('file_format', 'PDF'),
            pages_count=int(data.get('pages_count', 12)),
            is_starred=bool(data.get('is_starred', False)),
            is_shared=bool(data.get('is_shared', True)),
            expiry_date=data.get('expiry_date'),
            uploader=user if getattr(user, 'is_authenticated', False) else None,
            uploader_name=data.get('uploader_name') or (user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'S. Jenkins')
        )

        # Create initial version v1.0
        Version.objects.create(
            document=document,
            version_number=1,
            version_label='v1.0',
            changes_summary='Initial document registration and submission.',
            author_name=document.uploader_name,
            author_role='Review Team',
            file_url=document.file_url,
            file_size=document.file_size,
            status='Current'
        )

        # Update or create folder record count
        folder_obj, _ = DocumentFolder.objects.get_or_create(name=folder_name, project=project)
        folder_obj.files_count = Document.objects.filter(folder=folder_name, project=project).count()
        folder_obj.save()

        DocumentService.log_audit(
            user=user,
            action="DOCUMENT_UPLOADED",
            resource_id=document.id,
            new_state={"ref": document.document_reference, "title": document.title, "folder": document.folder}
        )
        return document

    @staticmethod
    def create_version(document, data, user):
        """Push a new document revision."""
        version_label = data.get('version_label', f"v{document.versions.count() + 1}.0")
        
        # Mark older versions as superseded
        document.versions.all().update(status='Superseded')

        version = Version.objects.create(
            document=document,
            version_number=document.versions.count() + 1,
            version_label=version_label,
            changes_summary=data.get('changes_summary', 'Updated specifications and reviewed clauses.'),
            author_name=data.get('author_name') or (user.get_full_name() or user.email if getattr(user, 'is_authenticated', False) else 'Legal Team'),
            author_role=data.get('author_role', 'Review Team'),
            file_url=data.get('file_url', document.file_url),
            file_size=data.get('file_size', document.file_size),
            status='Current'
        )

        document.current_version = version_label
        document.save()

        DocumentService.log_audit(
            user=user,
            action="DOCUMENT_VERSION_PUSHED",
            resource_id=document.id,
            new_state={"version": version_label}
        )
        return version

    @staticmethod
    def apply_digital_signature_stamp(document, actor, comments=None):
        """Apply official Directorate digital signature stamp and lock into approval vault."""
        hash_raw = f"{document.id}-{document.document_reference}-{timezone.now().isoformat()}"
        signature_hash = f"0x3f8a{hashlib.sha256(hash_raw.encode()).hexdigest()[:12]}c91"
        stamp_ref = f"APP-DOC-{timezone.now().year}-{uuid.uuid4().hex[:4].upper()}"

        document.is_digitally_stamped = True
        document.stamped_by_name = actor.get_full_name() or actor.email if getattr(actor, 'is_authenticated', False) else 'Gov. Planning Comm.'
        document.stamped_at = timezone.now()
        document.stamp_reference = stamp_ref
        document.signature_hash = signature_hash
        document.status = 'APPROVED'
        document.save()

        approval = Approval.objects.create(
            approval_reference=stamp_ref,
            document=document,
            version=document.versions.filter(status='Current').first(),
            category=document.folder.replace('_', ' ').title(),
            approved_by_name=document.stamped_by_name,
            status='APPROVED',
            comments=comments or 'Officially reviewed, stamped, and approved by government regulatory board.',
            signature_hash=signature_hash
        )

        DocumentService.log_audit(
            user=actor,
            action="DOCUMENT_DIGITALLY_STAMPED",
            resource_id=document.id,
            new_state={"stamp_ref": stamp_ref, "hash": signature_hash}
        )
        return approval

    @staticmethod
    def review_and_decide(document, status_val, comments, actor):
        """Submit formal review decision (APPROVED or REJECTED)."""
        document.status = status_val.upper()
        document.save()

        approval = Approval.objects.create(
            document=document,
            version=document.versions.filter(status='Current').first(),
            category=document.folder.replace('_', ' ').title(),
            approved_by_name=actor.get_full_name() or actor.email if getattr(actor, 'is_authenticated', False) else 'Review Officer',
            status=status_val.upper(),
            comments=comments
        )

        DocumentService.log_audit(
            user=actor,
            action=f"DOCUMENT_REVIEW_{status_val.upper()}",
            resource_id=document.id,
            new_state={"status": document.status, "comments": comments}
        )
        return approval

    @staticmethod
    def toggle_star(document):
        """Toggle star/favorite flag."""
        document.is_starred = not document.is_starred
        document.save()
        return document

    @staticmethod
    def create_template(data, user):
        """Create a new standard regulatory document template."""
        template = DocumentTemplate.objects.create(
            title=data.get('title', 'Standard Document Template'),
            category=data.get('category', 'INSPECTION'),
            description=data.get('description', 'Standard regulatory document template for agency workflows.'),
            file_format=data.get('file_format', 'PDF'),
            file_url=data.get('file_url', 'https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/template.pdf'),
            file_size=data.get('file_size', '450 KB'),
            usage_count=0
        )
        return template
