"""
Ingesting an instrument's export file as a telemetry session.

This is the transport leg the current PUNDIT unit actually uses: it has no
radio, so a capture leaves it as a file and reaches the platform when someone
uploads that file from the inspector app. The session it produces is marked
``transport='FILE'`` and is otherwise an ordinary session — packets, chain,
all-or-nothing promotion at ``/end``. Nothing about the file path gets a
shortcut into the registry.

Two rules shape everything here.

**The file supplies measurements; the app supplies context.** A PUNDIT export
holds path lengths and transit times — the numbers only the instrument knows.
It does not hold the structural element, the test type, or the weather, because
those are the inspector's judgements, made on site and entered in the app. So
each may come from either place, and neither is invented to fill a gap. A file
that carries the full documented template works as-is; a bare measurement
export works when the app supplies the element and test type.

**An unrecognised column is refused, never mapped.** If the export writes
``distance_mm`` where the platform documents ``PATH LENGTH L (MM)``, guessing
that they are the same thing would put a wrong number into a statutory
registry with nothing on the record to show it happened. The refusal names the
column and lists the accepted ones, which is also how the contract gets
extended: a real export that fails this check is a sample to map against,
deliberately, rather than a silent misread.

Validation of the rows themselves is not reimplemented. The file is handed to
``apps.data_import.registry``'s UPV builder, so a rule tightened for the CSV
wizard — a pulse-velocity point must have a transit time, a crack-depth point
must have both transit times — is tightened here in the same edit. There is one
definition of what a valid PUNDIT row is.
"""
import hashlib
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

# Reaching into a sibling app's coercion helpers on purpose. They are the
# platform's one definition of "how a value is read out of an uploaded file",
# and the alternative — a second set of numeric and choice parsers here —
# is exactly the drift that would let a file import accept a row the CSV
# wizard rejects, from the same bytes.
from apps.data_import.readers import (
    ImportReadError, SourceRow, detect_import_type, read_rows,
)
from apps.data_import.registry import (
    REGISTRY, RowError, UPV_ACCEPTED_KEYS, UPV_COLUMNS, UPV_CONTEXT_KEYS,
    UPV_MEASUREMENT_KEYS, group_upv_rows,
)
from apps.evidence.files import safe_file_name

from .models import TelemetrySession
from .services import TelemetryError, TelemetryService

logger = logging.getLogger(__name__)

#: Storage prefix for a retained instrument export. Namespaced so a retention
#: sweep can find them without walking every evidence file on the bucket.
EXPORT_STORAGE_PREFIX = 'telemetry/exports'

#: The accepted-column contract, in the key form the registry sees. Defined
#: beside ``UPV_COLUMNS`` in ``apps.data_import.registry`` because that module
#: owns the contract — a device's column mapping is validated against the same
#: sets, and a second definition here is how the two would drift apart.
MEASUREMENT_KEYS = UPV_MEASUREMENT_KEYS
CONTEXT_KEYS = UPV_CONTEXT_KEYS
ACCEPTED_KEYS = UPV_ACCEPTED_KEYS

#: Only PUNDIT has a file contract that describes a whole capture. GPR's
#: documents survey headers (one row, no anomaly rows), and GNSS and SLAM have
#: none. Saying so is better than accepting the file and promoting a session
#: with nothing in it.
UNSUPPORTED_DATA_TYPES = {
    'gpr': ('The platform\'s GPR file contract describes survey headers — one '
            'row per survey — not the anomaly rows a capture session records. '
            'Import a GPR survey through the data-import wizard instead.'),
    'gnss': ('No file contract exists for GNSS boundary points yet. Stream the '
             'survey through the telemetry console, or ask Nexucon to agree a '
             'column layout for the export.'),
    'scan': ('No file contract exists for scan defects yet. Stream the capture '
             'through the telemetry console, or ask Nexucon to agree a column '
             'layout for the export.'),
}


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _accepted_columns():
    """The accepted columns, spelled the way the template spells them."""
    return ', '.join(UPV_COLUMNS)


