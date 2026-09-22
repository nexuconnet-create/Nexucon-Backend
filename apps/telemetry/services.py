"""
Telemetry ingestion service.

The contract this module implements:

  start  → a session exists, OPEN and PENDING, holding no interpretation of
           anything. Nothing is written to any statutory registry yet.
  append → packets accumulate, each one chained to the last. Still nothing
           interpreted.
  end    → every packet is replayed through the SAME serializer the manual
           entry form uses. All valid → the real rows and their Evidence
           Registry records are written in one transaction, and the session
           becomes SYNCED. Any invalid → nothing at all is written, and the
           session becomes FAILED carrying the reason.

That last rule is the important one. A partial promotion would leave a GPR
survey in the registry describing only the anomalies that happened to parse,
with no indication that rows were dropped — a statutory record that under-
reports what was found. All-or-nothing is the only defensible failure mode,
and the packet log is what makes it possible: the operator can see which
sequence number was rejected and re-capture that row.
"""
import logging

from django.db import transaction
from django.utils import timezone

from common.errors import describe_drf_error as _describe  # noqa: F401  (re-export)
from common.hashing import canonical_json, chain_hash, sha256_hex

from .models import (
    DeviceToken, TelemetryPacket, TelemetrySession,
    generate_device_token, hash_device_token,
)

logger = logging.getLogger(__name__)


