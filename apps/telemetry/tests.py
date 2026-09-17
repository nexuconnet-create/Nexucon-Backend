"""
Tests for the telemetry ingestion envelope (Inspector PWA Part 3).

The behaviour under test is the promotion contract: a session writes nothing
into any statutory registry until /end, and /end is all-or-nothing. The
individual assertions that matter most:

  * a malformed row leaves the session FAILED and writes **zero** registry
    rows — never a partial survey describing only the rows that parsed
  * the packet chain detects a single edited row
  * a re-sent sequence number is refused rather than silently replacing a row
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.digital_eye.models import FieldDevice, GPRSurvey, PUNDITReading, PUNDITTest
from apps.evidence.models import EvidenceRecord
from apps.projects.models import Project
from common.hashing import chain_hash

from .models import TelemetryPacket, TelemetrySession
from .services import TelemetryError, TelemetryService

User = get_user_model()


def _reading_labels(readings):
    return [r.point_label for r in readings]


class TelemetryTestBase(TestCase):
    def setUp(self):
        # Superuser because `scoped_projects` gates the project FK inside the
        # digital_eye serializers that promotion runs through, and a bare user
        # with no government Profile is scoped to nothing by design.
        self.user = User.objects.create_superuser(
            username='telemetry_op@nexucon.com',
            email='telemetry_op@nexucon.com',
            password='Password123!',
            first_name='Tele',
            last_name='Op',
        )
        self.project = Project.objects.create(name='Telemetry Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='GPR-UNIT-001', device_type='gpr',
            assigned_project=self.project, is_active=True,
        )

    def _open(self, data_type='gpr', config=None):
        return TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type=data_type, session_config=config or {},
        )


class TelemetrySessionLifecycleTests(TelemetryTestBase):
    """start / append semantics, before any promotion happens."""

    def test_start_opens_a_pending_session_and_writes_no_registry_rows(self):
        session = self._open()
        self.assertEqual(session.status, TelemetrySession.STATUS_OPEN)
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_PENDING)
        self.assertIsNone(session.data_payload)
        self.assertEqual(session.sha256_hash, '')
        self.assertIsNone(session.session_end)
        # Nothing has been interpreted yet.
        self.assertEqual(GPRSurvey.objects.count(), 0)
        self.assertEqual(EvidenceRecord.objects.count(), 0)

    def test_a_session_start_is_null_when_the_device_reported_none(self):
        """`created_at` is the server's receipt time; `session_start` is the
        device's, and must not be back-filled with the former."""
        session = self._open()
        self.assertIsNone(session.session_start)
        self.assertIsNotNone(session.created_at)

    def test_second_open_session_for_the_same_device_is_refused(self):
        first = self._open()
        with self.assertRaises(TelemetryError) as ctx:
            self._open()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(first.session_reference, str(ctx.exception))

    def test_a_new_session_is_allowed_once_the_previous_one_is_ended(self):
        first = self._open()
        TelemetryService.append_packet(first, {'anomaly_type': 'void'})
        TelemetryService.end_session(first, None)
        second = self._open()
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.status, TelemetrySession.STATUS_OPEN)

    def test_append_chains_each_packet_to_the_previous_one(self):
        session = self._open()
        first = TelemetryService.append_packet(session, {'anomaly_type': 'void'})
        second = TelemetryService.append_packet(session, {'anomaly_type': 'rebar'})

        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.previous_hash, '')
        self.assertEqual(first.chain_hash,
                         chain_hash('', 1, {'anomaly_type': 'void'}))
        self.assertEqual(second.sequence, 2)
        self.assertEqual(second.previous_hash, first.chain_hash)
        self.assertEqual(second.chain_hash,
                         chain_hash(first.chain_hash, 2, {'anomaly_type': 'rebar'}))
        session.refresh_from_db()
        self.assertEqual(session.packet_count, 2)

    def test_verify_chain_passes_on_an_untouched_session(self):
        session = self._open()
        for i in range(3):
            TelemetryService.append_packet(session, {'depth_m': 0.4 * i})
        self.assertTrue(session.verify_chain())

    def test_verify_chain_fails_after_one_stored_packet_is_edited(self):
        session = self._open()
        for i in range(3):
            TelemetryService.append_packet(session, {'depth_m': 0.4 * i})
        packet = session.packets.get(sequence=2)
        packet.payload = {'depth_m': 99.9}
        packet.save(update_fields=['payload'])
        self.assertFalse(session.verify_chain())

    def test_re_sending_a_sequence_number_is_refused_not_overwritten(self):
        session = self._open()
        TelemetryService.append_packet(session, {'depth_m': 1.0}, sequence=1)
        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.append_packet(session, {'depth_m': 2.0}, sequence=1)
        self.assertEqual(ctx.exception.status_code, 409)
        # The original row is untouched — append-only means append-only.
        stored = session.packets.get(sequence=1)
        self.assertEqual(stored.payload, {'depth_m': 1.0})
        self.assertEqual(session.packets.count(), 1)

    def test_appending_to_an_ended_session_is_refused(self):
        session = self._open()
        TelemetryService.append_packet(session, {'anomaly_type': 'void'})
        TelemetryService.end_session(session, None)
        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.append_packet(session, {'anomaly_type': 'rebar'})
        self.assertEqual(ctx.exception.status_code, 409)


