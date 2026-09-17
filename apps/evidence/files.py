"""
File evidence: storing the bytes, and answering "are these still them?".

Two operations, and the split between them is the design.

**Store** writes the bytes, then reads them back to hash them. The read-back is
deliberate: a digest taken from the buffer the client sent attests what the
client *claimed*, while a digest taken from storage attests what **exists** —
which is the only thing ``/verify/`` can ever check later, and the only thing
that means anything as evidence. The same read-back is what ``/verify/`` does,
so the two operations are symmetric by construction rather than by agreement.

Two checks come free with it, and both are named:

* the storage backend's byte count must equal what was uploaded — a short write
  is a real failure mode, and hashing alone would not notice one;
* when the client sends its own SHA-256 or size, it is compared, and a mismatch
  refuses the upload naming both values.

**Verify** re-reads the stored bytes and reports two independent answers,
because they are different questions:

``payload_ok``
    Does the record's canonical JSON payload still hash to ``evidence_hash``?
    That is what the correlation engine read.

``file_bytes_ok``
    Do the stored bytes still hash to the digest recorded at upload?

Collapsing them into one boolean would let a tampered payload and a replaced
file look identical, and would make a record with no file at all report as a
failure rather than as the honest ``None`` it is.
"""
import hashlib
import logging
import os

from django.conf import settings
from django.utils import timezone

from .ingestion import EvidenceIngestionService
from .models import EvidenceFile

logger = logging.getLogger(__name__)

_UPLOAD_DEFAULT_BYTES = 25 * 1024 * 1024


