"""
Applier registry: what a queued item is allowed to do, and how it does it.

Every entity type the offline client may queue is declared here exactly once,
with the actions it permits. Two rules follow from that and they are the whole
point of this module:

**Nothing is queued that cannot be applied.** ``enqueue`` consults this
registry before it writes a row, so an item the server could never process is
refused while the inspector is still looking at the screen — not silently
parked in a queue that will fail on every flush until someone notices.

**There is no second write path.** Each applier runs the *same serializer* the
manual create/update endpoint runs. A queued finding is validated by
``FindingSerializer``; a queued telemetry session is promoted by
``TelemetryService``, which already reuses the digital-eye serializers. If a
rule tightens in one place it tightens here, because here *is* there.

``DELETE`` appears in the spec's action vocabulary but in none of the
registries below. That is deliberate rather than an omission: inspections,
findings, stop-work orders and evidence records are statutory records. They are
resolved, lifted or superseded — never deleted from the field. A registry entry
offering DELETE would be a promise the platform does not keep, so the queue
refuses it at enqueue and says which actions it does accept.
"""
from dataclasses import dataclass
from typing import Callable, FrozenSet

from common.errors import describe_drf_error
from common.permissions import scoped_projects


class SyncApplyError(Exception):
    """A queued item could not be applied. The message goes to the client."""


@dataclass(frozen=True)
class ApplyResult:
    """Where a queued write landed."""

    model_label: str
    target_id: str
    created: bool = True


@dataclass(frozen=True)
class Applier:
    """One entity type's contract with the queue.

    ``validate`` runs at *enqueue* and may only inspect the payload — it must
    not touch the database, because the state at enqueue is not the state at
    apply time and a check that passes offline is not a promise about later.
    Its job is to catch the structural mistakes a client makes (a telemetry
    session with no packets, an update with no ``entity_id``) so they surface
    immediately rather than on the next reconnect.

    ``apply`` runs at *process* time, inside the caller's transaction, and is
    where real validation happens.
    """

    entity_type: str
    model_label: str
    actions: FrozenSet[str]
    validate: Callable[[dict, str], None]
    apply: Callable[[object, object], ApplyResult]
    description: str = ''


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def _require(payload, keys, entity_type, action):
    missing = [k for k in keys if payload.get(k) in (None, '', [], {})]
    if missing:
        raise SyncApplyError(
            f'A {action} {entity_type} needs {", ".join(missing)} '
            f'in its payload.'
        )


def _in_scope(user, project_id, what):
    """Assert a project is inside the caller's scope.

    The manual endpoints scope their project FK through ``ScopedProjectField``.
    The serializers used here do not — they are the plain model serializers —
    so scoping is done explicitly, and *before* the serializer runs, so a
    payload naming another agency's project never reaches a save.
    """
    if not project_id:
        raise SyncApplyError(f'{what} does not identify a project.')
    try:
        allowed = scoped_projects(user).filter(pk=project_id).exists()
    except Exception:  # noqa: BLE001 — a malformed uuid is simply not in scope
        allowed = False
    if not allowed:
        raise SyncApplyError(
            f'{what} names a project that is not in your scope.')


def _run_serializer(serializer_class, data, request, instance=None, **save_kwargs):
    """Validate and save through the app's own serializer.

    Raises ``SyncApplyError`` carrying the flattened validation message rather
    than the raw ``ValidationError``: the string ends up in the queue row's
    ``last_error`` and is rendered verbatim on a phone.
    """
    kwargs = {'data': data, 'context': {'request': request}}
    if instance is not None:
        kwargs['instance'] = instance
        kwargs['partial'] = True
    serializer = serializer_class(**kwargs)
    if not serializer.is_valid():
        raise SyncApplyError(describe_drf_error(serializer.errors))
    return serializer.save(**save_kwargs)


# ----------------------------------------------------------------------
# Inspections
# ----------------------------------------------------------------------

def _validate_inspection(payload, action):
    if action == 'CREATE':
        _require(payload, ['project', 'inspection_type'], 'INSPECTION', action)


