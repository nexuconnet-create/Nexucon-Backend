"""
Tests for the offline sync queue (Inspector PWA Part 3).

The queue exists so a field write survives a dead connection. Everything worth
testing follows from that, and almost all of it is about the two ways a replay
goes wrong:

  * **A duplicate apply.** The same write landing twice. Tested by replaying a
    synced item and asserting the target row count does not move, and by
    asserting that a second caller cannot claim an item the first has taken.
  * **A swallowed write.** A queued item that is accepted and then never
    applied, or applied and then dropped when it fails. Tested by driving an
    item to the retry cap and asserting it is still present, still carrying its
    payload, and reported as `exhausted`.

The third theme is honesty at enqueue: the queue refuses work it cannot do
rather than parking it, so `DELETE` is a 400 while the inspector is still on
the screen — not a failure discovered on the next reconnect.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.digital_eye.models import FieldDevice, GPRSurvey
from apps.evidence.models import EvidenceRecord
from apps.inspections.models import Finding, Inspection, StopWorkOrder
from apps.projects.models import Project
from apps.telemetry.models import TelemetrySession
from apps.telemetry.services import TelemetryService

from .appliers import REGISTRY
from .models import SyncQueueItem
from .services import SyncError, SyncService

User = get_user_model()


class SyncTestBase(TestCase):
    def setUp(self):
        # Superuser: `scoped_projects` returns every project for one, and the
        # appliers scope every write through it. A bare user with no
        # government Profile is scoped to nothing by design, which would make
        # every one of these tests fail for the wrong reason.
        self.user = User.objects.create_superuser(
            username='field_sync@nexucon.com', email='field_sync@nexucon.com',
            password='Password123!', first_name='Sade', last_name='Okoro',
        )
        self.project = Project.objects.create(name='Sync Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='GPR-SYNC-001', device_type='gpr',
            assigned_project=self.project, is_active=True,
        )

    def _enqueue(self, client_item_id='item-1', entity_type='INSPECTION',
                 action='CREATE', payload=None, entity_id='', user=None):
        return SyncService.enqueue(
            user=user or self.user, client_item_id=client_item_id,
            entity_type=entity_type, action=action,
            payload=payload if payload is not None else {
                'project': str(self.project.id),
                'inspection_type': 'Foundation Inspection',
            },
            entity_id=entity_id,
        )


class EnqueueContractTests(SyncTestBase):
    """What the queue accepts, and what it refuses while the client is watching."""

    def test_a_new_item_is_journalled_as_pending(self):
        item, created = self._enqueue()
        self.assertTrue(created)
        self.assertEqual(item.sync_status, SyncQueueItem.STATUS_PENDING)
        self.assertEqual(item.retry_count, 0)
        self.assertEqual(item.inspector, self.user)
        self.assertTrue(item.payload_hash)

    def test_the_payload_hash_is_derived_and_not_client_supplied(self):
        item, _ = self._enqueue(payload={
            'project': str(self.project.id),
            'inspection_type': 'Foundation Inspection', 'summary_notes': 'x'})
        other, _ = self._enqueue(client_item_id='item-2', payload={
            'summary_notes': 'x',
            'inspection_type': 'Foundation Inspection',
            'project': str(self.project.id)})
        # Same content, different key order — the same hash. Key order is not
        # content, and a hash that changed with it would make every retry from
        # a JSON serializer that reorders keys look like a conflict.
        self.assertEqual(item.payload_hash, other.payload_hash)

    def test_delete_is_refused_at_enqueue_not_on_the_next_reconnect(self):
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(entity_type='INSPECTION', action='DELETE')
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('CREATE', str(ctx.exception))
        self.assertEqual(SyncQueueItem.objects.count(), 0)

    def test_delete_is_refused_for_every_entity_type(self):
        """Not a role restriction — no applier implements deletion, and a queue
        that accepts work it can never do is a trap."""
        for entity_type in REGISTRY:
            with self.assertRaises(SyncError):
                self._enqueue(client_item_id=f'del-{entity_type}',
                              entity_type=entity_type, action='DELETE')

    def test_an_unknown_entity_type_is_refused(self):
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(entity_type='MAGIC_BEAM')
        self.assertIn('not a queueable entity type', str(ctx.exception))

    def test_a_payload_missing_its_required_keys_is_refused_at_enqueue(self):
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(payload={'inspection_type': 'Foundation Inspection'})
        self.assertIn('project', str(ctx.exception))

    def test_an_update_without_an_entity_id_is_refused(self):
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(action='UPDATE', payload={'summary_notes': 'x'})
        self.assertIn('entity_id', str(ctx.exception))

    def test_a_telemetry_item_can_only_be_created(self):
        with self.assertRaises(SyncError):
            self._enqueue(
                entity_type='TELEMETRY', action='UPDATE',
                entity_id='11111111-1111-1111-1111-111111111111',
                payload={'device': str(self.device.id),
                         'project': str(self.project.id),
                         'data_type': 'gpr', 'packets': []})

    def test_a_telemetry_item_needs_packets(self):
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(entity_type='TELEMETRY', payload={
                'device': str(self.device.id),
                'project': str(self.project.id), 'data_type': 'gpr'})
        self.assertIn('packets', str(ctx.exception))


class EnqueueIdempotencyTests(SyncTestBase):
    """The same key twice: free when identical, loud when not."""

    def test_the_same_id_and_payload_is_a_retry_not_a_second_write(self):
        first, created_first = self._enqueue()
        second, created_second = self._enqueue()
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SyncQueueItem.objects.count(), 1)

    def test_the_same_id_with_a_different_payload_is_a_conflict(self):
        item, _ = self._enqueue(payload={
            'project': str(self.project.id),
            'inspection_type': 'Foundation Inspection'})

        with self.assertRaises(SyncError) as ctx:
            self._enqueue(payload={
                'project': str(self.project.id),
                'inspection_type': 'Final Clearance'})

        self.assertEqual(ctx.exception.status_code, 409)
        # The stored row is untouched. Overwriting it is how a replay becomes
        # data loss: the device believes what it sent is queued.
        item.refresh_from_db()
        self.assertEqual(item.payload['inspection_type'], 'Foundation Inspection')
        self.assertEqual(SyncQueueItem.objects.count(), 1)

    def test_the_same_id_with_a_different_action_is_a_conflict(self):
        """Same id, same payload, different verb — still a conflict. The device
        is describing two different operations under one key, and guessing
        which one it meant is how a create becomes an update of the wrong row."""
        self._enqueue()
        with self.assertRaises(SyncError) as ctx:
            self._enqueue(action='UPDATE',
                          entity_id='11111111-1111-1111-1111-111111111111')
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(SyncQueueItem.objects.get().action, 'CREATE')

    def test_the_same_client_item_id_from_two_inspectors_is_not_a_collision(self):
        """The key is scoped per inspector, so one device's ids can never
        collide with — or overwrite — another's queued work."""
        other = User.objects.create_superuser(
            username='other_sync@nexucon.com', email='other_sync@nexucon.com',
            password='Password123!')
        first, created_first = self._enqueue(client_item_id='shared-key')
        second, created_second = self._enqueue(client_item_id='shared-key',
                                               user=other)
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(SyncQueueItem.objects.count(), 2)


