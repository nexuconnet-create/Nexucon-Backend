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
import logging
import uuid

from django.utils import timezone

from common.hashing import canonical_json as _canonical  # noqa: F401  (re-export)
from common.hashing import sha256_hex  # noqa: F401  (re-export)

from .geofence import (
    ENFORCEMENT_OFF,
    ENFORCEMENT_STRICT,
    enforcement_mode,
    evaluate_geofence,
    refusal_message,
)
from .models import Inspection, InspectionSignoff, InspectionSubmission

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Invalid inspection-execution transition or input."""


# `_canonical` and `sha256_hex` now live in common.hashing, which is the layer
# every attestation in the platform shares (inspections, evidence, telemetry).
# They are re-exported under their original names so no call site here — or in
# the tests that exercise them through this module — had to change.


def _parse_device_time(device_time):
    """Parse a device-supplied ISO-8601 timestamp, or None.

    A device with no clock sends nothing; the server receipt time is recorded
    separately. The device's own timestamp is never invented on its behalf.
    """
    if not device_time:
        return None
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(str(device_time).replace('Z', '+00:00'))
    except ValueError:
        raise ExecutionError('device_time must be an ISO-8601 timestamp.')
    if parsed.tzinfo is None:
        from django.conf import settings as dj_settings
        from zoneinfo import ZoneInfo
        parsed = parsed.replace(
            tzinfo=ZoneInfo(getattr(dj_settings, 'TIME_ZONE', 'UTC')))
    return parsed



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
    @staticmethod
    def _validate_coords(latitude, longitude, accuracy_m=None,
                         missing_message=None):
        """Parse and range-check a device position.

        Returns ``(lat, lon, accuracy)`` where `accuracy` is a non-negative
        float in metres or None when the device did not report one. Shared by
        check-in, submit and check-out so the three cannot drift into
        validating different things.
        """
        if latitude is None or longitude is None:
            raise ExecutionError(missing_message or (
                'GPS coordinates (latitude, longitude) are required.'))
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            raise ExecutionError('latitude and longitude must be numeric.')
        if not (-90 <= lat <= 90):
            raise ExecutionError('latitude out of range (-90..90).')
        if not (-180 <= lon <= 180):
            raise ExecutionError('longitude out of range (-180..180).')

        if accuracy_m is None or accuracy_m == '':
            return lat, lon, None
        try:
            accuracy = float(accuracy_m)
        except (TypeError, ValueError):
            raise ExecutionError('gps_accuracy_m must be numeric, in metres.')
        if accuracy < 0:
            raise ExecutionError('gps_accuracy_m must not be negative.')
        return lat, lon, accuracy

    @classmethod
    def checkin(cls, inspection: Inspection, user, latitude, longitude,
                device_time=None, accuracy_m=None):
        """
        Mandatory GPS + timestamp check-in (plan §5 W7: "Mandatory GPS
        coordinates and timestamp for every inspection submission"), evaluated
        against the project's site geofence.

        `gps_verified` is set from the geofence evaluation and is True only
        when the project records site coordinates, the point is within the
        effective radius, and the device's reported accuracy does not exceed
        that radius. It is never set unconditionally — see
        apps/inspections/geofence.py for why each of those three conditions
        matters.

        Whether an unverified check-in is refused or merely recorded depends on
        GEOFENCE_ENFORCEMENT ('off' / 'warn' / 'strict'); the default is
        'warn'.
        """
        lat, lon, accuracy = cls._validate_coords(
            latitude, longitude, accuracy_m,
            missing_message=('GPS coordinates (latitude, longitude) are '
                             'required to start an inspection.'))

        recorded_at = _parse_device_time(device_time)

        mode = enforcement_mode()
        result = None
        if mode != ENFORCEMENT_OFF:
            result = evaluate_geofence(inspection.project, lat, lon, accuracy)
            if mode == ENFORCEMENT_STRICT and not result.verified:
                raise ExecutionError(refusal_message(result))

        inspection.gps_latitude = lat
        inspection.gps_longitude = lon
        inspection.checkin_time = timezone.now()
        inspection.gps_accuracy_m = accuracy
        if result is None:
            # Enforcement off: nothing was evaluated, so nothing is claimed.
            inspection.gps_verified = False
            inspection.geofence_state = ''
            inspection.geofence_reason = ''
            inspection.geofence_distance_m = None
        else:
            inspection.gps_verified = result.verified
            inspection.geofence_state = result.state
            inspection.geofence_reason = result.reason
            inspection.geofence_distance_m = (
                None if result.distance_m is None else round(result.distance_m, 2))
        if inspection.status == 'REQUESTED' or inspection.status == 'SCHEDULED':
            inspection.status = 'IN_PROGRESS'
        if user and getattr(user, 'is_authenticated', False) and not inspection.inspector:
            inspection.inspector = user
            inspection.inspector_name = user.get_full_name() or user.email
        inspection.save()

        _record_audit(user, 'inspection.execution.checkin', inspection, {
            'latitude': lat, 'longitude': lon,
            'accuracy_m': accuracy,
            'device_time': recorded_at.isoformat() if recorded_at else None,
            'server_time': inspection.checkin_time.isoformat(),
            'gps_verified': inspection.gps_verified,
            'geofence': result.as_dict() if result else None,
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
        if not checklist_results:
            raise ExecutionError('checklist_results are required (may be an empty list only for free-form inspections with evidence).')
        if not evidence_files and not checklist_results:
            raise ExecutionError('A submission requires checklist results and/or evidence files.')

        # You cannot submit an inspection you never checked into. Check-in is
        # where the geofence is evaluated, so without it a submission carries
        # coordinates that no geofence ever saw. Previously `submit()` silently
        # backfilled `checkin_time` with the submission time and set
        # `gps_verified = True` outright, which is what made the flag
        # meaningless for every submission that skipped check-in.
        if not inspection.checkin_time:
            raise ExecutionError(
                'Check in before submitting — this inspection has no recorded '
                'check-in, so its site presence has not been verified.')

        # Under strict enforcement a submission additionally requires that the
        # check-in actually verified. Under 'warn' the unverified state is
        # recorded and reported, not blocked.
        if enforcement_mode() == ENFORCEMENT_STRICT and not inspection.gps_verified:
            raise ExecutionError(
                'This inspection was not geofence-verified at check-in '
                f'({inspection.geofence_reason or "no reason recorded"}), so it '
                'cannot be submitted under strict geofence enforcement.')

        lat, lon, accuracy = cls._validate_coords(
            latitude, longitude,
            missing_message=('Mandatory GPS coordinates missing — every '
                             'submission must carry the device location at '
                             'submission time.'))

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

        recorded_at = _parse_device_time(device_time)
        submitted_at = timezone.now()

        submission_hash = sha256_hex(_canonical({
            'inspection': str(inspection.id),
            'checklist': normalised,
            'evidence': evidence,
            'gps': [lat, lon],
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
            gps_latitude=lat,
            gps_longitude=lon,
            gps_recorded_at=recorded_at,
            submitted_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
            submitted_at=submitted_at,
            submission_hash=submission_hash,
            device_info=str(device_info or '')[:255],
        )

        # Reflect on the inspection itself. `gps_verified` is deliberately NOT
        # touched here: it is the check-in's geofence verdict and is the only
        # thing that ever writes it. Re-asserting it at submit time would let a
        # submission claim a verification the check-in never made.
        inspection.checklist_results = normalised
        inspection.gps_latitude = lat
        inspection.gps_longitude = lon
        inspection.completed_date = submitted_at
        inspection.status = 'COMPLETED'
        inspection.save()

        _record_audit(user, 'inspection.execution.submit', inspection, {
            'submission_id': str(submission.id),
            'submission_hash': submission_hash,
            'items': len(normalised),
            'evidence_files': len(evidence),
            'gps_verified': inspection.gps_verified,
            'geofence_state': inspection.geofence_state,
        })
        return submission

    # ----------------------------------------------------------- check-out
    @classmethod
    def checkout(cls, inspection: Inspection, user, latitude=None,
                 longitude=None, device_time=None, accuracy_m=None):
        """
        Record that the inspector has left the site.

        Deliberately does NOT change `inspection.status`: the spec defines no
        status transition for check-out, and inventing one (e.g. forcing
        COMPLETED) would fabricate a workflow step the client never specified.
        Check-out is a recorded fact — time and position — not a state change.
        """
        if not inspection.checkin_time:
            raise ExecutionError(
                'Cannot check out of an inspection that was never checked into.')
        if inspection.check_out_time:
            raise ExecutionError(
                'This inspection has already been checked out at '
                f'{inspection.check_out_time.isoformat()}.')

        recorded_at = _parse_device_time(device_time)
        lat = lon = accuracy = None
        if latitude is not None or longitude is not None:
            lat, lon, accuracy = cls._validate_coords(
                latitude, longitude, accuracy_m,
                missing_message=('Check-out needs both latitude and longitude, '
                                 'or neither.'))

        inspection.check_out_time = timezone.now()
        inspection.checkout_latitude = lat
        inspection.checkout_longitude = lon
        inspection.save(update_fields=[
            'check_out_time', 'checkout_latitude', 'checkout_longitude',
            'updated_at',
        ])

        _record_audit(user, 'inspection.execution.checkout', inspection, {
            'latitude': lat, 'longitude': lon,
            'accuracy_m': accuracy,
            'device_time': recorded_at.isoformat() if recorded_at else None,
            'check_out_time': inspection.check_out_time.isoformat(),
        })
        return inspection

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