def _apply_inspection(item, request):
    from apps.inspections.models import Inspection
    from apps.inspections.serializers import InspectionSerializer

    user = item.inspector
    inspector_name = user.get_full_name() or user.email
    payload = dict(item.payload)

    if item.action == 'CREATE':
        _in_scope(user, payload.get('project'), 'This inspection')
        obj = _run_serializer(
            InspectionSerializer, payload, request,
            # The inspector is the authenticated caller, never the payload.
            # A client that could name its own inspector could file an
            # inspection under a colleague's badge.
            inspector=user, inspector_name=inspector_name,
        )
        return ApplyResult('inspections.Inspection', obj.id, True)

    obj = _scoped_instance(Inspection, user, item, 'inspection')
    obj = _run_serializer(InspectionSerializer, payload, request, instance=obj)
    return ApplyResult('inspections.Inspection', obj.id, False)


# ----------------------------------------------------------------------
# Findings
# ----------------------------------------------------------------------

def _validate_finding(payload, action):
    if action == 'CREATE':
        _require(payload, ['inspection', 'title', 'description'], 'FINDING', action)


def _apply_finding(item, request):
    from apps.inspections.models import Finding, Inspection
    from apps.inspections.serializers import FindingSerializer

    user = item.inspector
    payload = dict(item.payload)

    if item.action == 'CREATE':
        # A finding belongs to an inspection, and its project is that
        # inspection's project. Resolving scope through the parent means the
        # client cannot attach a finding to an inspection it cannot see, nor
        # claim a project the inspection is not on.
        inspection = _scoped_instance_by_id(
            Inspection, user, payload.get('inspection'), 'inspection')
        payload['project'] = str(inspection.project_id)
        obj = _run_serializer(FindingSerializer, payload, request)
        return ApplyResult('inspections.Finding', obj.id, True)

    obj = _scoped_instance(Finding, user, item, 'finding')
    obj = _run_serializer(FindingSerializer, payload, request, instance=obj)
    return ApplyResult('inspections.Finding', obj.id, False)


# ----------------------------------------------------------------------
# Stop work orders
# ----------------------------------------------------------------------

def _validate_stop_work_order(payload, action):
    if action == 'CREATE':
        _require(payload, ['project', 'reason'], 'STOP_WORK_ORDER', action)


def _apply_stop_work_order(item, request):
    from apps.inspections.models import StopWorkOrder
    from apps.inspections.serializers import StopWorkOrderSerializer

    user = item.inspector
    payload = dict(item.payload)
    issuer = user.get_full_name() or user.email

    if item.action == 'CREATE':
        _in_scope(user, payload.get('project'), 'This stop work order')
        obj = _run_serializer(
            StopWorkOrderSerializer, payload, request,
            issued_by_name=issuer,
        )
        return ApplyResult('inspections.StopWorkOrder', obj.id, True)

    # Lifting a stop work order is an UPDATE, and the lift is attributed the
    # same way the issue was — from the authenticated caller.
    obj = _scoped_instance(StopWorkOrder, user, item, 'stop work order')
    obj = _run_serializer(
        StopWorkOrderSerializer, payload, request, instance=obj,
        lifted_by_name=issuer,
    )
    return ApplyResult('inspections.StopWorkOrder', obj.id, False)


# ----------------------------------------------------------------------
# Evidence records
# ----------------------------------------------------------------------

def _validate_evidence(payload, action):
    _require(payload, ['project', 'source_type', 'source_model', 'source_id'],
             'EVIDENCE', action)