class ProcessInspectionsTests(SyncTestBase):
    """Applying a queued write lands a real row through the real serializer."""

    def _process(self):
        return SyncService.process(user=self.user, request=None)

    def test_a_queued_inspection_lands_as_a_real_row(self):
        self._enqueue()
        results = self._process()
        self.assertEqual(results['synced'], 1)
        self.assertEqual(results['failed'], 0)

        inspection = Inspection.objects.get()
        self.assertEqual(inspection.project, self.project)
        self.assertEqual(inspection.inspection_type, 'Foundation Inspection')
        # The inspector comes from the authenticated caller, never the payload.
        self.assertEqual(inspection.inspector, self.user)
        self.assertEqual(inspection.inspector_name, 'Sade Okoro')
        self.assertTrue(inspection.inspection_reference)

        item = SyncQueueItem.objects.get()
        self.assertEqual(item.sync_status, SyncQueueItem.STATUS_SYNCED)
        self.assertEqual(item.target_model, 'inspections.Inspection')
        self.assertEqual(item.target_id, str(inspection.id))
        self.assertIsNotNone(item.synced_at)

    def test_the_payload_cannot_name_a_different_inspector(self):
        """A client that could set `inspector` could file an inspection under a
        colleague's badge."""
        other = User.objects.create_user(
            username='someone_else@nexucon.com', email='someone_else@nexucon.com',
            password='Password123!')
        self._enqueue(payload={
            'project': str(self.project.id),
            'inspection_type': 'Foundation Inspection',
            'inspector': str(other.id),
            'inspector_name': 'Someone Else',
        })
        self._process()
        inspection = Inspection.objects.get()
        self.assertEqual(inspection.inspector, self.user)
        self.assertEqual(inspection.inspector_name, 'Sade Okoro')

    def test_a_project_outside_the_callers_scope_fails_the_item(self):
        """Refused before the serializer runs, and recorded on the item rather
        than raised at the caller — one bad item must not stop the queue."""
        outsider = User.objects.create_user(
            username='outsider@nexucon.com', email='outsider@nexucon.com',
            password='Password123!')
        owner = User.objects.create_superuser(
            username='owner@nexucon.com', email='owner@nexucon.com',
            password='Password123!')
        item, _ = self._enqueue(client_item_id='scoped', user=owner)

        results = SyncService.process(user=owner, request=None)
        self.assertEqual(results['synced'], 1)

        # Now the same payload from a user who is scoped to nothing.
        foreign, _ = SyncService.enqueue(
            user=outsider, client_item_id='scoped',
            entity_type='INSPECTION', action='CREATE',
            payload={'project': str(self.project.id),
                     'inspection_type': 'Foundation Inspection'})
        results = SyncService.process(user=outsider, request=None)
        self.assertEqual(results['synced'], 0)
        self.assertEqual(results['failed'], 1)
        foreign.refresh_from_db()
        self.assertEqual(foreign.sync_status, SyncQueueItem.STATUS_FAILED)
        self.assertIn('not in your scope', foreign.last_error)
        # Nothing was written for it.
        self.assertEqual(Inspection.objects.count(), 1)

    def test_a_replay_after_sync_leaves_the_target_row_count_unchanged(self):
        self._enqueue()
        self._process()
        self.assertEqual(Inspection.objects.count(), 1)

        # The device flushes again — same id, same payload. Nothing new is
        # queued and nothing new is written.
        item, created = self._enqueue()
        self.assertFalse(created)
        results = self._process()
        self.assertEqual(results['processed'], 0)
        self.assertEqual(Inspection.objects.count(), 1)

    def test_a_malformed_row_fails_only_its_own_item(self):
        self._enqueue(client_item_id='bad', payload={
            'project': str(self.project.id), 'inspection_type': 'Foundation Inspection',
            'priority': 'Not A Priority'})
        self._enqueue(client_item_id='good')

        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)
        self.assertEqual(results['failed'], 1)
        self.assertEqual(Inspection.objects.count(), 1)

        bad = SyncQueueItem.objects.get(client_item_id='bad')
        self.assertEqual(bad.sync_status, SyncQueueItem.STATUS_FAILED)
        self.assertTrue(bad.last_error)
        # A message, never a stack trace — this string is rendered on a phone.
        self.assertNotIn('Traceback', bad.last_error)

    def test_an_update_applies_partially_to_the_named_row(self):
        inspection = Inspection.objects.create(
            project=self.project, inspection_type='Foundation Inspection',
            summary_notes='Original')
        self._enqueue(action='UPDATE', entity_id=str(inspection.id),
                      payload={'summary_notes': 'Corrected on site'})
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)
        inspection.refresh_from_db()
        self.assertEqual(inspection.summary_notes, 'Corrected on site')
        self.assertEqual(Inspection.objects.count(), 1)