class TelemetryPromotionTests(TelemetryTestBase):
    """The /end contract — all-or-nothing into the real registries."""

    GPR_CONFIG = {
        'title': 'Foundation Zone B',
        'survey_area': 'Grid 4-7',
        'structural_element': 'Raft Slab',
        'antenna_frequency_mhz': 400,
        'depth_range_m': 2.5,
        'latitude': 6.4281,
        'longitude': 3.4219,
    }

    def _gpr_session(self):
        return self._open('gpr', dict(self.GPR_CONFIG))

    def test_end_with_no_packets_is_refused(self):
        session = self._gpr_session()
        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.end_session(session, None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('no packets', str(ctx.exception))
        session.refresh_from_db()
        self.assertEqual(session.status, TelemetrySession.STATUS_OPEN)

    def test_happy_path_writes_real_rows_and_evidence_records(self):
        session = self._gpr_session()
        TelemetryService.append_packet(session, {
            'anomaly_type': 'void', 'severity': 'high',
            'depth_m': 0.8, 'estimated_size_m': 1.2, 'confidence': 0.82,
        })
        TelemetryService.append_packet(session, {
            'anomaly_type': 'rebar', 'severity': 'low',
            'depth_m': 0.15, 'rebar_cover_mm': 42.0,
        })

        promoted = TelemetryService.end_session(session, None)

        session.refresh_from_db()
        self.assertEqual(session.status, TelemetrySession.STATUS_ENDED)
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_SYNCED)
        self.assertEqual(session.sync_error, '')
        self.assertIsNotNone(session.session_end)
        self.assertIsNotNone(session.promoted_at)
        self.assertEqual(len(session.sha256_hash), 64)
        self.assertEqual(session.data_payload['packets'].__len__(), 2)
        self.assertEqual(promoted['anomalies'], 2)

        survey = GPRSurvey.objects.get(pk=promoted['survey_id'])
        self.assertEqual(survey.project, self.project)
        self.assertEqual(survey.device, self.device)
        self.assertEqual(survey.title, 'Foundation Zone B')
        self.assertEqual(survey.status, 'completed')
        self.assertEqual(survey.anomalies.count(), 2)
        # The rows carry the values the device sent, not defaults.
        void = survey.anomalies.get(anomaly_type='void')
        self.assertEqual(void.depth_m, 0.8)
        self.assertEqual(void.severity, 'high')

        # Every anomaly is normalised into the Evidence Registry, exactly as
        # the manual create path does.
        self.assertEqual(EvidenceRecord.objects.count(), 2)
        self.assertTrue(EvidenceRecord.objects.filter(
            source_model='digital_eye.GPRAnomaly').exists())

    def test_one_malformed_row_writes_nothing_at_all(self):
        session = self._gpr_session()
        TelemetryService.append_packet(session, {
            'anomaly_type': 'void', 'severity': 'high', 'depth_m': 0.8,
        })
        # `anomaly_type` is a choice field; 'unobtainium' is not one of them.
        TelemetryService.append_packet(session, {
            'anomaly_type': 'unobtainium', 'severity': 'low',
        })

        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.end_session(session, None)
        self.assertIn('anomaly_type', str(ctx.exception))

        session.refresh_from_db()
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_FAILED)
        self.assertEqual(session.status, TelemetrySession.STATUS_ENDED)
        self.assertIn('anomaly_type', session.sync_error)
        # The whole point: the good row did not survive either.
        self.assertEqual(GPRSurvey.objects.count(), 0)
        self.assertEqual(EvidenceRecord.objects.count(), 0)
        self.assertIsNone(session.data_payload)
        self.assertEqual(session.sha256_hash, '')

    def test_a_failed_promotion_can_be_retried_in_place(self):
        """A FAILED session is re-endable: the alternative is re-capturing
        measurements the device has already sent."""
        session = self._gpr_session()
        TelemetryService.append_packet(session, {'anomaly_type': 'unobtainium'})
        with self.assertRaises(TelemetryError):
            TelemetryService.end_session(session, None)
        session.refresh_from_db()
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_FAILED)

        # The rejected packet is corrected (append-only means a new session in
        # production; here the stored row is fixed to simulate a re-capture).
        packet = session.packets.get(sequence=1)
        packet.payload = {'anomaly_type': 'void', 'severity': 'high'}
        packet.chain_hash = chain_hash('', 1, packet.payload)
        packet.save(update_fields=['payload', 'chain_hash'])

        TelemetryService.end_session(session, None)
        session.refresh_from_db()
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_SYNCED)
        self.assertEqual(GPRSurvey.objects.count(), 1)

    def test_re_ending_a_synced_session_is_refused(self):
        session = self._gpr_session()
        TelemetryService.append_packet(session, {'anomaly_type': 'void'})
        TelemetryService.end_session(session, None)
        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.end_session(session, None)
        self.assertEqual(ctx.exception.status_code, 409)
        # And crucially it did not duplicate the survey.
        self.assertEqual(GPRSurvey.objects.count(), 1)

    def test_promotion_failure_message_never_leaks_a_stack_trace(self):
        session = self._gpr_session()
        TelemetryService.append_packet(session, {'anomaly_type': 'unobtainium'})
        with self.assertRaises(TelemetryError) as ctx:
            TelemetryService.end_session(session, None)
        message = str(ctx.exception)
        self.assertNotIn('Traceback', message)
        self.assertNotIn('File "', message)
        self.assertIn('nothing was written', message)

    def test_the_envelope_hash_attests_the_stored_payload(self):
        from common.hashing import canonical_json, sha256_hex

        session = self._gpr_session()
        TelemetryService.append_packet(session, {'anomaly_type': 'void'})
        TelemetryService.end_session(session, None)
        session.refresh_from_db()
        self.assertEqual(session.sha256_hash,
                         sha256_hex(canonical_json(session.data_payload)))