def _apply_evidence(item, request):
    """Write through the ingestion service, not the serializer's save.

    ``EvidenceIngestionService`` keys on ``(source_model, source_id)`` with
    ``update_or_create`` and computes ``evidence_hash`` itself. Going around it
    would produce a record with an empty hash — an evidence row that claims to
    be in the registry but is not verifiable, which is worse than no row.

    ``source_type`` is required rather than derived from ``source_model``. A
    type inferred by string-munging a model name is a guess, and the evidence
    registry's whole purpose is that every row says truthfully what it is.
    """
    from apps.evidence.ingestion import EvidenceIngestionService
    from apps.evidence.models import EvidenceRecord

    user = item.inspector
    payload = dict(item.payload)
    _in_scope(user, payload.get('project'), 'This evidence record')
    # The ingestion service takes the project *object*, not its id — it assigns
    # it to the FK directly. Resolved through `scoped_projects` so the row it
    # writes can only ever name a project the caller can already see.
    project = scoped_projects(user).filter(pk=payload['project']).first()
    if project is None:
        raise SyncApplyError(
            'This evidence record names a project that is not in your scope.')

    valid_types = {value for value, _label in EvidenceRecord.SOURCE_TYPES}
    if payload['source_type'] not in valid_types:
        raise SyncApplyError(
            f'source_type "{payload["source_type"]}" is not a recognised '
            f'evidence type. Valid values: {", ".join(sorted(valid_types))}.')

    record = EvidenceIngestionService.ingest_record(
        project=project,
        source_type=payload['source_type'],
        source_model=payload['source_model'],
        source_id=payload['source_id'],
        structural_element_id=payload.get('structural_element_id') or '',
        bim_guid=payload.get('bim_guid') or '',
        coordinates=payload.get('coordinates'),
        captured_at=payload.get('captured_at'),
        confidence=payload.get('confidence'),
        payload=payload.get('payload') or {},
        ingested_by=user,
    )
    return ApplyResult('evidence.EvidenceRecord', record.id, True)


# ----------------------------------------------------------------------
# Telemetry sessions
# ----------------------------------------------------------------------

def _validate_telemetry(payload, action):
    if action != 'CREATE':
        # A telemetry session is captured whole, offline, and replayed whole.
        # There is no meaningful "update a session" — packets are append-only
        # and a session that has ended is immutable.
        raise SyncApplyError(
            'A TELEMETRY item can only be CREATE. A session is captured as a '
            'whole and replayed as a whole.')
    _require(payload, ['device', 'project', 'data_type', 'packets'],
             'TELEMETRY', action)
    if not isinstance(payload.get('packets'), (list, tuple)):
        raise SyncApplyError('A TELEMETRY payload needs packets as a list.')


def _apply_telemetry(item, request):
    """Replay a whole offline capture through the telemetry service.

    The service — not this module — decides what a session means: it opens one,
    chains the packets, and promotes them all-or-nothing through the real
    digital-eye serializers. Nothing about ingestion is reimplemented here.

    Resuming is why ``item.target_id`` is read first. If a previous attempt
    opened a session and then failed at promotion, the session already holds
    the packets the device sent. Starting a second one would ask the device for
    data it has already given up, and would leave an orphaned OPEN session
    blocking the device from ever starting another.
    """
    from apps.telemetry.models import TelemetrySession
    from apps.telemetry.services import TelemetryError, TelemetryService

    user = item.inspector
    payload = item.payload
    _in_scope(user, payload.get('project'), 'This telemetry session')
    project = scoped_projects(user).filter(pk=payload['project']).first()

    session = None
    if item.target_id:
        session = TelemetrySession.objects.filter(pk=item.target_id).first()
        if session and session.sync_status == TelemetrySession.SYNC_SYNCED:
            # Already promoted on a previous attempt that did not manage to
            # mark the queue row. Re-applying would duplicate every row.
            return ApplyResult('telemetry.TelemetrySession', session.id, False)
        if session and session.status == TelemetrySession.STATUS_ABORTED:
            session = None

    try:
        if session is None:
            from apps.digital_eye.models import FieldDevice
            device = FieldDevice.objects.filter(
                pk=payload['device'], is_active=True).first()
            if not device:
                raise SyncApplyError(
                    f'Device {payload["device"]} was not found or is not active.')
            session = TelemetryService.start_session(
                device=device,
                project=project,
                operator=user,
                data_type=payload['data_type'],
                session_config=payload.get('session_config') or {},
                session_start=payload.get('session_start'),
            )
            # Recorded inside the caller's transaction, so the session and the
            # pointer to it commit together or not at all. A pointer written
            # outside it would survive a rollback and aim the next attempt at a
            # session that no longer exists.
            item.target_id = str(session.id)
            item.save(update_fields=['target_id', 'updated_at'])

        already = set(session.packets.values_list('sequence', flat=True))
        for index, packet in enumerate(payload['packets'], start=1):
            if not isinstance(packet, dict):
                raise SyncApplyError(f'Packet #{index} is not an object.')
            sequence = packet.get('sequence') or index
            if sequence in already:
                # Replay of a packet already received. Skipped rather than
                # refused: on a resume this is the expected case, and the
                # append-only rule exists to stop a *client* renumbering, not
                # to punish a retry.
                continue
            TelemetryService.append_packet(
                session, packet.get('payload'),
                sequence=sequence, recorded_at=packet.get('recorded_at'),
            )

        promoted = TelemetryService.end_session(session, request)
    except TelemetryError as exc:
        # The service marks the session FAILED inside its own savepoint; this
        # re-raise is what rolls the item back to a state a retry can resume
        # from. The message is already written for a human to read.
        raise SyncApplyError(str(exc))
    except SyncApplyError:
        raise

    return ApplyResult('telemetry.TelemetrySession', session.id, True)


