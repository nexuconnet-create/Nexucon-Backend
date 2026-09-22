"""
Manual import service: upload, validate, commit.

The three stages are separate on purpose, and each one exists because of a
specific way an import goes wrong.

**Upload stores and attests. It parses nothing.**
    A file that is written to storage with a server-computed hash is a fact; a
    file that was parsed and rejected on the way in leaves the inspector with a
    message and nothing to look at. So the bytes land first, the hash is
    computed over the bytes that were actually received, and the stored file's
    size is checked against the number of bytes hashed — a mismatch means the
    storage backend did not keep what it was given, and that is reported rather
    than stored as though it were true.

**Validate decides, and writes nothing.**
    Every row is parsed with no early exit, so the inspector sees *all* the
    problems in one pass instead of fixing one and discovering the next. Each
    row is then run through the same serializer the manual entry endpoint uses,
    so a value the platform rejects in a file is a value it rejects in a form.
    Validation replaces its records rather than appending, so running it twice
    gives the same answer as running it once.

**Commit re-validates, then writes, and the status is written with the rows.**
    The re-validation closes the window where a serializer rule changed between
    the two calls. The status line is inside the same ``atomic()`` block as the
    rows, and that is the most important line in this module: writing
    ``IMPORTED`` outside the transaction lets a rollback leave a batch claiming
    to have imported a file that produced zero records.

All-or-nothing is the invariant throughout. A batch with 9 good rows and 1 bad
row is ``FAILED``, and its counts say 9 and 1 — the inspector is told how close
it was, and nothing enters a statutory registry from a file the platform has
already rejected.
"""
import hashlib
import io
import logging
import os

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.inspections.models import Inspection
from common.errors import describe_drf_error

from .models import ImportBatch, ImportRecord
from .readers import ImportReadError, SourceRow, detect_import_type, read_rows
from .registry import REGISTRY, ImportContext, RowError

logger = logging.getLogger(__name__)

#: Kept as a module constant rather than a settings lookup at each use: these
#: bound a *response*, and a response that changed size with the deployment
#: would make a client's paging logic wrong in a way nothing reports.
DEFAULT_MAX_ERRORS = 200