class ProcessOtherEntityTests(SyncTestBase):
    def test_a_queued_finding_lands_against_its_inspection(self):
        inspection = Inspection.objects.create(
            project=self.project, inspection_type='Foundation Inspection')
        self._enqueue(
            client_item_id='finding-1', entity_type='FINDING',
            payload={'inspection': str(inspection.id),
                     'title': 'Honeycombing to column C4',
                     'description': 'Exposed aggregate over 300mm.',
                     'severity': 'HIGH', 'category': 'STRUCTURAL'})
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)

        finding = Finding.objects.get()
        self.assertEqual(finding.inspection, inspection)
        # The project is taken from the inspection, not from the payload: a
        # client cannot attach a finding to a project the inspection is not on.
        self.assertEqual(finding.project, self.project)
        self.assertTrue(finding.finding_reference)

    def test_a_queued_stop_work_order_records_the_issuing_inspector(self):
        self._enqueue(
            client_item_id='swo-1', entity_type='STOP_WORK_ORDER',
            payload={'project': str(self.project.id),
                     'reason': 'Unshored excavation adjacent to the boundary.'})
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)
        swo = StopWorkOrder.objects.get()
        self.assertEqual(swo.issued_by_name, 'Sade Okoro')
        self.assertTrue(swo.order_number)

    def test_a_queued_evidence_record_lands_with_a_computed_hash(self):
        self._enqueue(
            client_item_id='ev-1', entity_type='EVIDENCE',
            payload={'project': str(self.project.id),
                     'source_type': 'inspection_finding',
                     'source_model': 'inspections.Finding',
                     'source_id': 'field-photo-0001',
                     'payload': {'caption': 'Column C4 crack'}})
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)

        record = EvidenceRecord.objects.get()
        self.assertEqual(record.source_id, 'field-photo-0001')
        self.assertEqual(record.project, self.project)
        # Written through the ingestion service, so the hash exists. A record
        # with an empty hash claims registry membership it does not have.
        self.assertTrue(record.evidence_hash)
        self.assertEqual(record.evidence_hash, record.compute_hash())

    def test_a_replayed_evidence_record_updates_rather_than_duplicating(self):
        """`(source_model, source_id)` is the evidence registry's own natural
        key, so the second landing is an update at the target as well as a
        dedup at the queue."""
        payload = {'project': str(self.project.id),
                   'source_type': 'inspection_finding',
                   'source_model': 'inspections.Finding',
                   'source_id': 'field-photo-0002',
                   'payload': {'caption': 'first'}}
        self._enqueue(client_item_id='ev-2', entity_type='EVIDENCE',
                      payload=payload)
        SyncService.process(user=self.user, request=None)
        self.assertEqual(EvidenceRecord.objects.count(), 1)

        item = SyncQueueItem.objects.get()
        # Force a re-apply of the same row as though the SYNCED mark were lost.
        item.sync_status = SyncQueueItem.STATUS_PENDING
        item.save(update_fields=['sync_status'])
        SyncService.process(user=self.user, request=None)
        self.assertEqual(EvidenceRecord.objects.count(), 1)

    def test_an_unknown_evidence_source_type_is_refused_not_guessed(self):
        self._enqueue(
            client_item_id='ev-3', entity_type='EVIDENCE',
            payload={'project': str(self.project.id),
                     'source_type': 'gpr_radargram',
                     'source_model': 'inspections.Finding',
                     'source_id': 'x'})
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['failed'], 1)
        item = SyncQueueItem.objects.get()
        self.assertIn('not a recognised evidence type', item.last_error)
        self.assertEqual(EvidenceRecord.objects.count(), 0)