class EvidenceFileError(Exception):
    """The upload or verification was refused. The message is shown to the caller."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _setting(name):
    return int(getattr(settings, name, _UPLOAD_DEFAULT_BYTES))


def safe_file_name(name):
    """The client's filename, reduced to something safe to store and display.

    A filename is client input that reaches both a storage key and, later, a
    screen. Stripping the path separators is what keeps ``../../etc/passwd``
    from being a path rather than a name.
    """
    base = os.path.basename(str(name or '')).replace('\\', '_').strip()
    return base[:255] or 'upload'


class EvidenceFileService:
    """Storing an evidence file, and verifying one that is already stored."""

    @classmethod
    def store(cls, *, record, uploaded_file, request=None,
              expected_sha256='', expected_size=None):
        """Attach uploaded bytes to an evidence record.

        One file per record, enforced by the ``OneToOneField``: a record whose
        bytes could be replaced by a second upload would have two answers to
        "what is this evidence?", and the hash recorded on the first would
        describe a file nobody can reach any more.

        Raises ``EvidenceFileError``; nothing is left in storage when it does.
        """
        if record is None:
            raise EvidenceFileError('An evidence record is required.', status_code=400)
        if getattr(record, 'file', None) is not None:
            raise EvidenceFileError(
                f'Evidence {record.evidence_reference} already has a file. '
                'Delete it first if it is the wrong one — replacing it silently '
                'would leave the recorded hash describing a file nobody can '
                'reach.', status_code=409)

        name = safe_file_name(getattr(uploaded_file, 'name', ''))
        max_bytes = _setting('EVIDENCE_MAX_UPLOAD_BYTES')
        declared = getattr(uploaded_file, 'size', None)
        if declared is not None and declared > max_bytes:
            raise EvidenceFileError(
                f'{name} is {declared} bytes, above the '
                f'{max_bytes // (1024 * 1024)} MiB an evidence upload accepts.',
                status_code=400)

        evidence_file = EvidenceFile(
            record=record,
            file_name=name,
            content_type=(getattr(uploaded_file, 'content_type', '') or '')[:120],
            uploaded_by=getattr(request, 'user', None)
            if getattr(request, 'user', None) is not None
            and request.user.is_authenticated else None,
        )
        # `save=False` so the row is written once, after the file has been
        # measured and hashed — never in a state that claims a size it has not
        # confirmed.
        evidence_file.file.save(name, uploaded_file, save=False)
        stored_name = evidence_file.file.name

        try:
            size = evidence_file.file.size
        except Exception as exc:  # noqa: BLE001 — an unreadable store is a refusal
            logger.warning('evidence upload: cannot size %s (%s)', stored_name, exc)
            size = None

        if not size:
            cls._discard(evidence_file)
            raise EvidenceFileError(
                f'{name} stored no bytes. An empty file cannot be evidence of '
                'anything — upload the capture itself.', status_code=400)

        if expected_size is not None and int(expected_size) != size:
            cls._discard(evidence_file)
            raise EvidenceFileError(
                f'{name} stored {size} bytes but the client declared '
                f'{int(expected_size)}. The upload was truncated; nothing was '
                'recorded.', status_code=400)

        stored_hash = evidence_file.compute_stored_hash()
        if expected_sha256 and stored_hash.lower() != str(expected_sha256).lower():
            cls._discard(evidence_file)
            raise EvidenceFileError(
                f'{name} does not match the hash the client sent. Client said '
                f'{expected_sha256}; storage holds {stored_hash}. Nothing was '
                'recorded.', status_code=400)

        evidence_file.file_size_bytes = size
        evidence_file.sha256_hash = stored_hash
        evidence_file.storage_name = stored_name
        evidence_file.save()
        logger.info(
            'Evidence file stored: %s for %s (%s bytes)',
            name, record.evidence_reference, size,
        )
        return evidence_file

    @staticmethod
    def _discard(evidence_file):
        """Remove bytes that were refused, so a rejected upload leaves nothing."""
        try:
            evidence_file.file.delete(save=False)
        except Exception:  # noqa: BLE001 — the refusal is the answer; cleanup is best-effort
            logger.exception('evidence upload: could not remove refused bytes')

    @classmethod
    def file_backed_record(cls, *, project, uploaded_file, request=None,
                           extraction=None, inspection=None, **context):
        """A record whose whole evidence *is* the file.

        Used by the upload endpoint, which has no source row to normalise: the
        file is the source. ``source_model`` and ``source_id`` are left blank on
        purpose — the registry's uniqueness constraint is conditional on
        ``source_model`` being non-empty, so filling it in would make the
        constraint compare a row against itself and block a second upload of the
        same name.

        The payload is the caller's own context plus the file's own measured
        facts. Nothing is inferred: a field the uploader left out is absent from
        the payload rather than present as a placeholder.
        """
        payload = {k: v for k, v in (context or {}).items() if v not in ('', None)}
        payload['file_name'] = safe_file_name(getattr(uploaded_file, 'name', ''))
        declared_type = getattr(uploaded_file, 'content_type', '') or ''
        if declared_type:
            payload['declared_content_type'] = declared_type
        if extraction:
            payload['extraction'] = extraction

        record = EvidenceIngestionService.ingest_record(
            project=project,
            source_type='uploaded_file',
            source_model='',
            source_id='',
            structural_element_id=context.get('structural_element_id') or '',
            bim_guid=context.get('bim_guid') or '',
            coordinates=context.get('coordinates'),
            captured_at=context.get('captured_at'),
            confidence=context.get('confidence'),
            payload=payload,
            ingested_by=getattr(request, 'user', None)
            if getattr(request, 'user', None) is not None
            and request.user.is_authenticated else None,
        )
        if inspection is not None:
            record.inspection = inspection
            record.save(update_fields=['inspection', 'updated_at'])
        return record

    @classmethod
    def verify(cls, record):
        """Re-check a record's payload hash and its stored bytes.

        Returns a dict and never raises for a mismatch: a file that fails
        verification is a *result*, and the caller has to see it. Only a
        missing record is an error, and the view turns that into a 404.
        """
        payload_ok = bool(record.evidence_hash) and (
            record.evidence_hash == record.compute_hash())

        evidence_file = getattr(record, 'file', None)
        result = {
            'evidence_reference': record.evidence_reference,
            'payload_ok': payload_ok,
            'evidence_hash': record.evidence_hash,
            'file_present': evidence_file is not None,
            'file_bytes_ok': None,
            'file_sha256': None,
            'file_size_bytes': None,
            'note': '',
            'verified_at': timezone.now().isoformat(),
        }

        if evidence_file is None:
            result['note'] = ('No file is attached to this record. Its payload '
                              'is all the evidence there is.')
            return result

        result['file_sha256'] = evidence_file.sha256_hash
        result['file_size_bytes'] = evidence_file.file_size_bytes

        ceiling = _setting('EVIDENCE_VERIFY_MAX_BYTES')
        if evidence_file.file_size_bytes > ceiling:
            note = (
                f'{evidence_file.file_name} is {evidence_file.file_size_bytes} '
                f'bytes, above the {ceiling // (1024 * 1024)} MiB that will be '
                're-read for verification. Its bytes were not checked.'
            )
            result['note'] = note
            cls._record_verification(evidence_file, None, note)
            return result

        try:
            stored_hash = evidence_file.compute_stored_hash()
        except Exception as exc:  # noqa: BLE001 — unreadable storage is a result
            note = (f'{evidence_file.file_name} could not be read back from '
                    f'storage ({exc.__class__.__name__}). Its bytes were not '
                    'checked.')
            result['note'] = note
            cls._record_verification(evidence_file, None, note)
            return result

        result['file_bytes_ok'] = stored_hash == evidence_file.sha256_hash
        if not result['file_bytes_ok']:
            result['note'] = (
                'The stored bytes no longer match the digest recorded when the '
                'file was uploaded. The file has been changed or replaced since '
                f'it was stored (storage now holds {stored_hash}).'
            )
        cls._record_verification(
            evidence_file, result['file_bytes_ok'], result['note'])
        return result

    @staticmethod
    def _record_verification(evidence_file, ok, note):
        """Persist the outcome, so the last verification is readable without
        re-hashing anything."""
        evidence_file.last_verify_ok = ok
        evidence_file.last_verified_at = timezone.now()
        evidence_file.last_verify_note = note[:255]
        evidence_file.save(update_fields=[
            'last_verify_ok', 'last_verified_at', 'last_verify_note', 'updated_at'])


def sha256_of(uploaded_file):
    """SHA-256 of an uploaded file's bytes, for tests and callers that hold one."""
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    return digest.hexdigest()