class TelemetryPunditPromotionTests(TelemetryTestBase):
    """PUNDIT promotion must run the same computed-output path as manual entry."""

    def setUp(self):
        super().setUp()
        self.device.device_type = 'pundit'
        self.device.save(update_fields=['device_type'])

    def test_pundit_session_creates_a_test_with_one_reading_per_packet(self):
        session = TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type='pundit',
            session_config={'test_type': 'pulse_velocity',
                            'structural_element': 'Column C1'},
        )
        for path_mm, transit_us in ((300.0, 70.0), (300.0, 72.0), (300.0, 68.0)):
            TelemetryService.append_packet(session, {
                'path_length_mm': path_mm, 'transit_time_us': transit_us,
            })

        promoted = TelemetryService.end_session(session, None)

        test = PUNDITTest.objects.get(pk=promoted['test_id'])
        self.assertEqual(test.readings.count(), 3)
        self.assertEqual(_reading_labels(test.readings.all()), ['A', 'B', 'C'])
        # The computed columns are populated, which proves the serializer ran
        # rather than a raw insert.
        self.assertIsNotNone(test.velocity_km_s)
        self.assertIsNotNone(test.estimated_compressive_strength_mpa)
        self.assertNotEqual(test.quality_grade, 'pending')
        self.assertEqual(EvidenceRecord.objects.count(), 1)

    def test_a_reading_with_a_non_positive_transit_time_fails_the_whole_batch(self):
        session = TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type='pundit',
            session_config={'test_type': 'pulse_velocity',
                            'structural_element': 'Column C2'},
        )
        TelemetryService.append_packet(session, {
            'path_length_mm': 300.0, 'transit_time_us': 70.0})
        TelemetryService.append_packet(session, {
            'path_length_mm': 300.0, 'transit_time_us': -5.0})

        with self.assertRaises(TelemetryError):
            TelemetryService.end_session(session, None)

        self.assertEqual(PUNDITTest.objects.count(), 0)
        self.assertEqual(PUNDITReading.objects.count(), 0)
        self.assertEqual(EvidenceRecord.objects.count(), 0)