# ----------------------------------------------------------------------
# Scoped lookups
# ----------------------------------------------------------------------

def _scoped_instance_by_id(model, user, entity_id, label):
    if not entity_id:
        raise SyncApplyError(
            f'This {label} update does not say which {label} it refers to — '
            'send entity_id.')
    try:
        obj = model.objects.filter(pk=entity_id).first()
    except Exception:  # noqa: BLE001 — a malformed uuid is simply not found
        obj = None
    if not obj:
        raise SyncApplyError(f'{label.capitalize()} {entity_id} was not found.')
    project_id = getattr(obj, 'project_id', None)
    if project_id is None:
        # Findings reach their project through their inspection.
        inspection = getattr(obj, 'inspection', None)
        project_id = inspection.project_id if inspection else None
    _in_scope(user, project_id, f'{label.capitalize()} {entity_id}')
    return obj


def _scoped_instance(model, user, item, label):
    return _scoped_instance_by_id(model, user, item.entity_id, label)


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------

REGISTRY = {
    'INSPECTION': Applier(
        entity_type='INSPECTION',
        model_label='inspections.Inspection',
        actions=frozenset({'CREATE', 'UPDATE'}),
        validate=_validate_inspection,
        apply=_apply_inspection,
        description='A field inspection record.',
    ),
    'FINDING': Applier(
        entity_type='FINDING',
        model_label='inspections.Finding',
        actions=frozenset({'CREATE', 'UPDATE'}),
        validate=_validate_finding,
        apply=_apply_finding,
        description='A finding raised against an inspection.',
    ),
    'STOP_WORK_ORDER': Applier(
        entity_type='STOP_WORK_ORDER',
        model_label='inspections.StopWorkOrder',
        actions=frozenset({'CREATE', 'UPDATE'}),
        validate=_validate_stop_work_order,
        apply=_apply_stop_work_order,
        description=('A stop work order, or the lift of one (an UPDATE on an '
                     'existing order).'),
    ),
    'EVIDENCE': Applier(
        entity_type='EVIDENCE',
        model_label='evidence.EvidenceRecord',
        actions=frozenset({'CREATE', 'UPDATE'}),
        validate=_validate_evidence,
        apply=_apply_evidence,
        description=('An evidence registry row. The file bytes it describes '
                     'are uploaded separately — this queues the record.'),
    ),
    'TELEMETRY': Applier(
        entity_type='TELEMETRY',
        model_label='telemetry.TelemetrySession',
        actions=frozenset({'CREATE'}),
        validate=_validate_telemetry,
        apply=_apply_telemetry,
        description='A complete offline instrument capture, replayed whole.',
    ),
}

#: Every action named in the spec's vocabulary. Kept beside the registry so the
#: 400 that refuses DELETE can list what the platform does support without
#: hardcoding a second copy of the list.
KNOWN_ACTIONS = ('CREATE', 'UPDATE', 'DELETE')