class ProcessTelemetryTests(SyncTestBase):
    """A whole offline capture, replayed whole."""

    GPR_CONFIG = {
        'title': 'Foundation Zone B', 'survey_area': 'Grid 4-7',
        'structural_element': 'Raft Slab', 'antenna_frequency_mhz': 400,
        'depth_range_m': 2.5, 'latitude': 6.4281, 'longitude': 3.4219,
    }

    def _telemetry_payload(self, **overrides):
        payload = {
            'device': str(self.device.id),
            'project': str(self.project.id),
            'data_type': 'gpr',
            'session_config': dict(self.GPR_CONFIG),
            'packets': [
                {'sequence': 1, 'payload': {
                    'anomaly_type': 'void', 'severity': 'high',
                    'depth_m': 0.8, 'estimated_size_m': 1.2, 'confidence': 0.82}},
                {'sequence': 2, 'payload': {
                    'anomaly_type': 'rebar', 'severity': 'low',
                    'depth_m': 0.35, 'estimated_size_m': 0.4, 'confidence': 0.91}},
            ],
        }
        payload.update(overrides)
        return payload

    def test_a_whole_offline_capture_promotes_into_the_registry(self):
        self._enqueue(client_item_id='tel-1', entity_type='TELEMETRY',
                      payload=self._telemetry_payload())
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)
        self.assertEqual(results['failed'], 0)

        self.assertEqual(GPRSurvey.objects.count(), 1)
        self.assertEqual(GPRSurvey.objects.get().anomalies.count(), 2)
        # And normalised into the evidence registry, one row per anomaly,
        # exactly as the live path does it.
        self.assertEqual(
            EvidenceRecord.objects.filter(source_type='gpr').count(), 2)

        item = SyncQueueItem.objects.get()
        self.assertEqual(item.target_model, 'telemetry.TelemetrySession')
        self.assertTrue(item.target_id)

    def test_a_failed_promotion_leaves_the_item_retryable_and_writes_nothing(self):
        payload = self._telemetry_payload()
        # A packet the GPR anomaly serializer will refuse.
        payload['packets'][1]['payload'] = {'anomaly_type': 'void',
                                            'depth_m': 'not-a-number'}
        self._enqueue(client_item_id='tel-2', entity_type='TELEMETRY',
                      payload=payload)

        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['failed'], 1)
        # All-or-nothing: not one GPR row, not one evidence row.
        self.assertEqual(GPRSurvey.objects.count(), 0)
        self.assertEqual(EvidenceRecord.objects.count(), 0)

        item = SyncQueueItem.objects.get()
        self.assertEqual(item.sync_status, SyncQueueItem.STATUS_FAILED)
        self.assertLess(item.retry_count, item.max_retries)
        # A retry sees a claimable item, not an exhausted one.
        self.assertEqual(
            SyncService.claimable(self.user).filter(pk=item.pk).count(), 1)

    def test_a_retry_resumes_the_open_session_instead_of_starting_a_second(self):
        """The device has already handed over its packets. A retry must not ask
        for them again, and must not leave an orphaned OPEN session behind —
        one OPEN session per device is the rule the live path enforces, so an
        orphan would block the instrument from ever capturing again.

        The state below is what a previous attempt leaves when it opened a
        session and recorded a packet before failing: the queue row is still
        unmarked, and the session is still OPEN holding what already arrived.
        """
        item, _ = self._enqueue(client_item_id='tel-3', entity_type='TELEMETRY',
                                payload=self._telemetry_payload())
        session = TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type='gpr', session_config=dict(self.GPR_CONFIG))
        TelemetryService.append_packet(
            session, self._telemetry_payload()['packets'][0]['payload'],
            sequence=1)
        item.target_id = str(session.id)
        item.save(update_fields=['target_id'])

        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['synced'], 1)

        # One session — the same one — holding both packets. The first was
        # skipped as already-received, not appended twice.
        self.assertEqual(TelemetrySession.objects.count(), 1)
        self.assertEqual(str(TelemetrySession.objects.get().id), str(session.id))
        self.assertEqual(session.packets.count(), 2)
        self.assertEqual(GPRSurvey.objects.get().anomalies.count(), 2)

    def test_a_replay_after_promotion_does_not_promote_a_second_time(self):
        """The narrow window this closes: the rows landed but the queue row did
        not get marked. Re-applying would duplicate every anomaly."""
        item, _ = self._enqueue(client_item_id='tel-4', entity_type='TELEMETRY',
                                payload=self._telemetry_payload())
        SyncService.process(user=self.user, request=None)
        self.assertEqual(GPRSurvey.objects.count(), 1)

        session = TelemetrySession.objects.get()
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_SYNCED)

        item.refresh_from_db()
        item.sync_status = SyncQueueItem.STATUS_PENDING
        item.save(update_fields=['sync_status'])
        SyncService.process(user=self.user, request=None)

        self.assertEqual(TelemetrySession.objects.count(), 1)
        self.assertEqual(GPRSurvey.objects.count(), 1)
        self.assertEqual(EvidenceRecord.objects.filter(source_type='gpr').count(), 2)