class TelemetryError(Exception):
    """Invalid telemetry operation. Carries an HTTP status for the view.

    ``code`` is an optional machine-readable label for the refusals a client
    has to *act* on differently rather than merely display — an unrecognised
    column, say, where the app offers to record the mapping. Without it the
    only way to tell those apart would be to match on the English message,
    which breaks the first time the wording is improved. Most refusals carry
    none, and a client that ignores it behaves exactly as before.
    """

    def __init__(self, message, status_code=400, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class DeviceTokenService:
    """Issuing and revoking the credentials instruments push with.

    Separate from ``apps.settings.APIKeyGateway`` because the two answer
    different questions. That gateway issues *platform* credentials — a key
    that acts for an application across many endpoints. This issues a
    credential bound to one ``FieldDevice``, which is what makes a pushed
    reading's provenance checkable: the token names the instrument, so the
    session's device is not a field the client can choose.
    """

    @classmethod
    def issue(cls, *, device, label, issued_by, expires_at=None):
        """Mint a credential for ``device``. Returns ``(token_row, plaintext)``.

        The plaintext is returned here and nowhere else — it is never stored
        and never recoverable, so a lost credential is replaced, not looked up.
        """
        label = (label or '').strip()
        if not label:
            raise TelemetryError(
                'A label is required — "which credential is this?" has to be '
                'answerable later, when one of several is being revoked.')

        raw = generate_device_token()
        token = DeviceToken.objects.create(
            device=device,
            label=label,
            # The scheme marker plus eight characters of the secret, which is
            # enough to recognise a credential in a list and not enough to use.
            key_prefix=raw[:14],
            hashed_key=hash_device_token(raw),
            issued_by=issued_by if (issued_by and issued_by.is_authenticated) else None,
            expires_at=expires_at,
        )
        logger.info('Device credential issued for %s by %s',
                    device.device_id, getattr(issued_by, 'email', 'system'))
        return token, raw

    @classmethod
    def revoke(cls, token, revoked_by=None):
        """Revoke a credential. Idempotent, and effective on the next request."""
        if token.revoked_at is not None:
            return token
        token.revoked_at = timezone.now()
        token.save(update_fields=['revoked_at'])
        logger.info('Device credential revoked for %s by %s',
                    token.device.device_id, getattr(revoked_by, 'email', 'system'))
        return token


class _SessionPromotionContext:
    """Minimal request stand-in for a promotion with no HTTP request.

    Exists only so ``ScopedProjectField`` can read ``.user``. See
    ``TelemetryService._context`` for why this grants nothing extra.
    """

    def __init__(self, user):
        self.user = user


class TelemetryService:
    """Session lifecycle and promotion into the statutory registries."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def start_session(cls, *, device, project, operator, data_type,
                      session_config=None, session_start=None, transport=''):
        """Open a session for ``device`` on ``project``.

        Refuses a second OPEN session for the same device. Two open sessions
        would mean two packet sequences arriving from one instrument with no
        way to tell which capture a reading belongs to — the device serial is
        the provenance, and it can only be in one place at a time.

        ``transport`` is stored as given, including empty: a caller that does
        not know how it reached the platform records that it does not know,
        rather than being assigned a plausible default.
        """
        existing = TelemetrySession.objects.filter(
            device=device, status=TelemetrySession.STATUS_OPEN).first()
        if existing:
            raise TelemetryError(
                f'Device {device.device_id} already has an open session '
                f'({existing.session_reference}). End it before starting another.',
                status_code=409,
            )

        return TelemetrySession.objects.create(
            device=device,
            project=project,
            operator=operator if (operator and operator.is_authenticated) else None,
            operator_name=(
                (operator.get_full_name() or operator.email)
                if (operator and operator.is_authenticated) else ''
            ),
            data_type=data_type,
            transport=transport or '',
            session_config=session_config or {},
            session_start=session_start,
        )

    @classmethod
    def append_packet(cls, session, payload, sequence=None, recorded_at=None):
        """Append one reading to an open session, chained to the previous one.

        ``sequence`` defaults to the next free position. When the device sends
        its own, a repeat is refused rather than overwritten: the packet log is
        append-only, so a re-sent sequence number is a client bug that must
        surface, not a row to silently replace.
        """
        if not session.is_open:
            raise TelemetryError(
                f'Session {session.session_reference} is {session.status} and '
                'cannot accept more packets.',
                status_code=409,
            )
        if payload is None:
            raise TelemetryError('A packet payload is required.')

        if sequence is None:
            last = session.packets.order_by('-sequence').values_list(
                'sequence', flat=True).first()
            sequence = (last or 0) + 1
        else:
            try:
                sequence = int(sequence)
            except (TypeError, ValueError):
                raise TelemetryError('sequence must be an integer.')
            if sequence < 1:
                raise TelemetryError('sequence starts at 1.')

        previous = session.packets.order_by('-sequence').values_list(
            'chain_hash', flat=True).first() or ''

        with transaction.atomic():
            if session.packets.filter(sequence=sequence).exists():
                raise TelemetryError(
                    f'Packet {sequence} has already been received for this '
                    'session. Packets are append-only — start a new session to '
                    're-capture this row.',
                    status_code=409,
                )
            packet = TelemetryPacket.objects.create(
                session=session,
                sequence=sequence,
                payload=payload,
                previous_hash=previous,
                chain_hash=chain_hash(previous, sequence, payload),
                recorded_at=recorded_at,
            )
            TelemetrySession.objects.filter(pk=session.pk).update(
                packet_count=session.packets.count())
        session.refresh_from_db(fields=['packet_count'])
        return packet

    # ------------------------------------------------------------------
    # Promotion
    # ------------------------------------------------------------------

    #: data_type -> the applier that writes it into the real registry.
    APPLIERS = {
        'gpr': '_promote_gpr',
        'pundit': '_promote_pundit',
        'gnss': '_promote_gnss',
        'scan': '_promote_scan',
    }

    @classmethod
    def end_session(cls, session, request):
        """Close the session and promote its packets into the registry.

        Refused only when the session is already SYNCED — re-promoting it
        would duplicate every row — or when it was aborted.

        A session that is already ENDED but still PENDING is promoted
        normally, and that is not a loophole: ``status`` and ``sync_status``
        are independent axes, and ENDED+PENDING is precisely the state a
        capture that has finished but not yet reached the registry is in. A
        capture imported from an instrument export is born in it. Refusing
        that combination would make the documented state unreachable from the
        one endpoint that exists to leave it.

        A FAILED promotion is likewise retried in place, because the
        alternative would be to re-capture measurements the device has already
        sent.
        """
        if session.status == TelemetrySession.STATUS_ABORTED:
            raise TelemetryError(
                f'Session {session.session_reference} was aborted.', status_code=409)
        if session.sync_status == TelemetrySession.SYNC_SYNCED:
            raise TelemetryError(
                f'Session {session.session_reference} has already been '
                f'promoted. Promoting it again would write a second copy of '
                f'every row into the registry.',
                status_code=409,
            )

        packets = list(session.packets.order_by('sequence'))
        if not packets:
            raise TelemetryError(
                'This session received no packets, so there is nothing to '
                'promote. Abort it instead if the capture was abandoned.')

        applier = getattr(cls, cls.APPLIERS[session.data_type])

        envelope = {
            'session_reference': session.session_reference,
            'device': session.device.device_id,
            'project': str(session.project_id),
            'data_type': session.data_type,
            'session_config': session.session_config,
            'packets': [
                {
                    'sequence': p.sequence,
                    'payload': p.payload,
                    'recorded_at': p.recorded_at.isoformat() if p.recorded_at else None,
                }
                for p in packets
            ],
        }

        try:
            with transaction.atomic():
                promoted = applier(session, packets, request)
                session.status = TelemetrySession.STATUS_ENDED
                session.session_end = session.session_end or timezone.now()
                session.data_payload = envelope
                session.sha256_hash = sha256_hex(canonical_json(envelope))
                session.sync_status = TelemetrySession.SYNC_SYNCED
                session.sync_error = ''
                session.promoted_at = timezone.now()
                session.save(update_fields=[
                    'status', 'session_end', 'data_payload', 'sha256_hash',
                    'sync_status', 'sync_error', 'promoted_at', 'updated_at',
                ])
        except Exception as exc:  # noqa: BLE001 — every failure is recorded
            # Nothing above survives: the transaction rolled back, so no survey,
            # no anomalies and no evidence records exist. The session records
            # what happened so the operator can act on it.
            reason = _describe(exc)
            TelemetrySession.objects.filter(pk=session.pk).update(
                status=TelemetrySession.STATUS_ENDED,
                session_end=session.session_end or timezone.now(),
                sync_status=TelemetrySession.SYNC_FAILED,
                sync_error=reason[:2000],
                updated_at=timezone.now(),
            )
            session.refresh_from_db()
            logger.warning('Telemetry promotion failed for %s: %s',
                           session.session_reference, reason)
            raise TelemetryError(
                f'Promotion failed — nothing was written to the registry. {reason}')

        session.refresh_from_db()
        return promoted

    # ------------------------------------------------------------------
    # Per-sensor appliers
    #
    # Each one runs inside the caller's transaction and writes through the
    # real serializers, so a telemetry capture is validated exactly as a
    # manual entry is — there is no second, looser write path.
    # ------------------------------------------------------------------

    @classmethod
    def _context(cls, session, request):
        """Serializer context for a promotion.

        The digital_eye serializers scope their project FK through
        ``ScopedProjectField``, which reads ``context['request'].user``. A
        promotion triggered from a retry sweep has no HTTP request, so the
        session's own operator stands in. That is not a widening of
        authority: the project still comes from the session row, which was
        scope-checked when the session was opened, and this shim can only
        ever admit a project that row already names.
        """
        if request is not None:
            return {'request': request}
        return {'request': _SessionPromotionContext(session.operator)}

    @classmethod
    def _run(cls, serializer_class, data, session, request, **save_kwargs):
        serializer = serializer_class(data=data,
                                      context=cls._context(session, request))
        serializer.is_valid(raise_exception=True)
        return serializer.save(**save_kwargs)

    @classmethod
    def _promote_gpr(cls, session, packets, request):
        """One GPR survey plus one anomaly row per packet."""
        from apps.digital_eye.serializers import (
            GPRAnomalySerializer, GPRSurveySerializer,
        )
        from apps.evidence.ingestion import EvidenceIngestionService

        config = dict(session.session_config or {})
        config.setdefault('title', f'Telemetry capture {session.session_reference}')

        survey = cls._run(
            GPRSurveySerializer,
            {**config, 'project': session.project_id, 'device': session.device_id},
            session, request,
            created_by=request.user if request else None,
            operator=request.user if request else None,
            operator_name=session.operator_name,
            status='completed',
            completed_at=timezone.now(),
        )

        anomalies = []
        for packet in packets:
            anomaly = cls._run(
                GPRAnomalySerializer,
                {**packet.payload, 'survey': survey.id},
                session, request,
            )
            anomalies.append(anomaly)
            # Normalise into the Evidence Registry, exactly as the manual
            # create path does.
            EvidenceIngestionService.ingest_gpr_anomaly(
                anomaly, ingested_by=request.user if request else None)

        return {'survey_id': str(survey.id),
                'survey_reference': survey.survey_reference,
                'anomalies': len(anomalies)}

    @classmethod
    def _reparse_file_packets(cls, session):
        """Re-read and re-parse the stored original file to recover per-element
        grouping for legacy sessions whose packets lack context keys.

        Returns a list of ``(context_dict, [reading_dict, ...])`` groups — the
        same shape ``_promote_pundit`` builds from well-formed packets — or
        ``None`` when the file cannot be recovered.

        The original bytes are retained in storage precisely for this: the
        packets are a *derived interpretation* of a file, and if that
        interpretation was wrong the original is the only ground truth. A
        legacy session imported before the multi-element fix is that case — the
        parse was not wrong per se, but it dropped the per-element context that
        promotion now needs.
        """
        from django.core.files.storage import default_storage

        from apps.data_import.readers import (
            ImportReadError, detect_import_type, read_rows,
        )
        from apps.data_import.registry import (
            REGISTRY, RowError, UPV_CONTEXT_KEYS, group_upv_rows,
        )
        from apps.telemetry.export_import import (
            SessionFromFileService, _BuildContext,
        )

        storage_name = session.source_file_storage_name
        if not storage_name:
            return None

        try:
            with default_storage.open(storage_name) as handle:
                content = handle.read()
        except Exception:  # noqa: BLE001
            logger.warning(
                'Cannot re-read stored file %s for session %s',
                storage_name, session.session_reference)
            return None

        try:
            import_type = detect_import_type(content)
            header_map = (session.device.column_mapping or None
                          if session.device else None)
            rows, _ = read_rows(content, import_type, header_map=header_map)
            if not rows:
                return None

            # Merge session_config context into the rows, exactly as the
            # current file-import path does.
            config = dict(session.session_config or {})
            merged = SessionFromFileService._merge_context(rows, config)
            groups, error = group_upv_rows(merged)
            if error:
                return None

            result = []
            for _key, group_rows in groups:
                payload, _ = REGISTRY['UPV'].build(
                    group_rows, _BuildContext(session.project))
                readings = payload.pop('readings', [])
                payload.pop('project', None)
                context = {k: v for k, v in payload.items()
                           if k in UPV_CONTEXT_KEYS}
                result.append((context, readings))
            return result if result else None

        except (ImportReadError, RowError, Exception):  # noqa: BLE001
            logger.warning(
                'Re-parse failed for session %s: stored file may be corrupt',
                session.session_reference, exc_info=True)
            return None

    @classmethod
    def _promote_pundit(cls, session, packets, request):
        """One PUNDIT test per element the capture holds.

        The readings are handed to the serializer as its own ``readings`` list
        rather than written directly, so the per-point velocity, the element
        mean, the quality grade, the E.C.S through the project's active curve
        and the point-count threading all happen in the one place that already
        implements them.

        A capture is not always one test. An instrument export is a day's work
        and routinely holds every element the operator walked, so the packets of
        such a file carry the context of the test they belong to and are
        regrouped here exactly as the importer split them.

        Only a *file* capture is regrouped, and that restriction is deliberate
        rather than incidental. A file is a finished export whose packets were
        grouped by the importer from rows the operator can read for themselves.
        A live or replayed session declares its element once, in
        ``session_config``, at the moment it starts — and a device naming an
        element on every packet while its operator named one at the start is a
        disagreement this method has no business settling by quietly splitting
        the capture into two tests.
        """
        from apps.data_import.registry import UPV_CONTEXT_KEYS
        from apps.digital_eye.serializers import PUNDITTestSerializer
        from apps.evidence.ingestion import EvidenceIngestionService

        config = dict(session.session_config or {})
        regroup = session.transport == TelemetrySession.TRANSPORT_FILE

        # Consecutive packets of one context are one test — the rule the
        # importer applies to the rows, and for the same reason: an element
        # seen in two separate blocks is a sorting accident, not two tests.
        groups = []
        for packet in packets:
            payload = dict(packet.payload or {})
            context = ({key: payload[key] for key in UPV_CONTEXT_KEYS
                        if key in payload} if regroup else {})
            key = tuple(sorted(context.items()))
            if groups and groups[-1][0] == key:
                groups[-1][1].append(payload)
                continue
            groups.append((key, [payload]))

        # Legacy file sessions imported before the multi-element fix carry no
        # per-element context on their packets, so every reading lands in one
        # group and fails on duplicate point labels. Detect this degenerate
        # case and recover by re-parsing the stored original file.
        if (regroup and len(groups) == 1
                and groups[0][0] == ()
                and len(groups[0][1]) > 1):
            labels = [(p.get('point_label') or '').strip().upper()
                      for p in groups[0][1]]
            has_dupes = len(labels) != len(set(l for l in labels if l))
            if has_dupes:
                reparsed = cls._reparse_file_packets(session)
                if reparsed:
                    logger.info(
                        'Recovered per-element grouping for legacy session %s '
                        '(%d groups from re-parse)',
                        session.session_reference, len(reparsed))
                    # Build the promotion from the fresh parse instead of the
                    # context-less packets.
                    return cls._promote_pundit_from_groups(
                        session, reparsed, config, request)

        # Build test groups into the promotion result — the normal path for
        # well-formed packets and the fallback for legacy packets that could
        # not be recovered.
        promotion_groups = []
        for key, members in groups:
            readings = []
            for payload in members:
                reading = {name: value for name, value in payload.items()
                           if name not in UPV_CONTEXT_KEYS}
                reading.setdefault('point_label', '')
                readings.append(reading)
            promotion_groups.append((dict(key), readings))

        return cls._promote_pundit_from_groups(
            session, promotion_groups, config, request)

    @classmethod
    def _promote_pundit_from_groups(cls, session, groups, config, request):
        """Write one PUNDIT test per group into the registry.

        ``groups`` is a list of ``(context_dict, [reading_dict, ...])`` pairs —
        produced either from the packets' own context keys (the normal path) or
        from a re-parse of the stored file (the legacy recovery path).
        """
        from apps.digital_eye.serializers import PUNDITTestSerializer
        from apps.evidence.ingestion import EvidenceIngestionService

        tests = []
        total = 0
        for context, readings in groups:
            for reading in readings:
                # Point labels are left to the serializer's own A, B, C... rule
                # when the device did not assign one — the same rule the manual
                # multi-point form uses.
                reading.setdefault('point_label', '')

            test_config = {**config, **context}
            test_config.setdefault(
                'structural_element',
                f'Telemetry capture {session.session_reference}')

            test = cls._run(
                PUNDITTestSerializer,
                {**test_config,
                 'project': session.project_id,
                 'device': session.device_id,
                 'readings': readings},
                session, request,
                created_by=request.user if request else None,
                operator=request.user if request else None,
                operator_name=session.operator_name,
            )
            EvidenceIngestionService.ingest_pundit_test(
                test, ingested_by=request.user if request else None)

            count = test.readings.count()
            total += count
            tests.append({
                'test_id': str(test.id),
                'test_reference': test.test_reference,
                'structural_element': test.structural_element,
                'floor': test.floor,
                'readings': count,
            })

        first = tests[0] if tests else {}
        return {
            'tests': tests,
            'test_count': len(tests),
            # Kept because a promotion used to be reportable as a single test
            # and a live capture still is one. A caller reading only these sees
            # the capture's first test rather than nothing at all.
            'test_id': first.get('test_id', ''),
            'test_reference': first.get('test_reference', ''),
            'readings': total,
        }

    @classmethod
    def _promote_gnss(cls, session, packets, request):
        """One GNSS survey whose boundary points are the packets."""
        from apps.digital_eye.serializers import (
            GnssBoundaryPointSerializer, GnssSurveySerializer,
        )
        from apps.evidence.ingestion import EvidenceIngestionService

        config = dict(session.session_config or {})
        config.setdefault('title', f'Telemetry capture {session.session_reference}')

        survey = cls._run(
            GnssSurveySerializer,
            {**config, 'project': session.project_id, 'device': session.device_id},
            session, request,
            created_by=request.user if request else None,
            operator=request.user if request else None,
            operator_name=session.operator_name,
            status='completed',
            completed_at=timezone.now(),
        )

        points = []
        for packet in packets:
            row = dict(packet.payload)
            row.setdefault('sequence', packet.sequence)
            if packet.recorded_at and not row.get('captured_at'):
                row['captured_at'] = packet.recorded_at
            points.append(cls._run(
                GnssBoundaryPointSerializer, {**row, 'survey': survey.id},
                session, request))

        EvidenceIngestionService.ingest_gnss_survey(
            survey, ingested_by=request.user if request else None)

        return {'survey_id': str(survey.id),
                'survey_reference': survey.survey_reference,
                'boundary_points': len(points)}

    @classmethod
    def _promote_scan(cls, session, packets, request):
        """One scan session whose defects are the packets."""
        from apps.scans.models import Defect
        from apps.scans.serializers import DefectSerializer
        from apps.scans.services import ScanService
        from apps.evidence.ingestion import EvidenceIngestionService

        scanner_id = (session.session_config or {}).get(
            'scanner_id') or session.device.device_id
        scan_session = ScanService.start_session({
            'project_id': session.project_id,
            'scanner_id': scanner_id,
            'timestamp': session.session_start,
        })

        defects = []
        for packet in packets:
            # `session` is read-only on DefectSerializer (the API derives it
            # from the URL), so it is supplied at save() rather than in data.
            defect = cls._run(
                DefectSerializer, dict(packet.payload), session, request,
                session=scan_session)
            defects.append(defect)
            EvidenceIngestionService.ingest_defect(
                defect, ingested_by=request.user if request else None)

        return {'scan_session_id': str(scan_session.id),
                'scanner_id': scanner_id,
                'defects': len(defects)}