class SessionFromFileService:
    """Turn an uploaded instrument export into a reviewable telemetry session."""

    @classmethod
    def create(cls, *, uploaded_file, device, project, operator, data_type,
               session_config=None, request=None):
        """Parse ``uploaded_file`` and open an ENDED/PENDING session for it.

        The session is ENDED because the capture is complete — the device
        finished and the file is the whole of it. It is PENDING because
        nothing has been promoted: the inspector reviews the parsed readings
        and promotes them at ``/end``, which is the only place registry rows
        are ever written. Auto-promoting here would mean a file whose parse
        was subtly wrong landed in the statutory registry with no one having
        looked at it.

        Raises ``TelemetryError`` for every refusal, having written nothing —
        no session, no packets, no stored bytes.
        """
        if data_type in UNSUPPORTED_DATA_TYPES:
            raise TelemetryError(UNSUPPORTED_DATA_TYPES[data_type])
        if data_type != 'pundit':
            raise TelemetryError(
                f'File import does not handle "{data_type}" sessions.')

        content = cls._read_upload(uploaded_file)
        name = safe_file_name(getattr(uploaded_file, 'name', ''))
        digest = cls._digest(content)

        # The same bytes arriving twice is a resend, not a second capture. A
        # field gateway whose response was lost in transit will retry, and an
        # inspector unsure whether the upload went through will try again —
        # both must land on the session that already exists rather than filing
        # one measurement twice under two references. Checked before the parse
        # because a resend has nothing left to learn.
        #
        # Scoped to the device: identical bytes from a *different* instrument
        # would be a coincidence worth looking at, not a duplicate to swallow.
        existing = (TelemetrySession.objects
                    .filter(device=device, source_file_sha256=digest)
                    .order_by('created_at')
                    .first())
        if existing is not None:
            logger.info('Telemetry file import: %s is already session %s',
                        name, existing.session_reference)
            return existing, {
                'duplicate': True,
                'rows': existing.packet_count,
                'skipped': 0,
                'readings': existing.packet_count,
            }

        import_type = detect_import_type(content)
        try:
            # The device's own declaration of what its columns are called. A
            # unit that writes its export in the platform's documented spelling
            # carries no mapping and takes the plain path; one that writes
            # `Distance (mm)` is read through the mapping its record holds.
            # Either way `_assert_known_columns` below still runs, so a column
            # the mapping does not cover is refused by name rather than folded
            # into a neighbouring reading.
            rows, skipped = read_rows(content, import_type,
                                      header_map=device.column_mapping or None)
        except ImportReadError as exc:
            raise TelemetryError(str(exc))

        if not rows:
            raise TelemetryError(
                f'{name} held no rows to import'
                + (f' ({skipped} blank lines were skipped).' if skipped else '.')
                + ' An empty file is not a capture.')

        cls._assert_known_columns(rows, name, device.column_mapping)

        config = dict(session_config or {})
        merged = cls._merge_context(rows, config)

        test_type = str(merged[0].data.get('test_type') or '').strip()
        if not test_type:
            raise TelemetryError(
                'TEST TYPE is required — the file does not carry one and none '
                'was given with the upload. A PUNDIT reading means different '
                'things depending on the test it came from, so the platform '
                'will not assume one.')

        # An export is a day's work, not one test. The same file routinely
        # holds every element the operator walked, so the rows are split into
        # the tests they describe before anything is built — by the same rule
        # the CSV wizard applies, so a file and a workbook of the same readings
        # produce the same records.
        #
        # Building one test over every row is what this used to do, and it is
        # why a file of ten elements was read as ten copies of the first
        # element's points and then refused at promotion for repeating a label.
        # The measurements were never wrong; the split was missing.
        groups, grouping_error = group_upv_rows(merged)
        if grouping_error:
            raise TelemetryError(f'{name}: {grouping_error}')

        packets = []
        # What every group in this file agrees on. Nothing else belongs on the
        # session, because a session carries one config and a file may hold
        # several tests.
        shared = None
        for _key, group_rows in groups:
            # The registry's own UPV builder validates the rows — the same code
            # the CSV wizard runs, so the two paths cannot disagree about what
            # a valid PUNDIT row is.
            try:
                payload, _ = REGISTRY['UPV'].build(group_rows,
                                                   _BuildContext(project))
            except RowError as exc:
                raise TelemetryError(str(exc))

            readings = payload.pop('readings', [])
            if not readings:
                raise TelemetryError(
                    f'{name} produced no readable readings. Nothing was imported.')
            payload.pop('project', None)

            # The context of the test a reading belongs to travels on the
            # packet. It cannot live on the session alone: one session has one
            # config, so a grouping kept only there could never describe a file
            # holding more than one element.
            context = {key: value for key, value in payload.items()
                       if key in UPV_CONTEXT_KEYS}
            shared = context if shared is None else {
                key: value for key, value in shared.items()
                if context.get(key) == value}
            for reading in readings:
                packets.append({**context, **reading})

        if not packets:
            raise TelemetryError(
                f'{name} produced no readable readings. Nothing was imported.')

        # The session records only what the whole file agrees on. For a
        # one-element file that is what it always was, and it stays the
        # fallback for a packet that carries no context of its own.
        config.update(shared or {})

        stored_name, digest = cls._store(content, name)

        try:
            with transaction.atomic():
                # Created directly rather than through `start_session`, and
                # opened only long enough to accept its packets. That method
                # refuses a device that already has an OPEN session — the right
                # rule for two live streams from one instrument, and the wrong
                # one here, because importing a file the unit exported
                # yesterday is not a second stream. The session is therefore
                # made, filled, and closed inside this one transaction, so it
                # is never observable in an OPEN state.
                session = TelemetrySession.objects.create(
                    device=device,
                    project=project,
                    operator=operator if (operator and operator.is_authenticated) else None,
                    operator_name=(
                        (operator.get_full_name() or operator.email)
                        if (operator and operator.is_authenticated) else ''
                    ),
                    data_type='pundit',
                    transport=TelemetrySession.TRANSPORT_FILE,
                    status=TelemetrySession.STATUS_OPEN,
                    session_config=config,
                    source_file_name=name,
                    source_file_sha256=digest,
                    source_file_storage_name=stored_name,
                )
                for index, packet in enumerate(packets, start=1):
                    TelemetryService.append_packet(session, packet, sequence=index)
                # Closed within the transaction: the instrument finished long
                # before the file reached the platform, and a session left
                # OPEN would refuse the next import from the same device.
                TelemetrySession.objects.filter(pk=session.pk).update(
                    status=TelemetrySession.STATUS_ENDED,
                    session_end=timezone.now(),
                    updated_at=timezone.now(),
                )
                session.refresh_from_db()
        except TelemetryError:
            cls._discard(stored_name)
            raise
        except Exception as exc:  # noqa: BLE001 — a failed import stores nothing
            cls._discard(stored_name)
            logger.exception('file import failed for %s', name)
            raise TelemetryError(
                f'{name} could not be imported, so nothing was recorded: {exc}')

        logger.info('Telemetry file import: %s → %s (%s readings in %s tests)',
                    name, session.session_reference, len(packets), len(groups))
        return session, {'rows': len(rows), 'skipped': skipped,
                         'readings': len(packets), 'tests': len(groups)}

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    @staticmethod
    def _read_upload(uploaded_file):
        """The uploaded bytes, refusing an empty upload outright.

        Empty is refused here rather than at the parser because the two mean
        different things: a parser seeing zero rows cannot tell an empty file
        from a file of blank lines, and the operator needs to know which.
        """
        if uploaded_file is None:
            raise TelemetryError('An export file is required.')
        try:
            content = uploaded_file.read()
        except Exception as exc:  # noqa: BLE001
            raise TelemetryError(f'The uploaded file could not be read: {exc}')
        if not content:
            raise TelemetryError(
                f'{safe_file_name(getattr(uploaded_file, "name", ""))} is empty. '
                'An empty file cannot be a capture — export the readings again.')
        return content

    @staticmethod
    def _assert_known_columns(rows, name, column_mapping=None):
        """Refuse a file whose columns the platform has not been told about.

        This is the guard that keeps a misread out of the registry. Without
        it, ``{"Distance": 300}`` would simply be ignored for want of a
        ``path_length_l_mm`` and every row would fail as "no path length" —
        a confusing message about the wrong problem, on a file the platform
        could arguably have read.

        ``column_mapping`` is the device's own declaration, and it changes only
        the advice, never the decision: a column the mapping does not name is
        still refused. The message says which of the two fixes applies — extend
        this device's mapping, or agree a layout with Nexucon — because those
        are different people's jobs.
        """
        present = set()
        for row in rows:
            for key, value in row.data.items():
                if key and not _blank(value):
                    present.add(key)
        unknown = sorted(present - ACCEPTED_KEYS)
        if unknown:
            if column_mapping:
                next_step = (
                    f'This device carries a column mapping that does not cover '
                    f'them. Add the missing column(s) to the mapping on the '
                    f'device record, or correct the export.')
            else:
                next_step = (
                    'This device has no column mapping recorded. If this is the '
                    'instrument\'s own column naming, record a mapping on the '
                    'device; otherwise send Nexucon the export itself and the '
                    'layout can be agreed.')
            raise TelemetryError(
                f'{name} has column(s) this platform does not recognise: '
                f'{", ".join(unknown)}. Nothing was imported, because guessing '
                f'which column is which would risk recording a wrong value as '
                f'a measurement. Accepted columns are: {_accepted_columns()}. '
                + next_step,
                code='unknown_columns')

    @staticmethod
    def _merge_context(rows, config):
        """Fold the upload's context into every row, without overwriting the
        file's own values.

        The app's values fill gaps only. Where the file states a structural
        element, that is the instrument's own record of the capture and it
        wins; where it is silent, the inspector's selection applies. A blank
        cell is silence, not a value, so it does not overwrite.
        """
        context = {k: v for k, v in config.items() if k in CONTEXT_KEYS}
        merged = []
        for row in rows:
            data = dict(context)
            for key, value in row.data.items():
                if not _blank(value):
                    data[key] = value
            merged.append(SourceRow(row_number=row.row_number, data=data))
        return merged

    @staticmethod
    def _digest(content):
        """SHA-256 of the export's bytes.

        One definition, used for two jobs that must agree: the name the bytes
        are stored under, and the key a resend is recognised by. Two separate
        hashes would be two chances to disagree about whether a file is the
        one already imported.
        """
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _store(content, name):
        """Retain the export and return ``(storage name, sha256)``.

        The raw bytes are kept because the packets are an *interpretation* of
        them. If the parse is ever questioned, the original is the only thing
        that can settle it.
        """
        digest = SessionFromFileService._digest(content)
        try:
            stored_name = default_storage.save(
                f'{EXPORT_STORAGE_PREFIX}/{digest[:2]}/{digest[:16]}-{name}',
                ContentFile(content))
        except Exception as exc:  # noqa: BLE001 — an unstorable upload is a refusal
            logger.warning('file import: cannot store %s (%s)', name, exc)
            raise TelemetryError(
                f'{name} could not be stored, so it was not imported: {exc}')
        return stored_name, digest

    @staticmethod
    def _discard(stored_name):
        """Remove bytes whose import failed, so a rejected file leaves nothing."""
        try:
            default_storage.delete(stored_name)
        except Exception:  # noqa: BLE001 — best effort; the row was never written
            logger.warning('file import: could not discard %s', stored_name)


class _BuildContext:
    """The minimum ``ImportContext`` the UPV builder reads.

    It only ever touches ``.project`` — the grouping key and the FK on the
    payload — so passing the project is passing everything the builder uses.
    If that entry's ``build`` ever starts reading another context field, this
    stand-in is what must be extended.
    """

    def __init__(self, project):
        self.project = project
        self.user = None
        self.inspection = None
        self.request = None
        self.scan_sessions = {}
        self.inspections_by_reference = {}