class AtomicClaimTests(SyncTestBase):
    """Two flushes at once must not both apply the same item."""

    def test_a_second_caller_cannot_claim_an_item_the_first_has_taken(self):
        item, _ = self._enqueue()
        self.assertTrue(SyncService._claim(item))
        # The row is now PROCESSING and freshly claimed, so the second caller's
        # conditional UPDATE matches nothing. This is the whole mechanism.
        self.assertFalse(SyncService._claim(item))
        self.assertEqual(
            SyncService.claimable(self.user).filter(pk=item.pk).count(), 0)

    def test_a_stale_processing_claim_is_reclaimable(self):
        """A worker killed mid-apply leaves its claim behind. Without a timeout
        the item would be stuck forever — the same loss as dropping it."""
        item, _ = self._enqueue()
        SyncService._claim(item)
        SyncQueueItem.objects.filter(pk=item.pk).update(
            claimed_at=timezone.now() - datetime.timedelta(hours=1))
        item.refresh_from_db()
        self.assertTrue(item.claim_is_stale)
        self.assertEqual(
            SyncService.claimable(self.user).filter(pk=item.pk).count(), 1)

    def test_a_claimed_item_that_is_still_fresh_is_not_reported(self):
        item, _ = self._enqueue()
        SyncService._claim(item)
        item.refresh_from_db()
        self.assertFalse(item.claim_is_stale)

    def test_an_item_held_by_a_live_claim_is_left_alone(self):
        """A fresh PROCESSING claim belongs to another caller. It is neither
        applied here nor counted as a failure — the row is simply not ours."""
        item, _ = self._enqueue()
        SyncQueueItem.objects.filter(pk=item.pk).update(
            sync_status=SyncQueueItem.STATUS_PROCESSING,
            claimed_at=timezone.now())
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['processed'], 0)
        self.assertEqual(results['synced'], 0)
        self.assertEqual(results['failed'], 0)
        self.assertEqual(Inspection.objects.count(), 0)