class ImportServiceError(Exception):
    """A refused import operation. Carries an HTTP status for the view."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class _ImportContextUser:
    """A request stand-in carrying only a user.

    The serializers in this platform scope their project field through
    ``self.context['request'].user``. Validation and commit take the real
    request when a view supplies one; when they are driven from a service call
    there is no request, and the honest stand-in is the batch's own inspector —
    the same user whose scope was checked when the file was uploaded.
    """

    def __init__(self, user):
        self.user = user
        self.method = 'POST'
        self.data = {}
        self.query_params = {}
        self.META = {}


def _max_errors():
    return int(getattr(settings, 'IMPORT_MAX_ERRORS', DEFAULT_MAX_ERRORS))


def _safe_name(name, fallback):
    """A filename safe to use as a storage key, extension preserved."""
    base = os.path.basename(name or '').strip().replace('\\', '/')
    base = base.rsplit('/', 1)[-1]
    cleaned = ''.join(ch if (ch.isalnum() or ch in '._- ') else '_' for ch in base)
    cleaned = cleaned.strip().strip('.')
    return cleaned[:200] or fallback


class ImportService:

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    @classmethod
    def upload(cls, *, user, project, uploaded_file, request=None,
               import_type='', record_type='', inspection=None):
        """Store an uploaded file and attest to it. Returns ``(batch, created)``.

        ``created`` is False when these exact bytes were already uploaded by
        this inspector for this project and are still awaiting a result — the
        endpoint reports that as ``deduplicated: true``. An already-imported
        batch is never reused: the same bytes uploaded again is a new
        submission, and pointing it at a committed batch would let a second
        commit write the file's rows a second time.
        """
        content, digest, size = cls._read_upload(uploaded_file)

        detected = detect_import_type(content)
        claimed = (import_type or '').strip().upper()
        if claimed and claimed != detected:
            # The bytes win, and the disagreement is reported rather than
            # resolved. Silently overriding a ".csv" that is really a PDF would
            # hide the fact that the wrong file was picked — which is the thing
            # the uploader needs to be told.
            raise ImportServiceError(
                f'The file was uploaded as {claimed} but its contents are '
                f'{detected}. Check that the right file was chosen.')
        import_type = detected

        record_type = (record_type or '').strip().upper()
        if record_type and record_type not in REGISTRY:
            raise ImportServiceError(
                f'"{record_type}" is not an importable record type. '
                f'Valid types: {", ".join(sorted(REGISTRY))}.')

        if inspection is not None and str(inspection.project_id) != str(project.id):
            raise ImportServiceError(
                'The inspection named belongs to a different project.')

        existing = ImportBatch.objects.filter(
            inspector=user, project=project, import_type=import_type,
            sha256_hash=digest, record_type=record_type,
            inspection=inspection,
            import_status__in=(ImportBatch.STATUS_PENDING,
                               ImportBatch.STATUS_FAILED),
        ).first()
        if existing is not None:
            return existing, False

        key = f'imports/{project.id}/{digest[:16]}/{_safe_name(uploaded_file.name, "upload")}'
        storage_name = default_storage.save(key, _BytesReader(content))
        stored_size = default_storage.size(storage_name)
        if stored_size != size:
            # The hash describes the bytes that were received; if storage kept
            # a different number of them, the hash no longer describes the file
            # on disk and saying otherwise would be a false attestation.
            default_storage.delete(storage_name)
            raise ImportServiceError(
                f'The uploaded file could not be stored intact: {size} bytes '
                f'were received and {stored_size} were written. Nothing was '
                'recorded — upload the file again.')

        batch = ImportBatch.objects.create(
            inspector=user,
            inspector_name=user.get_full_name() or user.email,
            project=project,
            inspection=inspection,
            import_type=import_type,
            record_type=record_type,
            file_name=_safe_name(uploaded_file.name, 'upload')[:200],
            file_size_bytes=size,
            file_url=default_storage.url(storage_name),
            storage_name=storage_name,
            sha256_hash=digest,
            import_status=ImportBatch.STATUS_PENDING,
        )
        return batch, True

    @staticmethod
    def _read_upload(uploaded_file):
        """``(bytes, sha256_hex, length)`` in one chunked pass.

        Chunked rather than ``.read()``: a 40 MB sensor export must not become
        40 MB of resident memory on a worker that is also serving requests.
        """
        digest = hashlib.sha256()
        parts = []
        size = 0
        limit = int(getattr(settings, 'IMPORT_MAX_UPLOAD_BYTES', 25 * 1024 * 1024))
        for chunk in uploaded_file.chunks():
            size += len(chunk)
            if size > limit:
                raise ImportServiceError(
                    f'The file is larger than {limit // (1024 * 1024)} MB. '
                    'Split it into several files — a single import is bounded '
                    'so one upload cannot exhaust the server.')
            digest.update(chunk)
            parts.append(chunk)
        if size == 0:
            raise ImportServiceError('The uploaded file is empty.')
        return b''.join(parts), digest.hexdigest(), size

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    @classmethod
    def validate(cls, batch, request=None):
        """Parse and check every row. Writes no registry rows.

        Runs from PENDING, FAILED or VALIDATED. From IMPORTED it is refused:
        the batch's rows are in the registry, and re-validating would replace
        the record list of a batch that has already been committed.
        """
        if batch.import_status == ImportBatch.STATUS_IMPORTED:
            raise ImportServiceError(
                'This batch has already been imported. Upload the file again '
                'to import it a second time.', status_code=409)

        ctx_user = getattr(request, 'user', None) or batch.inspector
        ctx = ImportContext(
            user=ctx_user, project=batch.project, inspection=batch.inspection,
            request=request or _ImportContextUser(batch.inspector),
        )

        try:
            rows = cls._parse(batch, ctx)
        except ImportReadError as exc:
            return cls._finish_failed(batch, [{'row': 0, 'message': str(exc)}])

        prepared, errors = cls._prepare(batch, rows, ctx)

        ImportRecord.objects.filter(batch=batch).delete()
        ImportRecord.objects.bulk_create([
            ImportRecord(
                batch=batch,
                row_number=item['record'].row_number,
                row_end=item['record'].row_end,
                record_type=item['record'].record_type,
                raw_data=item['record'].raw_data,
                record_data=item['record'].record_data,
                validation_status=item['record'].validation_status,
                error_message=item['record'].error_message,
            )
            for item in prepared
        ])

        valid = sum(1 for item in prepared if item['record'].validation_status == 'VALID')
        invalid = len(prepared) - valid

        batch.record_count = len(prepared)
        batch.valid_record_count = valid
        batch.invalid_record_count = invalid
        batch.skipped_row_count = getattr(batch, '_skipped_rows', 0)
        batch.validation_errors = errors[:cls._error_cap()]
        batch.errors_truncated = len(errors) > cls._error_cap()
        batch.validated_at = timezone.now()

        # `or errors` is a guard, not redundancy. Every error path in `_prepare`
        # now also produces an INVALID record, so `invalid` would catch them —
        # but a VALIDATED batch is one commit away from writing rows, and a
        # future error path that forgot to add its record would turn a rejected
        # file into a partial import. Failing closed here costs nothing.
        if invalid or errors or not prepared:
            batch.import_status = ImportBatch.STATUS_FAILED
            if not prepared and not errors:
                batch.validation_errors = [{
                    'row': 0,
                    'message': ('No records were found in this file. Download '
                                'the template for this record type — the first '
                                'line must name the columns.'),
                }]
        else:
            batch.import_status = ImportBatch.STATUS_VALIDATED

        batch.save(update_fields=[
            'record_count', 'valid_record_count', 'invalid_record_count',
            'skipped_row_count', 'validation_errors', 'errors_truncated',
            'validated_at', 'import_status', 'updated_at',
        ])
        return batch

    @classmethod
    def _finish_failed(cls, batch, errors):
        """The file could not be read at all — no records are written."""
        ImportRecord.objects.filter(batch=batch).delete()
        batch.record_count = 0
        batch.valid_record_count = 0
        batch.invalid_record_count = 0
        batch.validation_errors = errors[:cls._error_cap()]
        batch.errors_truncated = len(errors) > cls._error_cap()
        batch.validated_at = timezone.now()
        batch.import_status = ImportBatch.STATUS_FAILED
        batch.save(update_fields=[
            'record_count', 'valid_record_count', 'invalid_record_count',
            'validation_errors', 'errors_truncated', 'validated_at',
            'import_status', 'updated_at',
        ])
        return batch

    @staticmethod
    def _error_cap():
        return _max_errors()

    @classmethod
    def _parse(cls, batch, ctx):
        """Read the stored file back and turn it into rows.

        Re-read from storage rather than cached from the upload: the bytes that
        are validated are the bytes that were attested, and a file that has
        been removed from storage since is reported rather than silently
        validated against something remembered.
        """
        try:
            with default_storage.open(batch.storage_name, 'rb') as handle:
                content = handle.read()
        except Exception as exc:  # noqa: BLE001 — any storage failure is the same to the caller
            raise ImportReadError(
                f'The uploaded file could not be read back from storage '
                f'({exc.__class__.__name__}). Upload it again.')
        if not content:
            raise ImportReadError(
                'The stored file is empty. Upload it again.')
        if hashlib.sha256(content).hexdigest() != batch.sha256_hash:
            raise ImportReadError(
                'The stored file does not match the hash recorded when it was '
                'uploaded. Nothing was imported — upload the file again.')

        rows, skipped = read_rows(content, batch.import_type)
        batch._skipped_rows = skipped

        max_rows = int(getattr(settings, 'IMPORT_MAX_ROWS', 20000))
        if len(rows) > max_rows:
            raise ImportReadError(
                f'The file has {len(rows)} rows, more than the {max_rows} a '
                'single import accepts. Split it into several files.')
        return rows

    @classmethod
    def _prepare(cls, batch, rows, ctx):
        """Group rows, resolve references, and validate each record.

        Returns ``(prepared, errors)`` where each prepared item is
        ``{'record': <unsaved ImportRecord>, 'payload': dict,
        'save_kwargs': dict}``. Runs no saves, so it is safe to call inside the
        commit transaction as well as during validation.

        Every rejected row becomes an ``ImportRecord`` carrying the message,
        including the ones rejected before a record could be formed. That is not
        bookkeeping: the batch's status is decided by its invalid record count,
        so a row that was dropped without one would leave a batch reporting
        ``VALIDATED`` while an error sat in its own error list — and it would
        import, silently missing that row.
        """
        cls._resolve_references(batch, ctx)

        record_types = cls._record_types_for(batch, rows)
        groups, dropped = cls._group(rows, record_types)

        prepared = []
        errors = []
        for row, record_type, message in dropped:
            errors.append({
                'row': row.row_number,
                'record_type': record_type,
                'message': message,
            })
            prepared.append({
                'record': ImportRecord(
                    batch=batch,
                    row_number=row.row_number,
                    row_end=row.row_number,
                    record_type=record_type,
                    raw_data=[row.data],
                    validation_status=ImportRecord.STATUS_INVALID,
                    error_message=message,
                ),
                'payload': {},
                'save_kwargs': {},
            })

        for record_type, group_rows in groups:
            entry = REGISTRY[record_type]
            record = ImportRecord(
                batch=batch,
                row_number=group_rows[0].row_number,
                row_end=group_rows[-1].row_number,
                record_type=record_type,
                raw_data=[row.data for row in group_rows],
                validation_status=ImportRecord.STATUS_INVALID,
            )
            try:
                payload, save_kwargs = entry.build(group_rows, ctx)
            except RowError as exc:
                record.error_message = str(exc)
                errors.append({
                    'row': group_rows[0].row_number,
                    'record_type': record_type,
                    'message': str(exc),
                })
                prepared.append({'record': record, 'payload': {},
                                 'save_kwargs': {}})
                continue

            record.record_data = _jsonable(payload)
            problem = cls._check(entry, payload, save_kwargs, ctx)
            if problem:
                record.error_message = problem
                errors.append({
                    'row': group_rows[0].row_number,
                    'record_type': record_type,
                    'message': problem,
                })
            else:
                record.validation_status = ImportRecord.STATUS_VALID
            prepared.append({'record': record, 'payload': payload,
                             'save_kwargs': save_kwargs})

        errors.sort(key=lambda item: item.get('row') or 0)
        return prepared, errors

    @staticmethod
    def _resolve_references(batch, ctx):
        """Load, once, the rows a file may refer to by name."""
        from apps.scans.models import ScanSession

        sessions = ScanSession.objects.filter(project=batch.project)
        ctx.scan_sessions.update({str(s.id): s for s in sessions})

        inspections = Inspection.objects.filter(project=batch.project)
        ctx.inspections_by_reference.update(
            {i.inspection_reference: i for i in inspections if i.inspection_reference})

    @staticmethod
    def _record_types_for(batch, rows):
        """Which record type each row is.

        A single-kind file declares its type once on the batch — that is what
        the template is for, and it is the only shape a CSV can take. A JSON
        file may carry a ``record_type`` on each record, which is the one case
        where one file legitimately holds more than one kind of thing.
        """
        if batch.record_type:
            return [batch.record_type] * len(rows)
        types = []
        for row in rows:
            declared = str(row.data.get('record_type') or '').strip().upper()
            types.append(declared)
        return types

    @staticmethod
    def _group(rows, record_types):
        """Pair rows with their type, then group consecutive rows per record.

        Returns ``(groups, dropped)``. Each dropped entry is
        ``(row, record_type, message)`` — a row that never became a record, and
        the reason. ``_prepare`` turns those into failed ``ImportRecord`` rows so
        a rejected row is always visible in the batch's own reports.

        Grouping exists for UPV, where one reading point is not a test. The
        rule — same structural element, same test type, same floor — is the one
        ``digital_eye.excel_import`` already applies to the spreadsheet
        template, kept identical so a CSV and a workbook of the same readings
        produce the same records. A block that is interrupted and resumed is an
        error rather than two tests, because it is almost always a sorting
        accident, and silently splitting it would file the same element twice.
        """
        dropped = []
        groups = []
        seen_keys = {}

        for row, record_type in zip(rows, record_types):
            if record_type not in REGISTRY:
                dropped.append((row, record_type, (
                    f'Record type {record_type!r} is not importable. Valid '
                    f'types: {", ".join(sorted(REGISTRY))}.')))
                continue

            entry = REGISTRY[record_type]
            missing = [key for key in entry.required
                       if _blank(row.data.get(_fold(key)))]
            if missing:
                dropped.append((row, record_type, (
                    f'{", ".join(missing)} is required for a '
                    f'{record_type} record.')))
                continue

            if not entry.groups:
                groups.append((record_type, [row]))
                continue

            data = row.data
            key = (_text(data.get('structural_element')),
                   _text(data.get('test_type')).lower(),
                   _text(data.get('floor')))
            current = groups[-1] if groups else None
            if (current is not None and current[0] == record_type
                    and _group_key(current[1]) == key):
                current[1].append(row)
                continue
            if key in seen_keys:
                dropped.append((row, record_type, (
                    f'Rows for element {key[0]!r} (test type {key[1]!r}, '
                    f'floor {key[2] or "not recorded"}) appear in two '
                    f'separate blocks, the first starting at row '
                    f'{seen_keys[key]}. Keep one element\'s points on '
                    'consecutive rows so they form a single test.')))
                continue
            seen_keys[key] = row.row_number
            groups.append((record_type, [row]))

        return groups, dropped

    @staticmethod
    def _check(entry, payload, save_kwargs, ctx):
        """Run the record through its own serializer. Returns an error or ''.

        ``is_valid`` and not ``save``: validation must not write. A serializer
        that is valid here is validated again inside the commit transaction,
        which is where the write happens.
        """
        serializer_class = entry.serializer_class()
        serializer = serializer_class(
            data=payload, context={'request': ctx.request})
        if serializer.is_valid():
            return ''
        return describe_drf_error(serializer.errors)

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    @staticmethod
    def _save_kwargs(entry, item, ctx):
        """The build's save arguments, plus the record's owner.

        ``created_by`` and ``operator`` are the same values the manual create
        endpoints hand to ``serializer.save()``, so a record that arrived in a
        file is attributed exactly like one typed into the form. They are added
        here rather than in the builder because they come from the caller, not
        from the file — and by ``setdefault``, so a builder with a better answer
        for a given field keeps it.
        """
        save_kwargs = dict(item['save_kwargs'])
        for name in entry.owner_fields:
            save_kwargs.setdefault(name, ctx.user)
        return save_kwargs

    @classmethod
    def commit(cls, batch, request=None):
        """Write every validated row, and mark the batch imported, atomically.

        The whole write is one transaction, so a failure on row 400 of 500
        leaves the registry exactly as it was. That is not a nicety: a partly
        imported file leaves an inspector unable to tell how much of it landed,
        and the batch's own counts would then describe a state that no longer
        exists.
        """
        if batch.import_status == ImportBatch.STATUS_IMPORTED:
            raise ImportServiceError(
                'This batch has already been imported.', status_code=409)
        if not batch.can_commit:
            raise ImportServiceError(
                f'Only a fully validated batch can be imported. This batch is '
                f'{batch.import_status}'
                + (f' with {batch.invalid_record_count} rejected '
                   f'record(s)' if batch.invalid_record_count else '')
                + ' — validate it again after fixing the file.', status_code=409)

        ctx = ImportContext(
            user=getattr(request, 'user', None) or batch.inspector,
            project=batch.project, inspection=batch.inspection,
            request=request or _ImportContextUser(batch.inspector),
        )

        try:
            rows = cls._parse(batch, ctx)
        except ImportReadError as exc:
            # The stored file went away, or no longer hashes to what was
            # attested, between validation and commit. Refused, not raised: the
            # batch is still VALIDATED and still shows what it was validated
            # against, which is the honest state.
            raise ImportServiceError(str(exc), status_code=409)

        prepared, _errors = cls._prepare(batch, rows, ctx)

        written = 0
        with transaction.atomic():
            for item in prepared:
                record = item['record']
                if record.validation_status != ImportRecord.STATUS_VALID:
                    # A rule tightened between validate and commit. The whole
                    # import is refused rather than partly written.
                    raise ImportServiceError(
                        f'Row {record.row_number} no longer passes validation: '
                        f'{record.error_message} Nothing was imported — '
                        'validate the batch again.', status_code=409)

                entry = REGISTRY[record.record_type]
                serializer_class = entry.serializer_class()
                serializer = serializer_class(
                    data=item['payload'], context={'request': ctx.request})
                if not serializer.is_valid():
                    raise ImportServiceError(
                        f'Row {record.row_number} no longer passes validation: '
                        f'{describe_drf_error(serializer.errors)} Nothing was '
                        'imported — validate the batch again.', status_code=409)

                obj = serializer.save(**cls._save_kwargs(entry, item, ctx))
                written += 1

                ImportRecord.objects.filter(
                    batch=batch, row_number=record.row_number,
                    record_type=record.record_type,
                ).update(
                    record_data=_jsonable(item['payload']),
                    target_model=entry.model_label,
                    target_id=str(obj.pk),
                )

            # Inside the same transaction as the rows above, deliberately. A
            # status written outside it would survive a rollback and leave a
            # batch claiming IMPORTED with nothing in the registry.
            batch.import_status = ImportBatch.STATUS_IMPORTED
            batch.imported_at = timezone.now()
            batch.valid_record_count = written
            batch.save(update_fields=[
                'import_status', 'imported_at', 'valid_record_count', 'updated_at'])

        return batch

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @classmethod
    def status(cls, batch, error_limit=None):
        """The batch, its counts, and its errors — capped, and said to be capped."""
        cap = error_limit or cls._error_cap()
        errors = list(batch.validation_errors or [])
        return {
            'batch_id': str(batch.id),
            'batch_reference': batch.batch_reference,
            'file_name': batch.file_name,
            'import_type': batch.import_type,
            'record_type': batch.record_type,
            'import_status': batch.import_status,
            'project': str(batch.project_id),
            'inspection': str(batch.inspection_id) if batch.inspection_id else None,
            'sha256_hash': batch.sha256_hash,
            'file_size_bytes': batch.file_size_bytes,
            'record_count': batch.record_count,
            'valid_record_count': batch.valid_record_count,
            'invalid_record_count': batch.invalid_record_count,
            'skipped_row_count': batch.skipped_row_count,
            'error_count': len(batch.validation_errors or []),
            'errors_truncated': batch.errors_truncated,
            'errors': errors[:cap],
            'can_commit': batch.can_commit,
            'validated_at': batch.validated_at,
            'imported_at': batch.imported_at,
            'created_at': batch.created_at,
        }


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

class _BytesReader:
    """A stream over bytes that reports ``size``.

    Django's storage backends read ``.chunks()`` and consult ``.size`` for
    metadata; a bare ``io.BytesIO`` has the first but not the second, and the
    S3 backend logs a missing-size warning for every upload without it.

    The reads and the seeks are delegated to a real ``BytesIO`` rather than
    answered from the bytes directly, and that is not tidiness. A backend
    decides whether it may seek by *asking*, and django-storages asks the
    weakest question there is — ``is_seekable`` reads the absence of a
    ``seekable`` method as permission — and then calls ``seek(0, SEEK_SET)``
    before it uploads. An object that advertises a ``seek`` taking one
    argument and then meets that call raises ``TypeError`` from inside the
    storage backend, which surfaces as a 500 on an upload the importer never
    saw.

    None of this shows on the local filesystem, which never seeks: the failure
    exists only on the R2/S3 storage the deployed platform uses, and there it
    takes down *every* upload rather than one bad file.

    Delegating also makes ``read`` advance the way callers assume. The
    previous version returned the first ``size`` bytes on every call, so a
    consumer reading in a loop would have read the same bytes forever.
    """

    def __init__(self, content):
        self._stream = io.BytesIO(content)
        self.size = len(content)
        self.name = ''

    def chunks(self, chunk_size=1024 * 1024):
        buffer = self._stream.getbuffer()
        for start in range(0, self.size, chunk_size):
            yield bytes(buffer[start:start + chunk_size])

    def read(self, size=-1):
        return self._stream.read(size)

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def seekable(self):
        return True

    def readable(self):
        return True

    def __len__(self):
        return self.size


def _text(value):
    return '' if value is None else str(value).strip()


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _fold(column):
    from .readers import normalise_key
    return normalise_key(column)


def _group_key(rows):
    data = rows[0].data
    return (_text(data.get('structural_element')),
            _text(data.get('test_type')).lower(),
            _text(data.get('floor')))


def _jsonable(value):
    """A payload safe to store in a JSONField.

    Serializer payloads carry UUIDs and ``Decimal``s; both are meaningful to a
    human reading the record back and neither is JSON. Converted here rather
    than at each build site, so every record's stored interpretation goes
    through one rule.
    """
    import datetime
    import decimal
    import uuid as uuid_module

    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, uuid_module.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, SourceRow):
        return {'row': value.row_number, 'data': _jsonable(value.data)}
    return value
