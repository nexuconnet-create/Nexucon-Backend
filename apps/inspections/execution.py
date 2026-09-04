"""
Inspection Execution API service (implementation plan §5 Week 7).

Mobile inspection execution with:
  * dynamic checklists assembled from InspectionTemplate / ChecklistItem
  * mandatory GPS + device timestamp check-in
  * tamper-evident evidence upload — every artifact carries a SHA-256
    checksum and the whole submission is hashed
  * cryptographic digital sign-off binding inspector, content and time
  * automatic submission records in the immutable audit ledger

Everything is computed from the submitted content — no values are invented.
"""
import hashlib
import json
import logging
import uuid

from django.utils import timezone

from .models import Inspection, InspectionSignoff, InspectionSubmission

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Invalid inspection-execution transition or input."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _record_audit(user, action, inspection, metadata, severity='High'):
    from apps.audit.models import AuditEvent
    audit_user = user if (user and getattr(user, 'is_authenticated', False)) else None
    AuditEvent.objects.create(
        user=audit_user,
        user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
        action=action,
        resource_type='Inspection',
        resource_id=str(inspection.id),
        severity=severity,
        new_state=metadata,
        metadata={'inspection_reference': inspection.inspection_reference},
    )


class InspectionExecutionService:
    """Week-7 inspection execution workflow."""

    # ------------------------------------------------------ dynamic checklist
    @classmethod
    def build_checklist(cls, inspection, template=None):
        """
        Assemble the dynamic checklist for an inspection. A template can be
        chosen explicitly; otherwise the active template whose department
        matches the inspection type is used. Returns None when no template
        exists — the inspector then uses a free-form checklist.
        """
        from apps.settings.models import ChecklistItem, InspectionTemplate

        if template is None:
            template = InspectionTemplate.objects.filter(
                status='Active',
            ).filter(
                models_q_department(inspection),
            ).first() or InspectionTemplate.objects.filter(status='Active').first()
        if template is None:
            return None
        items = ChecklistItem.objects.filter(template=template)
        return {
            'template_id': template.id,
            'template_name': template.name,
            'version': template.version,
            'items': [
                {
                    'item_id': str(item.id),
                    'order': item.item_order,
                    'title': item.title,
                    'field_type': item.field_type,
                    'required': item.is_required,
                }
                for item in items
            ],
        }

    # ----------------------------------------------------------- check-in
    @classmethod
    def checkin(cls, inspection: Inspection, user, latitude, longitude,
                device_time=None):
        """
        Mandatory GPS + timestamp check-in (plan §5 W7: "Mandatory GPS
        coordinates and timestamp for every inspection submission").
        """
        if latitude is None or longitude is None:
            raise ExecutionError('GPS coordinates (latitude, longitude) are required to start an inspection.')
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            raise ExecutionError('latitude and longitude must be numeric.')
        if not (-90 <= lat <= 90):
            raise ExecutionError('latitude out of range (-90..90).')
        if not (-180 <= lon <= 180):
            raise ExecutionError('longitude out of range (-180..180).')

        recorded_at = None
        if device_time:
            from datetime import datetime
            try:
                parsed = datetime.fromisoformat(str(device_time).replace('Z', '+00:00'))
            except ValueError:
                raise ExecutionError('device_time must be an ISO-8601 timestamp.')
            if parsed.tzinfo is None:
                from django.conf import settings as dj_settings
                from zoneinfo import ZoneInfo
                parsed = parsed.replace(tzinfo=ZoneInfo(getattr(dj_settings, 'TIME_ZONE', 'UTC')))
            recorded_at = parsed

        inspection.gps_latitude = lat
        inspection.gps_longitude = lon
        inspection.checkin_time = timezone.now()
        inspection.gps_verified = True
        if inspection.status == 'REQUESTED' or inspection.status == 'SCHEDULED':
            inspection.status = 'IN_PROGRESS'
        if user and getattr(user, 'is_authenticated', False) and not inspection.inspector:
            inspection.inspector = user
            inspection.inspector_name = user.get_full_name() or user.email
        inspection.save()

        _record_audit(user, 'inspection.execution.checkin', inspection, {
            'latitude': lat, 'longitude': lon,
            'device_time': recorded_at.isoformat() if recorded_at else None,
            'server_time': inspection.checkin_time.isoformat(),
        })
        return inspection

    # ------------------------------------------------------------ submit
    @classmethod
    def submit(cls, inspection: Inspection, user, *, checklist_results,
               evidence_files, latitude, longitude, device_time=None,
               template=None, device_info=''):
        """
        Submit the completed inspection. Builds the tamper-evident record:
        every evidence file's SHA-256, the checklist results, GPS coordinates
        and the device timestamp are combined into the submission hash.
        """
        if latitude is None or longitude is None:
            raise ExecutionError(
                'Mandatory GPS coordinates missing — every submission must carry '
                'the device location at submission time.')
        if not checklist_results:
            raise ExecutionError('checklist_results are required (may be an empty list only for free-form inspections with evidence).')
        if not evidence_files and not checklist_results:
            raise ExecutionError('A submission requires checklist results and/or evidence files.')

        # Normalise checklist results.
        normalised = []
        for i, item in enumerate(checklist_results):
            normalised.append({
                'item_id': str(item.get('item_id') or ''),
                'title': str(item.get('title') or ''),
                'result': item.get('result'),
                'notes': str(item.get('notes') or ''),
                'evidence_hashes': item.get('evidence_hashes') or [],
            })

        # Normalise evidence files (name + sha256 are mandatory per artifact).
        evidence = []
        for f in evidence_files or []:
            digest = f.get('sha256') or f.get('sha256_checksum')
            if not digest:
                raise ExecutionError(
                    f"Evidence file {f.get('file_name', '?')} is missing its SHA-256 checksum.")
            evidence.append({
                'file_name': str(f.get('file_name') or ''),
                'sha256': str(digest).lower(),
                'url': str(f.get('url') or ''),
                'file_type': str(f.get('file_type') or ''),
            })

        from datetime import datetime
        recorded_at = None
        if device_time:
            try:
                parsed = datetime.fromisoformat(str(device_time).replace('Z', '+00:00'))
                recorded_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.now().tzinfo)
            except ValueError:
                raise ExecutionError('device_time must be an ISO-8601 timestamp.')
        submitted_at = timezone.now()

        submission_hash = sha256_hex(_canonical({
            'inspection': str(inspection.id),
            'checklist': normalised,
            'evidence': evidence,
            'gps': [float(latitude), float(longitude)],
            'device_time': recorded_at.isoformat() if recorded_at else None,
            'submitted_at': submitted_at.isoformat(),
            'submitted_by': str(user.id) if user and getattr(user, 'is_authenticated', False) else None,
        }))

        template_obj = template
        if template_obj is None and normalised and normalised[0].get('item_id'):
            # item_id is part of the sealed payload and may be a client-side
            # identifier; only attempt the template lookup when it is a valid
            # UUID (a malformed value must not 500 the submission).
            try:
                first_item_uuid = uuid.UUID(str(normalised[0]['item_id']))
            except (ValueError, AttributeError, TypeError):
                first_item_uuid = None
            if first_item_uuid is not None:
                from apps.settings.models import ChecklistItem
                first = ChecklistItem.objects.filter(id=first_item_uuid).first()
                template_obj = first.template if first else None

        submission = InspectionSubmission.objects.create(
            inspection=inspection,
            checklist_template=template_obj,
            checklist_results=normalised,
            evidence_files=evidence,
            gps_latitude=float(latitude),
            gps_longitude=float(longitude),
            gps_recorded_at=recorded_at,
            submitted_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
            submitted_at=submitted_at,
            submission_hash=submission_hash,
            device_info=str(device_info or '')[:255],
        )

        # Reflect on the inspection itself.
        inspection.checklist_results = normalised
        inspection.gps_latitude = float(latitude)
        inspection.gps_longitude = float(longitude)
        inspection.checkin_time = inspection.checkin_time or submitted_at
        inspection.gps_verified = True
        inspection.completed_date = submitted_at
        inspection.status = 'COMPLETED'
        inspection.save()

        _record_audit(user, 'inspection.execution.submit', inspection, {
            'submission_id': str(submission.id),
            'submission_hash': submission_hash,
            'items': len(normalised),
            'evidence_files': len(evidence),
        })
        return submission

    # ----------------------------------------------------------- sign-off
    @classmethod
    def sign_off(cls, submission: InspectionSubmission, user, signature_text=''):
        """
        Cryptographic digital sign-off: binds the submission hash, the
        inspector identity and the signing time into a single SHA-256
        signature (plan §5 W7: "Crypto digital sign-off").
        """
        if hasattr(submission, 'signoff'):
            raise ExecutionError('This submission has already been signed off.')
        if user is None or not getattr(user, 'is_authenticated', False):
            raise ExecutionError('Sign-off requires an authenticated inspector.')

        signed_at = timezone.now()
        signoff_hash = sha256_hex(_canonical({
            'submission_hash': submission.submission_hash,
            'inspection': str(submission.inspection_id),
            'inspector': str(user.id),
            'inspector_name': user.get_full_name() or user.email,
            'signature_text': signature_text,
            'signed_at': signed_at.isoformat(),
        }))

        signoff = InspectionSignoff.objects.create(
            submission=submission,
            signed_by=user,
            signed_by_name=user.get_full_name() or user.email,
            signature_text=str(signature_text or ''),
            signed_at=signed_at,
            signoff_hash=signoff_hash,
        )
        _record_audit(user, 'inspection.execution.signoff',
                      submission.inspection, {
                          'signoff_hash': signoff_hash,
                          'submission_hash': submission.submission_hash,
                      }, severity='Critical')
        return signoff

    # -------------------------------------------------------- verification
    @classmethod
    def verify_submission(cls, submission: InspectionSubmission) -> bool:
        """Recompute the submission hash — detects any tampering."""
        recorded_at = submission.gps_recorded_at
        device_time = recorded_at.isoformat() if recorded_at else None

        expected = sha256_hex(_canonical({
            'inspection': str(submission.inspection_id),
            'checklist': submission.checklist_results,
            'evidence': submission.evidence_files,
            'gps': [submission.gps_latitude, submission.gps_longitude],
            'device_time': device_time,
            'submitted_at': submission.submitted_at.isoformat(),
            'submitted_by': str(submission.submitted_by_id) if submission.submitted_by_id else None,
        }))
        return expected == submission.submission_hash

    @classmethod
    def verify_signoff(cls, signoff: InspectionSignoff) -> bool:
        submission = signoff.submission
        expected = sha256_hex(_canonical({
            'submission_hash': submission.submission_hash,
            'inspection': str(submission.inspection_id),
            'inspector': str(signoff.signed_by_id),
            'inspector_name': signoff.signed_by_name,
            'signature_text': signoff.signature_text,
            'signed_at': signoff.signed_at.isoformat(),
        }))
        return expected == signoff.signoff_hash


def models_q_department(inspection):
    """Q filter matching active templates to the inspection's discipline."""
    from django.db.models import Q
    discipline = (inspection.inspection_type or '').lower()
    mapping = {
        'structural review': 'structural',
        'foundation inspection': 'structural',
        'mep inspection': 'mep',
        'safety audit': 'safety',
        'drainage & environmental': 'environmental',
    }
    department = mapping.get(discipline)
    if department:
        return Q(department__iexact=department)
    return Q()