class ExhaustionTests(SyncTestBase):
    """A failing item is reported, never quietly dropped."""

    def _bad_item(self, client_item_id='exhaust-me'):
        item, _ = self._enqueue(client_item_id=client_item_id, payload={
            'project': str(self.project.id), 'inspection_type': 'Foundation Inspection',
            'priority': 'Not A Priority'})
        return item

    @override_settings(SYNC_MAX_RETRIES=2)
    def test_an_item_is_retried_until_the_cap_then_left_alone(self):
        item = self._bad_item()

        SyncService.process(user=self.user, request=None)
        item.refresh_from_db()
        self.assertEqual(item.retry_count, 1)
        self.assertFalse(item.exhausted)

        SyncService.process(user=self.user, request=None)
        item.refresh_from_db()
        self.assertEqual(item.retry_count, 2)
        self.assertTrue(item.exhausted)

        # Past the cap it is no longer claimable…
        self.assertEqual(
            SyncService.claimable(self.user).filter(pk=item.pk).count(), 0)
        results = SyncService.process(user=self.user, request=None)
        self.assertEqual(results['processed'], 0)
        item.refresh_from_db()
        self.assertEqual(item.retry_count, 2)

    @override_settings(SYNC_MAX_RETRIES=1)
    def test_an_exhausted_item_keeps_its_payload_and_its_error(self):
        item = self._bad_item()
        SyncService.process(user=self.user, request=None)
        item.refresh_from_db()
        self.assertTrue(item.exhausted)
        self.assertEqual(item.payload['priority'], 'Not A Priority')
        self.assertTrue(item.last_error)
        self.assertEqual(SyncQueueItem.objects.count(), 1)

    @override_settings(SYNC_MAX_RETRIES=1)
    def test_an_exhausted_item_can_be_retried_on_request(self):
        item = self._bad_item()
        SyncService.process(user=self.user, request=None)
        item.refresh_from_db()
        self.assertTrue(item.exhausted)

        # The client corrects the payload and asks for it explicitly.
        item.payload = {'project': str(self.project.id),
                        'inspection_type': 'Foundation Inspection'}
        item.save(update_fields=['payload'])
        results = SyncService.process(user=self.user, request=None,
                                      include_exhausted=True)
        item.refresh_from_db()
        self.assertEqual(results['synced'], 1)
        self.assertEqual(item.sync_status, SyncQueueItem.STATUS_SYNCED)
        self.assertEqual(Inspection.objects.count(), 1)