class TelemetryAPITests(APITestCase):
    """The HTTP surface, including project scoping."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='telemetry_api@nexucon.com',
            email='telemetry_api@nexucon.com',
            password='Password123!',
        )
        # No Profile, so `scoped_projects` resolves to nothing — the correct
        # stand-in for someone from another agency.
        self.outsider = User.objects.create_user(
            username='telemetry_outsider@nexucon.com',
            email='telemetry_outsider@nexucon.com',
            password='Password123!',
        )
        self.project = Project.objects.create(name='API Telemetry Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='GPR-API-001', device_type='gpr',
            assigned_project=self.project, is_active=True,
        )
        self.client.force_authenticate(self.user)

    def _start(self, **overrides):
        body = {'device': str(self.device.id), 'data_type': 'gpr',
                'project': str(self.project.id),
                'session_config': {'title': 'API capture'}}
        body.update(overrides)
        return self.client.post(reverse('telemetry-session-start'), body,
                                format='json')

    def test_start_returns_201_and_the_session_reference(self):
        response = self._start()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'OPEN')
        self.assertEqual(response.data['sync_status'], 'PENDING')
        self.assertIsNone(response.data['session_end'])
        self.assertEqual(response.data['packet_count'], 0)

    def test_second_start_for_the_same_device_is_409(self):
        self._start()
        response = self._start()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_unknown_device_is_404(self):
        response = self._start(device='00000000-0000-0000-0000-000000000000')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_outside_the_callers_scope_is_400(self):
        """A caller with no scope at all must not open a session on a project.

        Uses the unprofiled user rather than the superuser fixture: a
        superuser legitimately sees every project, so it cannot demonstrate
        that scoping is enforced.
        """
        self.client.force_authenticate(self.outsider)
        response = self._start()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_append_and_end_round_trip(self):
        session_id = self._start().data['id']
        append = self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': session_id}),
            {'payload': {'anomaly_type': 'void', 'severity': 'high'}},
            format='json')
        self.assertEqual(append.status_code, status.HTTP_201_CREATED)
        self.assertEqual(append.data['sequence'], 1)
        self.assertEqual(append.data['packet_count'], 1)

        end = self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': session_id}),
            {}, format='json')
        self.assertEqual(end.status_code, status.HTTP_200_OK)
        self.assertEqual(end.data['sync_status'], 'SYNCED')
        self.assertEqual(end.data['promoted']['anomalies'], 1)

    def test_end_failure_returns_the_reason_and_the_failed_sync_status(self):
        session_id = self._start().data['id']
        self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': session_id}),
            {'payload': {'anomaly_type': 'unobtainium'}}, format='json')
        end = self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': session_id}),
            {}, format='json')
        self.assertEqual(end.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(end.data['sync_status'], 'FAILED')
        self.assertIn('anomaly_type', end.data['detail'])
        self.assertEqual(GPRSurvey.objects.count(), 0)

    def test_status_reports_none_for_session_end_while_open(self):
        session_id = self._start().data['id']
        response = self.client.get(
            reverse('telemetry-session-status', kwargs={'session_id': session_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['session_end'])
        self.assertIsNone(response.data['promoted'])
        # An empty chain proves nothing, so it is not reported as intact.
        self.assertIsNone(response.data['chain_valid'])

    def test_status_reports_a_true_chain_once_packets_exist(self):
        session_id = self._start().data['id']
        self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': session_id}),
            {'payload': {'anomaly_type': 'void'}}, format='json')
        response = self.client.get(
            reverse('telemetry-session-status', kwargs={'session_id': session_id}))
        self.assertTrue(response.data['chain_valid'])

    def test_another_users_session_is_404_not_403(self):
        session_id = self._start().data['id']
        self.client.force_authenticate(self.outsider)
        response = self.client.get(
            reverse('telemetry-session-status', kwargs={'session_id': session_id}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_another_user_cannot_append_to_a_foreign_session(self):
        session_id = self._start().data['id']
        self.client.force_authenticate(self.outsider)
        response = self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': session_id}),
            {'payload': {'anomaly_type': 'void'}}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(TelemetryPacket.objects.count(), 0)

    def test_an_unknown_session_id_is_404(self):
        response = self.client.get(reverse('telemetry-session-status', kwargs={
            'session_id': '00000000-0000-0000-0000-000000000000'}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_access_is_refused(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(reverse('telemetry-session-list')).status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_device_list_projects_the_existing_registry(self):
        response = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row['device_id'], 'GPR-API-001')
        self.assertIsNone(row['open_session_reference'])

    def test_device_list_reports_the_open_session_reference(self):
        session = self._start()
        response = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(response.data[0]['open_session_reference'],
                         session.data['session_reference'])

    def test_session_list_filters_by_status(self):
        self._start()
        response = self.client.get(reverse('telemetry-session-list'),
                                   {'status': 'OPEN'})
        self.assertEqual(len(response.data), 1)
        response = self.client.get(reverse('telemetry-session-list'),
                                   {'status': 'ENDED'})
        self.assertEqual(len(response.data), 0)


class TelemetryRouteTests(TestCase):
    """The routes exist and are named as the PWA expects."""

    def test_every_route_resolves(self):
        session_id = '11111111-1111-1111-1111-111111111111'
        self.assertEqual(reverse('telemetry-session-start'), '/api/v1/telemetry/session/start/')
        self.assertEqual(reverse('telemetry-session-list'), '/api/v1/telemetry/sessions/')
        self.assertEqual(reverse('telemetry-device-list'), '/api/v1/telemetry/devices/')
        self.assertEqual(
            reverse('telemetry-session-data', kwargs={'session_id': session_id}),
            f'/api/v1/telemetry/session/{session_id}/data/')
        self.assertEqual(
            reverse('telemetry-session-end', kwargs={'session_id': session_id}),
            f'/api/v1/telemetry/session/{session_id}/end/')
        self.assertEqual(
            reverse('telemetry-session-status', kwargs={'session_id': session_id}),
            f'/api/v1/telemetry/session/{session_id}/status/')

    def test_a_literal_segment_is_not_swallowed_by_a_converter_route(self):
        """`/telemetry/sessions/` must resolve to the list view, not to a
        session-id route with id='sessions' — hence the uuid: converter."""
        from django.urls import resolve
        self.assertEqual(resolve('/api/v1/telemetry/sessions/').url_name,
                         'telemetry-session-list')
        self.assertEqual(resolve('/api/v1/telemetry/devices/').url_name,
                         'telemetry-device-list')