class FifoTests(SyncTestBase):
    """Order is load-bearing: an UPDATE replayed before its CREATE is corruption."""

    def test_items_are_processed_oldest_first(self):
        """Queued in one offline burst, so `queued_at` is identical across them
        and only the monotonic id separates them."""
        ids = []
        for index in range(5):
            item, _ = self._enqueue(client_item_id=f'fifo-{index}')
            ids.append(item.pk)
        self.assertEqual(ids, sorted(ids))

        seen = list(SyncService.claimable(self.user).values_list('id', flat=True))
        self.assertEqual(seen, ids)

    def test_order_holds_across_mixed_entity_types(self):
        self._enqueue(client_item_id='a', entity_type='INSPECTION')
        self._enqueue(client_item_id='b', entity_type='STOP_WORK_ORDER',
                      payload={'project': str(self.project.id),
                               'reason': 'Unsafe excavation.'})
        self._enqueue(client_item_id='c', entity_type='EVIDENCE',
                      payload={'project': str(self.project.id),
                               'source_type': 'inspection',
                               'source_model': 'inspections.Inspection',
                               'source_id': 'fifo-evidence'})

        order = list(SyncService.claimable(self.user).values_list(
            'client_item_id', flat=True))
        self.assertEqual(order, ['a', 'b', 'c'])

        SyncService.process(user=self.user, request=None)
        self.assertEqual(Inspection.objects.count(), 1)
        self.assertEqual(StopWorkOrder.objects.count(), 1)
        self.assertEqual(EvidenceRecord.objects.count(), 1)


class SyncStatusTests(SyncTestBase):
    def test_last_synced_at_is_none_before_anything_has_synced(self):
        """Never `now()`. A sync indicator that reads "synced just now" before
        anything has ever synced is the defect this endpoint replaces."""
        self._enqueue()
        payload = SyncService.status(user=self.user)
        self.assertIsNone(payload['last_synced_at'])
        self.assertEqual(payload['pending'], 1)
        self.assertEqual(payload['synced'], 0)
        self.assertIsNotNone(payload['oldest_pending_at'])

    def test_last_synced_at_is_a_real_timestamp_once_something_lands(self):
        self._enqueue()
        self.assertIsNone(SyncService.status(user=self.user)['last_synced_at'])
        SyncService.process(user=self.user, request=None)
        after = SyncService.status(user=self.user)
        self.assertIsNotNone(after['last_synced_at'])
        self.assertEqual(after['synced'], 1)
        self.assertEqual(after['pending'], 0)

    def test_counts_are_per_inspector(self):
        other = User.objects.create_superuser(
            username='other_status@nexucon.com',
            email='other_status@nexucon.com', password='Password123!')
        self._enqueue(client_item_id='mine')
        self._enqueue(client_item_id='theirs', user=other)
        self.assertEqual(SyncService.status(user=self.user)['pending'], 1)
        self.assertEqual(SyncService.status(user=other)['pending'], 1)

    @override_settings(SYNC_MAX_RETRIES=1)
    def test_an_exhausted_item_is_reported_with_its_reason(self):
        self._enqueue(payload={'project': str(self.project.id),
                               'inspection_type': 'Foundation Inspection',
                               'priority': 'Not A Priority'})
        SyncService.process(user=self.user, request=None)
        payload = SyncService.status(user=self.user)
        self.assertEqual(payload['failed'], 1)
        self.assertEqual(len(payload['exhausted']), 1)
        entry = payload['exhausted'][0]
        self.assertEqual(entry['client_item_id'], 'item-1')
        self.assertEqual(entry['retry_count'], 1)
        self.assertTrue(entry['last_error'])


class SyncAPITests(APITestCase):
    """The three endpoints, over HTTP."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='api_sync@nexucon.com', email='api_sync@nexucon.com',
            password='Password123!', first_name='Bola', last_name='Ade')
        self.project = Project.objects.create(name='API Sync Site', status='ACTIVE')
        self.client.force_authenticate(self.user)

    def _payload(self, **overrides):
        body = {
            'client_item_id': 'http-1',
            'entity_type': 'INSPECTION',
            'action': 'CREATE',
            'payload': {'project': str(self.project.id),
                        'inspection_type': 'Foundation Inspection'},
        }
        body.update(overrides)
        return body

    def test_queue_creates_an_item(self):
        response = self.client.post(reverse('sync-queue'), self._payload(),
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data['deduplicated'])
        self.assertEqual(response.data['sync_status'], 'PENDING')

    def test_a_retry_returns_200_with_deduplicated(self):
        self.client.post(reverse('sync-queue'), self._payload(), format='json')
        response = self.client.post(reverse('sync-queue'), self._payload(),
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['deduplicated'])
        self.assertEqual(SyncQueueItem.objects.count(), 1)

    def test_a_conflicting_payload_is_a_409(self):
        self.client.post(reverse('sync-queue'), self._payload(), format='json')
        response = self.client.post(
            reverse('sync-queue'),
            self._payload(payload={'project': str(self.project.id),
                                   'inspection_type': 'Final Clearance'}),
            format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(SyncQueueItem.objects.count(), 1)

    def test_delete_is_refused_at_enqueue_over_http(self):
        response = self.client.post(
            reverse('sync-queue'), self._payload(action='DELETE'), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SyncQueueItem.objects.count(), 0)

    def test_process_flushes_and_reports(self):
        self.client.post(reverse('sync-queue'), self._payload(), format='json')
        response = self.client.post(reverse('sync-process'), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['synced'], 1)
        self.assertEqual(response.data['remaining'], 0)
        self.assertEqual(Inspection.objects.count(), 1)

    def test_process_rejects_a_non_integer_limit(self):
        response = self.client.post(reverse('sync-process'), {'limit': 'many'},
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_reports_the_queue(self):
        self.client.post(reverse('sync-queue'), self._payload(), format='json')
        response = self.client.get(reverse('sync-status'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['pending'], 1)
        self.assertIsNone(response.data['last_synced_at'])

    def test_an_anonymous_caller_cannot_touch_the_queue(self):
        self.client.force_authenticate(None)
        for name in ('sync-queue', 'sync-status'):
            response = self.client.get(reverse(name))
            self.assertIn(response.status_code,
                          (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_one_inspector_cannot_see_another_inspectors_queue(self):
        self.client.post(reverse('sync-queue'), self._payload(), format='json')

        other = User.objects.create_superuser(
            username='nosy@nexucon.com', email='nosy@nexucon.com',
            password='Password123!')
        self.client.force_authenticate(other)
        response = self.client.get(reverse('sync-status'))
        self.assertEqual(response.data['pending'], 0)
        # And a flush by the other user applies nothing of the first user's.
        results = self.client.post(reverse('sync-process'), {}, format='json')
        self.assertEqual(results.data['processed'], 0)
        self.assertEqual(Inspection.objects.count(), 0)


class SyncRouteTests(TestCase):
    def test_the_three_spec_routes_resolve(self):
        self.assertEqual(reverse('sync-queue'), '/api/v1/sync/queue/')
        self.assertEqual(reverse('sync-process'), '/api/v1/sync/process/')
        self.assertEqual(reverse('sync-status'), '/api/v1/sync/status/')
