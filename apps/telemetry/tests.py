"""
Tests for the telemetry ingestion envelope (Inspector PWA Part 3).

The behaviour under test is the promotion contract: a session writes nothing
into any statutory registry until /end, and /end is all-or-nothing. The
individual assertions that matter most:

  * a malformed row leaves the session FAILED and writes **zero** registry
    rows — never a partial survey describing only the rows that parsed
  * the packet chain detects a single edited row
  * a re-sent sequence number is refused rather than silently replacing a row
  * a file the platform does not recognise is refused with nothing stored,
    and an unrecognised column is never guessed at — see the file-import
    section at the end of this module
"""
import hashlib
import json
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.digital_eye.models import FieldDevice, GPRSurvey, PUNDITReading, PUNDITTest
from apps.evidence.models import EvidenceRecord
from apps.projects.models import Project
from common.hashing import chain_hash

from apps.telemetry import gateway

from .export_import import EXPORT_STORAGE_PREFIX
from .gateway_config import (
    PROVISIONED_LABEL, GatewayConfigError, GatewayConfigService,
)
from .models import (
    DEVICE_TOKEN_PREFIX, DeviceToken, TelemetryPacket, TelemetrySession,
    hash_device_token,
)
from .services import DeviceTokenService, TelemetryError, TelemetryService

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

    def test_the_transport_routes_resolve(self):
        token_id = '11111111-1111-1111-1111-111111111111'
        self.assertEqual(reverse('telemetry-session-from-file'),
                         '/api/v1/telemetry/session/from-file/')
        self.assertEqual(reverse('telemetry-device-token-list'),
                         '/api/v1/telemetry/device-tokens/')
        self.assertEqual(
            reverse('telemetry-device-token-revoke', kwargs={'token_id': token_id}),
            f'/api/v1/telemetry/device-tokens/{token_id}/revoke/')

    def test_from_file_is_not_mistaken_for_a_session_id(self):
        from django.urls import resolve
        self.assertEqual(resolve('/api/v1/telemetry/session/from-file/').url_name,
                         'telemetry-session-from-file')


# ----------------------------------------------------------------------
# Transport — how a capture reached the platform
# ----------------------------------------------------------------------

class TelemetryTransportTests(TelemetryTestBase):
    """A session records how it arrived, or records that it did not."""

    def test_an_undeclared_transport_is_recorded_as_not_recorded(self):
        session = self._open()
        self.assertEqual(session.transport, '')
        self.assertIsNone(session.get_transport_display() or None)

    def test_a_declared_transport_is_stored(self):
        session = TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type='gpr', transport=TelemetrySession.TRANSPORT_WIFI)
        self.assertEqual(session.transport, 'WIFI')
        self.assertEqual(session.get_transport_display(),
                         'Direct Wi-Fi — instrument to network')

    def test_the_serializer_reports_null_rather_than_a_fallback_label(self):
        """`transport_display` is null when nothing was recorded.

        Null, not a plausible-looking default: naming a transport nobody
        observed would be a claim about a measurement's provenance.
        """
        from .serializers import TelemetrySessionSerializer
        session = self._open()
        self.assertIsNone(
            TelemetrySessionSerializer(session).data['transport_display'])


class TelemetryTransportAPITests(APITestCase):
    """A client may declare the transports it is actually responsible for."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='transport_api@nexucon.com',
            email='transport_api@nexucon.com', password='Password123!')
        self.project = Project.objects.create(name='Transport Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='GPR-TRANSPORT-001', device_type='gpr',
            assigned_project=self.project, is_active=True)
        self.client.force_authenticate(self.user)

    def _start(self, **overrides):
        body = {'device': str(self.device.id), 'data_type': 'gpr',
                'project': str(self.project.id)}
        body.update(overrides)
        return self.client.post(reverse('telemetry-session-start'), body,
                                format='json')

    def test_a_client_may_declare_a_machine_transport(self):
        response = self._start(transport='CLOUD')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['transport'], 'CLOUD')
        self.assertEqual(response.data['transport_display'],
                         'Cloud push — gateway to platform')

    def test_a_client_may_not_declare_a_transport_the_server_owns(self):
        """`FILE` and `MANUAL` are decided by which endpoint was called.

        A client able to claim them could file a typed-in number as an
        instrument export, which is the misreporting this field exists to
        prevent.
        """
        for claimed in ('FILE', 'MANUAL'):
            with self.subTest(transport=claimed):
                response = self._start(transport=claimed)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_omitting_the_transport_omits_it_from_the_response(self):
        response = self._start()
        self.assertEqual(response.data['transport'], '')
        self.assertIsNone(response.data['transport_display'])

    def test_the_session_list_can_be_filtered_by_transport(self):
        """Two captures on one device, arriving two different ways."""
        self._start(transport='CLOUD')
        TelemetrySession.objects.filter(device=self.device).update(
            status=TelemetrySession.STATUS_ENDED)
        self._start(transport='WIFI')

        response = self.client.get(reverse('telemetry-session-list'),
                                   {'transport': 'CLOUD'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['transport'] for row in response.data], ['CLOUD'])


# ----------------------------------------------------------------------
# Device credentials
# ----------------------------------------------------------------------

class DeviceTokenServiceTests(TelemetryTestBase):
    """Issuing, resolving and revoking a device credential."""

    def setUp(self):
        super().setUp()
        self.device.device_type = 'pundit'
        self.device.save(update_fields=['device_type'])

    def test_the_plaintext_is_returned_once_and_never_stored(self):
        token, raw = DeviceTokenService.issue(
            device=self.device, label='Site laptop bridge',
            issued_by=self.user)

        self.assertTrue(raw.startswith('nxdev_'))
        self.assertEqual(token.hashed_key, hash_device_token(raw))
        self.assertNotIn(raw, token.hashed_key)
        # Nothing on the row holds the secret — re-reading it cannot recover it.
        stored = DeviceToken.objects.get(pk=token.pk)
        self.assertNotEqual(stored.hashed_key, raw)
        self.assertEqual(stored.key_prefix, raw[:14])

    def test_a_label_is_required(self):
        with self.assertRaises(TelemetryError):
            DeviceTokenService.issue(device=self.device, label='   ',
                                     issued_by=self.user)

    def test_resolve_finds_a_live_credential(self):
        token, raw = DeviceTokenService.issue(
            device=self.device, label='Gateway', issued_by=self.user)
        self.assertEqual(DeviceToken.resolve(raw).pk, token.pk)

    def test_resolve_refuses_an_unknown_secret(self):
        DeviceTokenService.issue(device=self.device, label='Gateway',
                                 issued_by=self.user)
        self.assertIsNone(DeviceToken.resolve('nxdev_not-a-real-token'))
        self.assertIsNone(DeviceToken.resolve(''))
        self.assertIsNone(DeviceToken.resolve(None))

    def test_resolve_refuses_a_revoked_credential(self):
        token, raw = DeviceTokenService.issue(
            device=self.device, label='Gateway', issued_by=self.user)
        DeviceTokenService.revoke(token, revoked_by=self.user)
        self.assertIsNone(DeviceToken.resolve(raw))

    def test_resolve_refuses_an_expired_credential(self):
        from datetime import timedelta
        from django.utils import timezone
        _, raw = DeviceTokenService.issue(
            device=self.device, label='Expired gateway', issued_by=self.user,
            expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(DeviceToken.resolve(raw))

    def test_revoking_twice_keeps_the_first_timestamp(self):
        token, _ = DeviceTokenService.issue(
            device=self.device, label='Gateway', issued_by=self.user)
        DeviceTokenService.revoke(token)
        first = token.revoked_at
        DeviceTokenService.revoke(token)
        self.assertEqual(token.revoked_at, first)

    def test_the_device_relationship_is_protected(self):
        """Deleting an instrument must not orphan a live credential."""
        from django.db.models import ProtectedError
        DeviceTokenService.issue(device=self.device, label='Gateway',
                                 issued_by=self.user)
        with self.assertRaises(ProtectedError):
            self.device.delete()


class GatewayConfigServiceTests(TelemetryTestBase):
    """Provisioning an instrument for the field gateway.

    The claim this whole feature rests on is a negative one: the credential is
    never displayed, returned or logged. It goes from the mint straight into the
    file the gateway reads, so no person and no screen ever holds it — which is
    what removes the step that gets skipped, done wrong or leaked.
    """

    def setUp(self):
        super().setUp()
        self.config_dir = tempfile.mkdtemp(prefix='nexucon_gw_cfg_')
        self.inbox_dir = tempfile.mkdtemp(prefix='nexucon_gw_inbox_')
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.addCleanup(shutil.rmtree, self.inbox_dir, True)
        self._settings = override_settings(
            GATEWAY_CONFIG_DIR=self.config_dir,
            GATEWAY_INBOX_DIR=self.inbox_dir,
            GATEWAY_API_URL='http://web:8000',
        )
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        # PUNDIT because it is the one instrument with a whole-capture file
        # contract; the refusals for the others are tested below.
        self.device.device_type = 'pundit'
        self.device.save(update_fields=['device_type'])

    # -- helpers -------------------------------------------------------

    def _path(self, device=None):
        return os.path.join(self.config_dir, f'{(device or self.device).id}.json')

    def _config(self, device=None):
        return gateway.GatewayConfig.from_file(
            self._path(device), ledger_dir=self.config_dir)

    def _token_in_file(self, path=None):
        with open(path or self._path(), encoding='utf-8') as handle:
            return json.load(handle)['device_token']

    # -- what gets written ---------------------------------------------

    def test_enabling_writes_a_config_the_gateway_can_read(self):
        """The written file has to load through the gateway's own validator.

        Not a shape assertion: the config is only useful if the code that reads
        it accepts it, and every one of these fields is something the platform
        decides and the gateway would otherwise be told by hand.
        """
        GatewayConfigService.enable(self.device, actor=self.user)

        config = self._config()

        self.assertEqual(config.device, str(self.device.id))
        self.assertEqual(config.project, str(self.project.id))
        self.assertEqual(config.data_type, 'pundit')
        self.assertEqual(config.api_url, 'http://web:8000')
        self.assertEqual(
            config.watch_dir,
            os.path.join(self.inbox_dir, self.device.device_reference))
        # True, so a site that has not set its sync client up yet gets a
        # gateway that waits rather than one that exits every minute.
        self.assertTrue(config.wait_for_watch_dir)

    def test_the_config_file_is_named_after_the_device(self):
        """The gateway names each ledger from the config's ``device`` field.

        The filename is the device's id too, so the two agree — but only the
        field is load-bearing, and the naming test in ``tests_gateway`` is what
        pins that down.
        """
        path = GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(os.path.basename(path), f'{self.device.id}.json')

    def test_the_inbox_folder_is_created_for_the_site_to_sync_into(self):
        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertTrue(os.path.isdir(
            os.path.join(self.inbox_dir, self.device.device_reference)))

    def test_an_instrument_with_no_project_yet_gets_an_empty_project(self):
        """Empty, not absent, and not a project chosen on its behalf.

        The endpoint needs a project to open a session. Sending the capture to
        a guess would file a measurement against the wrong job, which is worse
        than a refusal that says so.
        """
        self.device.assigned_project = None
        self.device.save(update_fields=['assigned_project'])

        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(self._config().project, '')

    # -- the secret ----------------------------------------------------

    def test_the_credential_goes_into_the_file_and_nowhere_else(self):
        path = GatewayConfigService.enable(self.device, actor=self.user)

        raw = self._token_in_file(path)

        self.assertTrue(raw.startswith(DEVICE_TOKEN_PREFIX))
        # The return value is a path, not a secret, so a caller cannot
        # accidentally put one in a response or a log line.
        self.assertNotIn(raw, path)
        token = DeviceToken.resolve(raw)
        self.assertIsNotNone(token)
        self.assertEqual(token.device_id, self.device.id)
        # And the row holds a digest, so re-reading the database cannot
        # recover what was written.
        self.assertNotEqual(token.hashed_key, raw)

    def test_the_secret_is_never_logged(self):
        """A credential in a log is a credential on every machine that ships
        logs, and this module's whole purpose is that nobody sees it."""
        with self.assertLogs('apps.telemetry.gateway_config',
                             level='INFO') as captured:
            path = GatewayConfigService.enable(self.device, actor=self.user)
        raw = self._token_in_file(path)

        output = '\n'.join(captured.output)
        self.assertNotIn(raw, output)
        self.assertNotIn(raw[6:], output)
        self.assertIn('Gateway sync enabled', output)

    def test_every_credential_written_carries_the_provisioned_label(self):
        """So the next write can find the one it is replacing."""
        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(
            DeviceToken.objects.filter(device=self.device,
                                       label=PROVISIONED_LABEL).count(), 1)

    # -- repeat writes -------------------------------------------------

    def test_an_unchanged_enable_keeps_the_credential_already_written(self):
        """Re-minting would revoke a credential the gateway is still using.

        Its very next sweep would be refused with a 401, and nothing at the
        site could account for it — the officer only pressed the same button
        twice.
        """
        first = GatewayConfigService.enable(self.device, actor=self.user)
        before = self._token_in_file(first)

        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(self._token_in_file(first), before)
        self.assertEqual(
            DeviceToken.objects.filter(device=self.device,
                                       label=PROVISIONED_LABEL).count(), 1)

    def test_reassigning_the_instrument_rewrites_the_config(self):
        """The project is in the config, so a stale one sends to the old job.

        The panel would say sync was on and nothing anywhere would say it was
        pointed at the wrong project — which is exactly the silent wrongness
        this feature is built to remove.
        """
        first = GatewayConfigService.enable(self.device, actor=self.user)
        other = Project.objects.create(name='Second Site', status='ACTIVE')
        self.device.assigned_project = other
        self.device.save(update_fields=['assigned_project'])

        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(self._config().project, str(other.id))

    def test_rewriting_replaces_the_credential_and_revokes_the_old_one(self):
        """Every write mints a new one, because the old plaintext is gone.

        There is no earlier secret left to reuse: the row keeps only a digest.
        """
        first = GatewayConfigService.enable(self.device, actor=self.user)
        old = self._token_in_file(first)
        other = Project.objects.create(name='Second Site', status='ACTIVE')
        self.device.assigned_project = other
        self.device.save(update_fields=['assigned_project'])

        GatewayConfigService.enable(self.device, actor=self.user)

        new = self._token_in_file(first)
        self.assertNotEqual(new, old)
        self.assertIsNone(DeviceToken.resolve(old),
                          'the superseded credential is still live')
        self.assertEqual(DeviceToken.resolve(new).device_id, self.device.id)
        self.assertEqual(
            DeviceToken.objects.filter(device=self.device,
                                       label=PROVISIONED_LABEL,
                                       revoked_at__isnull=True).count(), 1)

    def test_a_config_that_will_not_parse_is_replaced_rather_than_trusted(self):
        """Self-healing, and safe: the worst case is a fresh credential."""
        path = self._path()
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{"api_url": "http://web:8000",')

        GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(self._config().device, str(self.device.id))

    # -- turning it off ------------------------------------------------

    def test_disabling_revokes_the_credential_and_removes_the_config(self):
        path = GatewayConfigService.enable(self.device, actor=self.user)
        raw = self._token_in_file(path)

        GatewayConfigService.disable(self.device, actor=self.user)

        self.assertFalse(os.path.exists(path))
        self.assertIsNone(DeviceToken.resolve(raw))
        self.device.refresh_from_db()
        self.assertFalse(self.device.gateway_enabled)

    def test_disabling_clears_a_config_that_was_deleted_by_hand(self):
        """The state this has to be able to clear: file gone, flag still on."""
        GatewayConfigService.enable(self.device, actor=self.user)
        os.remove(self._path())

        GatewayConfigService.disable(self.device, actor=self.user)

        self.device.refresh_from_db()
        self.assertFalse(self.device.gateway_enabled)
        self.assertEqual(
            DeviceToken.objects.filter(device=self.device,
                                       label=PROVISIONED_LABEL,
                                       revoked_at__isnull=True).count(), 0)

    def test_disabling_twice_is_not_an_error(self):
        GatewayConfigService.enable(self.device, actor=self.user)
        GatewayConfigService.disable(self.device, actor=self.user)

        GatewayConfigService.disable(self.device, actor=self.user)

        self.device.refresh_from_db()
        self.assertFalse(self.device.gateway_enabled)

    # -- what it refuses -----------------------------------------------

    def test_an_instrument_with_no_file_contract_is_refused_by_name(self):
        """Only PUNDIT has a contract describing a whole capture.

        Refused at provisioning rather than at the first file: a refused file
        is never offered again, so a mis-provisioned instrument would fill its
        folder with refusals and look, from the site, exactly like a gateway
        that is not running.
        """
        self.device.device_type = 'gpr'
        self.device.save(update_fields=['device_type'])

        with self.assertRaises(GatewayConfigError) as ctx:
            GatewayConfigService.enable(self.device, actor=self.user)

        self.assertIn('GPR file contract', str(ctx.exception))
        self.assertFalse(os.path.exists(self._path()))
        self.device.refresh_from_db()
        self.assertFalse(self.device.gateway_enabled)

    def test_an_instrument_kind_with_no_file_story_says_so(self):
        self.device.device_type = 'thermal'
        self.device.save(update_fields=['device_type'])

        with self.assertRaises(GatewayConfigError) as ctx:
            GatewayConfigService.enable(self.device, actor=self.user)

        self.assertIn('no file contract', str(ctx.exception))

    def test_a_refusal_mints_no_credential(self):
        """Nothing is created before the refusal, so nothing has to be undone."""
        self.device.device_type = 'gpr'
        self.device.save(update_fields=['device_type'])

        with self.assertRaises(GatewayConfigError):
            GatewayConfigService.enable(self.device, actor=self.user)

        self.assertEqual(DeviceToken.objects.filter(device=self.device).count(),
                         0)

    def test_provisioning_is_refused_when_nowhere_is_configured(self):
        with override_settings(GATEWAY_CONFIG_DIR=''):
            with self.assertRaises(GatewayConfigError) as ctx:
                GatewayConfigService.enable(self.device, actor=self.user)

        self.assertIn('GATEWAY_CONFIG_DIR', str(ctx.exception))

    def test_a_missing_config_directory_is_refused_not_created(self):
        """An absent directory means the volume is not mounted.

        Creating it would write the config to the container's own ephemeral
        filesystem, where the gateway never sees it — the panel would report
        success and the site would send nothing.
        """
        shutil.rmtree(self.config_dir)

        with self.assertRaises(GatewayConfigError) as ctx:
            GatewayConfigService.enable(self.device, actor=self.user)

        self.assertIn(self.config_dir, str(ctx.exception))
        self.assertFalse(os.path.exists(self.config_dir))

    def test_a_write_that_fails_leaves_no_live_credential(self):
        """The credential was minted a moment earlier and nothing holds it.

        Leaving it live would mean a secret nobody can see and nobody can use,
        which is one more thing to find and revoke later. A directory sitting
        where the config file belongs is the failure that gets past every check
        before the write: the config directory is real, the path is not
        readable as a config, and ``os.replace`` cannot land on it.
        """
        blocker = self._path()
        os.makedirs(blocker)

        with self.assertRaises(GatewayConfigError) as ctx:
            GatewayConfigService.enable(self.device, actor=self.user)

        self.assertIn('could not be written', str(ctx.exception))
        self.assertEqual(
            DeviceToken.objects.filter(device=self.device,
                                       label=PROVISIONED_LABEL,
                                       revoked_at__isnull=True).count(), 0)
        self.device.refresh_from_db()
        self.assertFalse(self.device.gateway_enabled)
        # And nothing was left half-written beside it.
        self.assertEqual(
            [n for n in os.listdir(self.config_dir) if n.endswith('.tmp')], [])


class DeviceTokenAPITests(APITestCase):
    """The credential endpoints, including who may mint one for what."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='token_api@nexucon.com',
            email='token_api@nexucon.com', password='Password123!')
        self.project = Project.objects.create(name='Token Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='PUNDIT-TOKEN-001', device_type='pundit',
            assigned_project=self.project, is_active=True)
        self.stranger = User.objects.create_user(
            username='token_stranger@nexucon.com',
            email='token_stranger@nexucon.com', password='Password123!')
        self.client.force_authenticate(self.user)

    def _issue(self, **overrides):
        body = {'device': str(self.device.id), 'label': 'Field bridge'}
        body.update(overrides)
        return self.client.post(reverse('telemetry-device-token-list'), body,
                                format='json')

    def test_issuing_returns_the_secret_exactly_once(self):
        response = self._issue()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        raw = response.data['token']
        self.assertTrue(raw.startswith('nxdev_'))

        # A second read of the same credential must not carry it again.
        listed = self.client.get(reverse('telemetry-device-token-list'))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listed.data), 1)
        self.assertNotIn('token', listed.data[0])
        self.assertNotIn('hashed_key', listed.data[0])

    def test_an_empty_label_is_refused(self):
        self.assertEqual(self._issue(label='   ').status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_a_device_outside_the_callers_scope_is_404(self):
        other_project = Project.objects.create(name='Other Site', status='ACTIVE')
        other_device = FieldDevice.objects.create(
            device_id='PUNDIT-OTHER-001', device_type='pundit',
            assigned_project=other_project, is_active=True)
        self.client.force_authenticate(self.stranger)
        response = self._issue(device=str(other_device.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(DeviceToken.objects.count(), 0)

    def test_revoking_an_out_of_scope_credential_is_404(self):
        raw = self._issue().data['token']
        token = DeviceToken.objects.get(hashed_key=hash_device_token(raw))
        self.client.force_authenticate(self.stranger)
        response = self.client.post(
            reverse('telemetry-device-token-revoke', kwargs={'token_id': token.id}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        token.refresh_from_db()
        self.assertIsNone(token.revoked_at)

    def test_revocation_stops_the_credential_immediately(self):
        raw = self._issue().data['token']
        token = DeviceToken.objects.get(hashed_key=hash_device_token(raw))

        self.client.credentials(HTTP_AUTHORIZATION=f'Device {raw}')
        live = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(live.status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.user)
        self.client.post(
            reverse('telemetry-device-token-revoke', kwargs={'token_id': token.id}))

        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Device {raw}')
        revoked = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(revoked.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_access_is_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.get(reverse('telemetry-device-token-list')).status_code,
            status.HTTP_401_UNAUTHORIZED)

    def test_a_credential_is_not_accepted_outside_telemetry(self):
        """A device secret must not authenticate anywhere a device is not the
        subject.

        Asserted with a POST, because a read-only list may be open to
        anonymous callers and would then prove nothing. If the credential
        authenticated as its issuer, this write would reach validation and
        return 400; refused, it stops at the permission check.
        """
        raw = self._issue().data['token']

        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Device {raw}')
        refused = self.client.post('/api/v1/projects/', {}, format='json')
        self.assertEqual(refused.status_code, status.HTTP_401_UNAUTHORIZED)

        # The same write as an authenticated user does get past the door, which
        # is what makes the 401 above a statement about the credential rather
        # than about the endpoint.
        self.client.credentials()
        self.client.force_authenticate(self.user)
        allowed = self.client.post('/api/v1/projects/', {}, format='json')
        self.assertNotEqual(allowed.status_code, status.HTTP_401_UNAUTHORIZED)


class DeviceSetupOrderTests(APITestCase):
    """Registering an instrument, then issuing the credential it sends with.

    This is the order `FIELD_GATEWAY.md` documents and the Instruments screen
    exists to perform, and it is the order sites actually work in: the
    instrument arrives and is registered before anyone knows which project its
    first job is on, but its gateway cannot send a single file until it holds a
    credential.

    It is tested here because the two halves live in different apps — the
    registry is `digital_eye`, the credential is `telemetry` — and the seam
    between them is exactly where a newly registered instrument went missing.
    `_scoped_devices` reached a device through its assigned project or through
    a session it had already captured, and a device that has just been
    registered has neither. ``assigned_project__in=...`` does not match NULL,
    so this held even for a superuser: no role could issue a credential for an
    instrument that had not yet been put on a project.
    """

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='device_setup@nexucon.com',
            email='device_setup@nexucon.com', password='Password123!')
        self.client.force_authenticate(self.user)

    def _register(self, **overrides):
        body = {'device_id': 'PUNDIT-SETUP-001', 'device_type': 'pundit'}
        body.update(overrides)
        return self.client.post('/api/v1/digital-eye/devices/', body,
                                format='json')

    def test_an_instrument_is_registered_with_no_project(self):
        """The state the rest of this class depends on being reachable."""
        created = self._register()

        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(created.data['assigned_project'])
        self.assertEqual(created.data['status'], 'registered')

    def test_a_freshly_registered_instrument_is_visible_to_whoever_registered_it(self):
        created = self._register()

        listed = self.client.get(reverse('telemetry-device-list'))

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertIn(str(created.data['id']),
                      [str(row['id']) for row in listed.data])

    def test_a_credential_can_be_issued_for_an_unassigned_instrument(self):
        created = self._register()

        response = self.client.post(
            reverse('telemetry-device-token-list'),
            {'device': created.data['id'], 'label': 'site laptop gateway'},
            format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['token'].startswith('nxdev_'))

    def test_the_new_credential_is_listed_against_its_instrument(self):
        created = self._register()
        self.client.post(
            reverse('telemetry-device-token-list'),
            {'device': created.data['id'], 'label': 'site laptop gateway'},
            format='json')

        listed = self.client.get(reverse('telemetry-device-token-list'),
                                 {'device': created.data['id']})

        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0]['label'], 'site laptop gateway')

    def test_registering_does_not_widen_anyone_elses_view(self):
        """The added relationship is "I registered it", not "anyone may see it"."""
        created = self._register()

        # A plain account with no government Profile, which `scoped_projects`
        # scopes to nothing by design. It is not the registrar, so the new
        # relationship must not reach it — that is the whole difference between
        # "its registrar can see it" and "everyone can".
        stranger = User.objects.create_user(
            username='stranger_devices@nexucon.com',
            email='stranger_devices@nexucon.com', password='Password123!')
        self.client.force_authenticate(stranger)

        listed = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(list(listed.data), [])

        refused = self.client.post(
            reverse('telemetry-device-token-list'),
            {'device': created.data['id'], 'label': 'not mine'}, format='json')
        self.assertEqual(refused.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(DeviceToken.objects.count(), 0)


class DeviceTokenSessionPinTests(APITestCase):
    """A credential acts for one instrument, and only for that instrument."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='pin_api@nexucon.com',
            email='pin_api@nexucon.com', password='Password123!')
        self.project = Project.objects.create(name='Pin Site', status='ACTIVE')
        self.device_a = FieldDevice.objects.create(
            device_id='PUNDIT-PIN-A', device_type='pundit',
            assigned_project=self.project, is_active=True)
        self.device_b = FieldDevice.objects.create(
            device_id='PUNDIT-PIN-B', device_type='pundit',
            assigned_project=self.project, is_active=True)
        self.token_a, self.raw_a = DeviceTokenService.issue(
            device=self.device_a, label='Unit A', issued_by=self.user)
        self.token_b, self.raw_b = DeviceTokenService.issue(
            device=self.device_b, label='Unit B', issued_by=self.user)

        # A session that belongs to B, created the ordinary way.
        self.session_b = TelemetryService.start_session(
            device=self.device_b, project=self.project, operator=self.user,
            data_type='pundit',
            session_config={'test_type': 'pulse_velocity',
                            'structural_element': 'Column B'})

    def _as_device(self, raw):
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Device {raw}')

    def test_a_credential_can_start_and_fill_its_own_session(self):
        self._as_device(self.raw_a)
        started = self.client.post(reverse('telemetry-session-start'), {
            'device': str(self.device_a.id), 'data_type': 'pundit',
            'project': str(self.project.id), 'transport': 'CLOUD',
            'session_config': {'test_type': 'pulse_velocity',
                               'structural_element': 'Column A'},
        }, format='json')
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(started.data['device_id'], 'PUNDIT-PIN-A')
        self.assertEqual(started.data['transport'], 'CLOUD')

        appended = self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': started.data['id']}),
            {'payload': {'path_length_mm': 300, 'transit_time_us': 70}},
            format='json')
        self.assertEqual(appended.status_code, status.HTTP_201_CREATED)

        ended = self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': started.data['id']}),
            {}, format='json')
        self.assertEqual(ended.status_code, status.HTTP_200_OK)
        self.assertEqual(ended.data['sync_status'], 'SYNCED')
        # The promoted row carries the issuer as its operator, never a blank.
        test = PUNDITTest.objects.get(pk=ended.data['promoted']['test_id'])
        self.assertEqual(test.created_by_id, self.user.id)

    def test_a_credential_cannot_open_a_session_as_another_instrument(self):
        self._as_device(self.raw_a)
        response = self.client.post(reverse('telemetry-session-start'), {
            'device': str(self.device_b.id), 'data_type': 'pundit',
            'project': str(self.project.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            TelemetrySession.objects.filter(device=self.device_a).count(), 0)

    def test_a_credential_cannot_read_another_instruments_session(self):
        self._as_device(self.raw_a)
        response = self.client.get(
            reverse('telemetry-session-status',
                    kwargs={'session_id': self.session_b.id}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_credential_cannot_append_to_another_instruments_session(self):
        self._as_device(self.raw_a)
        response = self.client.post(
            reverse('telemetry-session-data', kwargs={'session_id': self.session_b.id}),
            {'payload': {'path_length_mm': 300, 'transit_time_us': 70}},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.session_b.packets.count(), 0)

    def test_a_credential_cannot_end_another_instruments_session(self):
        self._as_device(self.raw_a)
        response = self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': self.session_b.id}),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.session_b.refresh_from_db()
        self.assertEqual(self.session_b.sync_status, TelemetrySession.SYNC_PENDING)

    def test_a_credential_cannot_reach_another_instruments_session_by_listing(self):
        self._as_device(self.raw_a)
        response = self.client.get(reverse('telemetry-session-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # B's session is the only one that exists, and it is not A's to see.
        self.assertEqual(response.data, [])

    def test_an_unknown_credential_is_401(self):
        self._as_device('nxdev_forged')
        response = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_deactivated_issuer_stops_the_credential(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self._as_device(self.raw_a)
        response = self.client.get(reverse('telemetry-device-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_touching_a_credential_updates_last_used(self):
        self.assertIsNone(self.token_a.last_used_at)
        self._as_device(self.raw_a)
        self.client.get(reverse('telemetry-device-list'))
        self.token_a.refresh_from_db()
        self.assertIsNotNone(self.token_a.last_used_at)


# ----------------------------------------------------------------------
# Instrument export files — the leg a radio-less unit uses
# ----------------------------------------------------------------------

def _stored_exports():
    """Every file currently held under the telemetry export prefix."""
    root = os.path.join(default_storage.location, EXPORT_STORAGE_PREFIX)
    if not os.path.isdir(root):
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, filename), root))
    return found


class FileImportTestBase(APITestCase):
    """Shared fixtures, with file writes confined to a temporary MEDIA_ROOT.

    The suite's `.env` points storage at R2, so without this an import test
    would upload to the real bucket. A fresh directory is made per test rather
    than per class or at import time: per class would let one test's stored
    export satisfy the next test's "nothing was left behind" assertion, and at
    import time would hold a path that a half-hour suite may well outlive.
    """

    def setUp(self):
        super().setUp()
        media_root = tempfile.mkdtemp(prefix='nexucon_telemetry_import_')
        self._storage_override = override_settings(
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {
                    'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
            },
            MEDIA_ROOT=media_root,
        )
        self._storage_override.enable()
        self.addCleanup(self._storage_override.disable)
        self.addCleanup(shutil.rmtree, media_root, True)

        self.user = User.objects.create_superuser(
            username='file_import@nexucon.com',
            email='file_import@nexucon.com', password='Password123!')
        self.project = Project.objects.create(name='Import Site', status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='PUNDIT-FILE-001', device_type='pundit',
            assigned_project=self.project, is_active=True)

    def _upload(self, content, name='export.csv', **overrides):
        body = {'device': str(self.device.id), 'project': str(self.project.id),
                'file': SimpleUploadedFile(name, content)}
        body.update(overrides)
        return self.client.post(reverse('telemetry-session-from-file'), body,
                                format='multipart')


#: The platform's documented UPV template, as an instrument export might write
#: it: element, test type and the measurements, all in the file.
UPV_CSV = (
    'STRUCTURAL ELEMENT,FLOOR,TEST TYPE,POINT,PATH LENGTH L (MM),'
    'TRANSIT TIME T (US),TRANSDUCER FREQUENCY (KHZ),TRANSDUCER TYPE\n'
    'Column C1,Ground Floor,Pulse Velocity,A,300,65.2,54,direct\n'
    'Column C1,Ground Floor,Pulse Velocity,B,300,68.1,54,direct\n'
    'Column C1,Ground Floor,Pulse Velocity,C,300,71.4,54,direct\n'
).encode('utf-8')

#: One export holding two elements — what a day's work actually looks like when
#: it leaves a unit that has no radio and no notion of a "session".
TWO_ELEMENT_CSV = (
    'STRUCTURAL ELEMENT,FLOOR,TEST TYPE,POINT,PATH LENGTH L (MM),'
    'TRANSIT TIME T (US),TRANSDUCER FREQUENCY (KHZ),TRANSDUCER TYPE\n'
    'Column C1,Ground Floor,Pulse Velocity,P1,300,65.2,54,direct\n'
    'Column C1,Ground Floor,Pulse Velocity,P2,295,66.1,54,direct\n'
    'Column C1,Ground Floor,Pulse Velocity,P3,305,68.4,54,direct\n'
    'Beam B2,Ground Floor,Pulse Velocity,P1,350,80.1,54,direct\n'
    'Beam B2,Ground Floor,Pulse Velocity,P2,345,81.3,54,direct\n'
    'Beam B2,Ground Floor,Pulse Velocity,P3,355,83.7,54,direct\n'
).encode('utf-8')

#: The same two elements, written in the order a spreadsheet sorted by point
#: would produce: each element interrupted by the other and resumed.
INTERLEAVED_ELEMENTS_CSV = (
    'STRUCTURAL ELEMENT,FLOOR,TEST TYPE,POINT,PATH LENGTH L (MM),'
    'TRANSIT TIME T (US)\n'
    'Column C1,Ground Floor,Pulse Velocity,P1,300,65.2\n'
    'Beam B2,Ground Floor,Pulse Velocity,P1,350,80.1\n'
    'Column C1,Ground Floor,Pulse Velocity,P2,295,66.1\n'
).encode('utf-8')

#: A bare measurement export — what a unit with no notion of a structural
#: element actually writes. The app supplies the context.
MEASUREMENTS_ONLY_CSV = (
    'POINT,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
    'A,300,65.2\n'
    'B,300,68.1\n'
).encode('utf-8')

#: The same two readings as `MEASUREMENTS_ONLY_CSV`, written in one real
#: instrument's own words. Nothing about these headers says "path length" to
#: the platform, which is the whole point: they are only readable once the
#: device carries a mapping that says so.
INSTRUMENT_WORDED_CSV = (
    'Location,Distance (mm),Time (us)\n'
    'A,300,65.2\n'
    'B,300,68.1\n'
).encode('utf-8')

#: The mapping that makes the file above readable. Keys are the instrument's
#: headers verbatim; values are contract keys.
INSTRUMENT_MAPPING = {
    'Location': 'point',
    'Distance (mm)': 'path_length_l_mm',
    'Time (us)': 'transit_time_t_us',
}


class FileImportParsingTests(FileImportTestBase):
    """The file → session path, and everything it refuses."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def test_a_full_template_export_becomes_a_pending_session(self):
        response = self._upload(UPV_CSV)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['transport'], 'FILE')
        self.assertEqual(response.data['transport_display'],
                         'Export file — instrument to file to app')
        # The capture is complete on arrival, and nothing is promoted yet.
        self.assertEqual(response.data['status'], 'ENDED')
        self.assertEqual(response.data['sync_status'], 'PENDING')
        self.assertEqual(response.data['packet_count'], 3)
        self.assertEqual(response.data['import_stats']['readings'], 3)
        self.assertEqual(response.data['source_file_name'], 'export.csv')
        self.assertEqual(response.data['source_file_sha256'],
                         hashlib.sha256(UPV_CSV).hexdigest())

        # Nothing reached the statutory registry — promotion is still a human
        # decision at /end.
        self.assertEqual(PUNDITTest.objects.count(), 0)
        self.assertEqual(EvidenceRecord.objects.count(), 0)

    def test_the_retained_export_is_the_exact_bytes_uploaded(self):
        """The packets are an interpretation of the file; the file is the
        ground truth if that interpretation is ever questioned."""
        response = self._upload(UPV_CSV)
        session = TelemetrySession.objects.get(pk=response.data['id'])

        self.assertTrue(session.source_file_storage_name)
        with default_storage.open(session.source_file_storage_name) as handle:
            stored = handle.read()
        self.assertEqual(stored, UPV_CSV)
        self.assertEqual(hashlib.sha256(stored).hexdigest(),
                         session.source_file_sha256)

    def test_the_imported_session_promotes_through_the_ordinary_end(self):
        session_id = self._upload(UPV_CSV).data['id']

        ended = self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': session_id}),
            {}, format='json')

        self.assertEqual(ended.status_code, status.HTTP_200_OK)
        self.assertEqual(ended.data['sync_status'], 'SYNCED')
        test = PUNDITTest.objects.get(pk=ended.data['promoted']['test_id'])
        self.assertEqual(test.readings.count(), 3)
        self.assertEqual(_reading_labels(test.readings.all()), ['A', 'B', 'C'])
        self.assertEqual(test.structural_element, 'Column C1')
        # The computed columns prove the real serializer ran, not a raw insert.
        self.assertIsNotNone(test.velocity_km_s)
        self.assertIsNotNone(test.estimated_compressive_strength_mpa)
        self.assertEqual(EvidenceRecord.objects.count(), 1)

    def test_the_packet_chain_is_intact_for_an_imported_session(self):
        session_id = self._upload(UPV_CSV).data['id']
        session = TelemetrySession.objects.get(pk=session_id)
        self.assertTrue(session.verify_chain())
        self.assertEqual(session.packets.order_by('sequence').first().sequence, 1)

    def test_a_measurement_only_export_uses_the_context_from_the_upload(self):
        response = self._upload(MEASUREMENTS_ONLY_CSV, name='unit-export.csv',
                                test_type='Pulse Velocity',
                                structural_element='Column C2', floor='First')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['packet_count'], 2)
        session = TelemetrySession.objects.get(pk=response.data['id'])
        self.assertEqual(session.session_config['test_type'], 'pulse_velocity')
        self.assertEqual(session.session_config['structural_element'], 'Column C2')

    def test_the_files_own_context_wins_over_the_uploads(self):
        """Where the export states the element, that is the instrument's own
        record of the capture and the form's value does not overwrite it."""
        response = self._upload(UPV_CSV, structural_element='Wrong Column')
        session = TelemetrySession.objects.get(pk=response.data['id'])
        self.assertEqual(session.session_config['structural_element'], 'Column C1')

    # -- refusals, each of which must leave nothing behind -----------------

    def test_an_unrecognised_column_is_refused_and_never_guessed(self):
        """`DISTANCE (MM)` is not `PATH LENGTH L (MM)`.

        Assuming they are the same would record a number nobody measured
        under a name that says it was measured.
        """
        content = (
            'STRUCTURAL ELEMENT,TEST TYPE,DISTANCE (MM),TRANSIT TIME T (US)\n'
            'Column C1,Pulse Velocity,300,65.2\n'
        ).encode('utf-8')
        response = self._upload(content, name='proprietary.csv')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('distance_mm', response.data['detail'])
        self.assertIn('PATH LENGTH L (MM)', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(TelemetryPacket.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_an_unrecognised_column_carries_a_code_the_app_can_act_on(self):
        """So the client can offer to record the mapping without matching on
        the English message, which breaks the first time it is reworded."""
        content = (
            'STRUCTURAL ELEMENT,TEST TYPE,DISTANCE (MM),TRANSIT TIME T (US)\n'
            'Column C1,Pulse Velocity,300,65.2\n'
        ).encode('utf-8')

        response = self._upload(content, name='proprietary.csv')

        self.assertEqual(response.data['code'], 'unknown_columns')

    def test_a_refusal_for_another_reason_carries_no_code(self):
        content = ('STRUCTURAL ELEMENT,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
                   'Column C3,300,65.2\n').encode('utf-8')

        response = self._upload(content)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(response.data['code'])

    def test_a_declared_scale_is_honoured_on_the_way_in(self):
        """The whole reason the mapping can express a unit.

        The same bytes read as millimetres give a pulse velocity a thousand
        times too low — positive, plausible, and impossible to tell from a slow
        reading once it is on a record. So the number is asserted, end to end,
        rather than the shape of the mapping that produced it.
        """
        self.device.column_mapping = {
            'Distance': {'to': 'path_length_l_mm', 'scale': 1000},
            'Time 1': 'transit_time_t_us',
        }
        self.device.save(update_fields=['column_mapping'])
        content = (
            'Structural Element,Test Type,Distance,Time 1\n'
            'Column C9,Pulse Velocity,0.300,65.2\n'
        ).encode('utf-8')

        response = self._upload(content, name='pl200.csv')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        packet = TelemetryPacket.objects.get(session_id=response.data['id'])
        self.assertEqual(packet.payload['path_length_mm'], 300.0)

        ended = self.client.post(
            reverse('telemetry-session-end',
                    kwargs={'session_id': response.data['id']}),
            {}, format='json')
        test = PUNDITTest.objects.get(pk=ended.data['promoted']['test_id'])
        # 300 mm across 65.2 us. Read as millimetres without the scale this
        # same file would promote at 0.0046 km/s.
        self.assertAlmostEqual(test.velocity_km_s, 4.601, places=2)

    def test_a_file_with_no_test_type_is_refused(self):
        content = ('STRUCTURAL ELEMENT,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
                   'Column C3,300,65.2\n').encode('utf-8')
        response = self._upload(content)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('TEST TYPE', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_an_instrument_s_own_export_is_taught_to_the_platform(self):
        """The whole errand, in the order an inspector actually does it.

        A PL-200 export is refused for its column names. The platform reads the
        file's own header row and proposes a mapping — including the conversion
        its metre-valued ``Distance`` needs to become the millimetres the
        contract is in. The inspector accepts it, the same file is sent again,
        and the reading that lands is a real pulse velocity rather than one a
        thousand times too low.

        Every step goes through the API, so this fails if any link between them
        stops holding — the refusal's code, the proposal, the serializer, the
        scaled read, or the promotion.
        """
        #: What a Proceq Pundit PL-200 writes: metres, and its own names. The
        #: element and test type are not in the file — the unit has no notion
        #: of them — so the import form supplies both, as it does in the app.
        pl200 = (
            'Id,Distance,Time 1,Time 2,Velocity,Measurement Type\n'
            '1,0.300,65.2,65.4,4601,Direct\n'
            '2,0.300,65.2,65.6,4601,Direct\n'
        ).encode('utf-8')
        context = {'test_type': 'Pulse Velocity',
                   'structural_element': 'Column C9'}

        # 1. Refused, with the code that tells the app a mapping is the fix.
        refused = self._upload(pl200, name='pl200.csv', **context)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(refused.data['code'], 'unknown_columns')

        # 2. The platform reads the header row and proposes.
        proposed = self.client.post(
            reverse('field-device-suggest-columns',
                    kwargs={'pk': str(self.device.id)}),
            {'file': SimpleUploadedFile('pl200.csv', pl200)},
            format='multipart')
        self.assertEqual(proposed.status_code, status.HTTP_200_OK)
        self.assertEqual(proposed.data['mapping']['Time 1'], 'transit_time_t_us')
        self.assertEqual(proposed.data['mapping']['Distance'],
                         {'to': 'path_length_l_mm', 'scale': 1000.0})
        # `Velocity` is computed by the platform, so it is never read — and it
        # is proposed as declined rather than merely left out, because a column
        # the mapping does not mention would refuse the file all over again.
        self.assertIn('Velocity', proposed.data['mapping'])
        self.assertIsNone(proposed.data['mapping']['Velocity'])

        # 3. Nothing has been recorded by any of that.
        self.assertEqual(
            FieldDevice.objects.get(pk=self.device.id).column_mapping, {})

        # 4. The inspector accepts it, and only now is anything written.
        saved = self.client.patch(
            reverse('field-device-detail', kwargs={'pk': str(self.device.id)}),
            {'column_mapping': proposed.data['mapping']}, format='json')
        self.assertEqual(saved.status_code, status.HTTP_200_OK, saved.data)

        # 5. The same file again, against the instrument that now knows.
        accepted = self._upload(pl200, name='pl200.csv', **context)
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED,
                         accepted.data)
        packet = TelemetryPacket.objects.get(
            session_id=accepted.data['id'], sequence=1)
        self.assertEqual(packet.payload['path_length_mm'], 300.0)
        # The declined columns reached nothing — not the packet, not a row.
        self.assertNotIn('velocity_km_s', packet.payload)
        self.assertNotIn('id', packet.payload)

        ended = self.client.post(
            reverse('telemetry-session-end',
                    kwargs={'session_id': accepted.data['id']}),
            {}, format='json')
        test = PUNDITTest.objects.get(pk=ended.data['promoted']['test_id'])
        self.assertAlmostEqual(test.velocity_km_s, 4.601, places=2)
        # The refusals stored nothing; the one accepted upload stored its bytes.
        self.assertEqual(len(_stored_exports()), 1)

    def test_a_pulse_velocity_row_with_no_transit_time_is_refused(self):
        content = (
            'STRUCTURAL ELEMENT,TEST TYPE,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
            'Column C4,Pulse Velocity,300,65.2\n'
            'Column C4,Pulse Velocity,300,\n'
        ).encode('utf-8')
        response = self._upload(content)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The message names the physical line, so the inspector can open the
        # file and look at it.
        self.assertIn('Row 3', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(TelemetryPacket.objects.count(), 0)
        self.assertEqual(PUNDITTest.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_an_empty_file_is_refused(self):
        response = self._upload(b'', name='empty.csv')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('empty', response.data['detail'].lower())
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_a_file_of_only_blank_lines_is_refused(self):
        response = self._upload(b'\n\n\n', name='blank.csv')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_a_pdf_is_refused_with_the_reason(self):
        """A PDF has no column contract, so it cannot be validated — the same
        refusal the data-import wizard gives."""
        response = self._upload(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n', name='scan.pdf')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('PDF', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_a_data_type_with_no_file_contract_is_refused_honestly(self):
        """A GPR export is survey headers, not the anomaly rows a session
        captures — accepting it would promote a session with nothing in it,
        so the refusal says why rather than importing under the wrong type."""
        response = self._upload(UPV_CSV, data_type='gpr')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('GPR', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_a_gnss_file_is_refused_honestly(self):
        response = self._upload(UPV_CSV, data_type='gnss')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('GNSS', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_an_out_of_scope_caller_cannot_import_onto_a_project(self):
        """A caller with no scope at all cannot land a capture on a project.

        Asserted as 400, matching `session/start/` — the device is resolved the
        same way on both endpoints, so a device that exists but whose project
        is out of scope is refused with the reason ("a project in your scope is
        required"), not reported as missing.
        """
        outsider = User.objects.create_user(
            username='file_outsider@nexucon.com',
            email='file_outsider@nexucon.com', password='Password123!')
        self.client.force_authenticate(outsider)
        response = self._upload(UPV_CSV)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('scope', response.data['detail'])
        self.assertEqual(TelemetrySession.objects.count(), 0)
        self.assertEqual(_stored_exports(), [])

    def test_anonymous_access_is_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self._upload(UPV_CSV).status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_a_missing_file_is_a_400_not_a_crash(self):
        response = self.client.post(reverse('telemetry-session-from-file'), {
            'device': str(self.device.id), 'project': str(self.project.id),
        }, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_json_export_is_accepted(self):
        import json as _json
        content = _json.dumps([
            {'STRUCTURAL ELEMENT': 'Column C5', 'TEST TYPE': 'Pulse Velocity',
             'POINT': 'A', 'PATH LENGTH L (MM)': 300, 'TRANSIT TIME T (US)': 65.2},
            {'STRUCTURAL ELEMENT': 'Column C5', 'TEST TYPE': 'Pulse Velocity',
             'POINT': 'B', 'PATH LENGTH L (MM)': 300, 'TRANSIT TIME T (US)': 68.1},
        ]).encode('utf-8')
        response = self._upload(content, name='export.json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['packet_count'], 2)

    def test_a_second_import_for_the_same_device_is_allowed(self):
        """An import is not a live stream.

        The one-open-session-per-device rule exists so two packet sequences
        cannot arrive from one instrument at once. A file the unit exported
        earlier is a separate, finished capture and does not contend for it.
        """
        first = self._upload(UPV_CSV)
        second = self._upload(MEASUREMENTS_ONLY_CSV, name='second.csv',
                              test_type='Pulse Velocity',
                              structural_element='Column C2')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(TelemetrySession.objects.count(), 2)
        self.assertNotEqual(first.data['session_reference'],
                            second.data['session_reference'])

    def test_an_import_alongside_a_live_stream_is_allowed(self):
        live = TelemetryService.start_session(
            device=self.device, project=self.project, operator=self.user,
            data_type='pundit', transport=TelemetrySession.TRANSPORT_WIFI,
            session_config={'test_type': 'pulse_velocity',
                            'structural_element': 'Live'})
        response = self._upload(UPV_CSV)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        live.refresh_from_db()
        # The live stream is untouched, and still open.
        self.assertEqual(live.status, TelemetrySession.STATUS_OPEN)
        self.assertEqual(live.packet_count, 0)

    def test_the_imported_session_is_audited_with_the_file_hash(self):
        from apps.audit.models import AuditEvent
        session_id = self._upload(UPV_CSV).data['id']
        session = TelemetrySession.objects.get(pk=session_id)

        event = AuditEvent.objects.filter(
            action='telemetry.session.file_import',
            resource_id=str(session_id)).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata['sha256'], session.source_file_sha256)
        self.assertEqual(event.metadata['file'], 'export.csv')

    def test_a_refused_import_is_audited_too(self):
        from apps.audit.models import AuditEvent
        self._upload(b'%PDF-1.4\n', name='bad.pdf')
        self.assertTrue(AuditEvent.objects.filter(
            action='telemetry.session.file_import_failed').exists())


class FileImportMultiElementTests(FileImportTestBase):
    """A file of several elements is several tests, not one broken one.

    The unit has no radio, so a day's readings leave it as a single export, and
    that export holds every element the operator walked. The platform used to
    build one test over all of them, name it after the first row, and then
    refuse the promotion for repeating a point label on that one element — a
    refusal that named a label which had never been wrong, on a file that was
    never malformed.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _promote(self, session_id):
        return self.client.post(
            reverse('telemetry-session-end', kwargs={'session_id': session_id}),
            {}, format='json')

    def test_a_file_of_two_elements_is_one_session_holding_two_tests(self):
        response = self._upload(TWO_ELEMENT_CSV)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Six readings, one capture: the file is not split into two sessions.
        self.assertEqual(response.data['packet_count'], 6)
        self.assertEqual(response.data['import_stats']['tests'], 2)
        self.assertEqual(TelemetrySession.objects.count(), 1)

    def test_every_packet_carries_the_element_it_was_measured_on(self):
        session_id = self._upload(TWO_ELEMENT_CSV).data['id']
        session = TelemetrySession.objects.get(pk=session_id)

        elements = [packet.payload.get('structural_element') for packet
                    in session.packets.order_by('sequence')]
        self.assertEqual(elements, ['Column C1'] * 3 + ['Beam B2'] * 3)

    def test_the_session_records_only_what_the_whole_file_agrees_on(self):
        """The element belongs to a test, not to the capture, once a capture
        can hold more than one. The transducer is on every row, so it stays."""
        session_id = self._upload(TWO_ELEMENT_CSV).data['id']
        session = TelemetrySession.objects.get(pk=session_id)

        self.assertNotIn('structural_element', session.session_config)
        self.assertEqual(session.session_config['transducer_frequency_khz'], 54)

    def test_promotion_writes_one_test_per_element(self):
        session_id = self._upload(TWO_ELEMENT_CSV).data['id']
        ended = self._promote(session_id)

        self.assertEqual(ended.status_code, status.HTTP_200_OK)
        self.assertEqual(ended.data['sync_status'], 'SYNCED')
        self.assertEqual(ended.data['promoted']['test_count'], 2)

        by_element = {test.structural_element: test
                      for test in PUNDITTest.objects.all()}
        self.assertEqual(set(by_element), {'Column C1', 'Beam B2'})
        for element, test in by_element.items():
            with self.subTest(element=element):
                self.assertEqual(test.readings.count(), 3)
                # P1, P2, P3 on each element — distinct within their own test,
                # which is the rule that was being broken when the two elements
                # were written as one.
                self.assertEqual(_reading_labels(test.readings.all()),
                                 ['P1', 'P2', 'P3'])
                # The computed columns prove the real serializer ran per test.
                self.assertIsNotNone(test.velocity_km_s)
                self.assertIsNotNone(test.estimated_compressive_strength_mpa)
        # 300 mm across 65.2 us on the first point of Column C1.
        self.assertAlmostEqual(by_element['Column C1'].readings.first().velocity_km_s,
                               4.601, places=2)
        self.assertEqual(EvidenceRecord.objects.count(), 2)

    def test_a_one_element_file_still_promotes_as_a_single_test(self):
        """The common case is unchanged, including the shape of the result."""
        session_id = self._upload(UPV_CSV).data['id']
        ended = self._promote(session_id)

        self.assertEqual(ended.data['promoted']['test_count'], 1)
        self.assertEqual(PUNDITTest.objects.count(), 1)
        self.assertEqual(ended.data['promoted']['readings'], 3)

    def test_an_element_interrupted_and_resumed_is_refused(self):
        """The one shape still refused, and the reason is not size.

        Two blocks of one element would file that element twice, so the file is
        refused by name and row rather than silently read as two tests.
        """
        response = self._upload(INTERLEAVED_ELEMENTS_CSV)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('two separate blocks', response.data['detail'])
        # Refused before a session exists, so nothing is left holding packets
        # that could never be promoted.
        self.assertEqual(TelemetrySession.objects.count(), 0)


class FileImportStorageTests(FileImportTestBase):
    """A refused import leaves no bytes behind either."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def test_a_rejected_file_is_not_left_in_storage(self):
        self._upload(b'%PDF-1.4\n', name='rejected.pdf')
        self.assertEqual(_stored_exports(), [])

    def test_a_successful_import_keeps_exactly_one_file(self):
        self._upload(UPV_CSV)
        self.assertEqual(len(_stored_exports()), 1)


class FileImportColumnMappingTests(FileImportTestBase):
    """A device's declared column mapping, and the guard it must not remove.

    Two directions matter equally. A mapping has to make a real instrument's
    export readable — that is why it exists, and without the first test here
    the whole feature could be inert. But it must not become a licence to
    guess: a column the mapping does not name is still refused by name, and
    that refusal is the property the statutory registry depends on. The
    second test is the regression guard for it.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _map_the_device(self, mapping):
        self.device.column_mapping = mapping
        self.device.save(update_fields=['column_mapping'])

    # -- the mapping is what makes the file readable --------------------

    def test_the_instrument_s_own_column_names_are_refused_without_a_mapping(self):
        response = self._upload(INSTRUMENT_WORDED_CSV, test_type='Pulse Velocity')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data['detail']
        # Refused by name, and told what it could have been.
        self.assertIn('location', detail)
        self.assertIn('distance_mm', detail)
        self.assertIn('no column mapping recorded', detail)
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_the_same_file_imports_once_the_device_carries_the_mapping(self):
        self._map_the_device(INSTRUMENT_MAPPING)

        response = self._upload(INSTRUMENT_WORDED_CSV, test_type='Pulse Velocity')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['import_stats']['readings'], 2)
        # The mapped columns arrived as the readings they name. Nothing is
        # promoted yet — the session is ENDED/PENDING, and the registry is
        # written only at /end.
        self.assertEqual(response.data['packet_count'], 2)
        self.assertEqual(PUNDITReading.objects.count(), 0)
        session = TelemetrySession.objects.get()
        payloads = [p.payload for p in session.packets.order_by('sequence')]
        self.assertEqual([p['point_label'] for p in payloads], ['A', 'B'])
        self.assertEqual([p['path_length_mm'] for p in payloads], [300, 300])
        self.assertEqual([p['transit_time_us'] for p in payloads], [65.2, 68.1])

    def test_a_column_the_mapping_does_not_cover_is_still_refused_by_name(self):
        # 'Location' and 'Distance (mm)' are covered; 'Time (us)' is not, and
        # the rows cannot be read without it. Extending the mapping is named as
        # the fix because that is the actual next step at a site.
        self._map_the_device({'Location': 'point',
                              'Distance (mm)': 'path_length_l_mm'})

        response = self._upload(INSTRUMENT_WORDED_CSV, test_type='Pulse Velocity')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('time_us', response.data['detail'])
        self.assertIn('does not cover', response.data['detail'])

    def test_a_mapped_column_and_its_contract_twin_together_are_ambiguous(self):
        # Both columns resolve to `path_length_l_mm`. The platform cannot know
        # which value is the real one, so it refuses rather than picking.
        self._map_the_device({'Distance (mm)': 'path_length_l_mm'})
        content = (
            'POINT,PATH LENGTH L (MM),Distance (mm),TRANSIT TIME T (US)\n'
            'A,300,999,65.2\n'
        ).encode('utf-8')

        response = self._upload(content, test_type='Pulse Velocity')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('both mean', response.data['detail'])

    def test_a_mapping_written_in_the_template_s_own_spelling_still_works(self):
        # The mapping's values are folded exactly as headers are, so someone
        # who writes the template's spelling has written a working mapping.
        self._map_the_device({'Location': 'POINT',
                              'Distance (mm)': 'PATH LENGTH L (MM)',
                              'Time (us)': 'TRANSIT TIME T (US)'})

        response = self._upload(INSTRUMENT_WORDED_CSV, test_type='Pulse Velocity')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['import_stats']['readings'], 2)


class FileImportResendTests(FileImportTestBase):
    """The same bytes arriving twice is a resend, not a second capture.

    A field gateway whose response was lost in transit retries, and an
    inspector unsure whether an upload went through tries again. Both must
    land on the session that already exists.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def test_the_same_bytes_twice_returns_the_first_session(self):
        first = self._upload(UPV_CSV)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        reference = first.data['session_reference']

        second = self._upload(UPV_CSV, name='export-again.csv')

        # 200, not 201: nothing was created this time. A gateway can treat
        # either as "the file is in", which is what stops it retrying forever.
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.data['session_reference'], reference)
        self.assertTrue(second.data['import_stats']['duplicate'])
        self.assertEqual(TelemetrySession.objects.count(), 1)
        # Three packets, not six: the second upload added nothing.
        self.assertEqual(TelemetryPacket.objects.count(), 3)

    def test_only_one_copy_of_the_bytes_is_kept(self):
        self._upload(UPV_CSV)
        self._upload(UPV_CSV, name='export-again.csv')
        self.assertEqual(len(_stored_exports()), 1)

    def test_a_resend_is_audited_apart_from_a_fresh_import(self):
        from apps.audit.models import AuditEvent
        self._upload(UPV_CSV)
        self._upload(UPV_CSV)
        self.assertTrue(AuditEvent.objects.filter(
            action='telemetry.session.file_import_resend').exists())

    def test_different_bytes_from_the_same_device_are_a_second_session(self):
        self._upload(UPV_CSV)
        changed = UPV_CSV.replace(b'65.2', b'70.9')

        response = self._upload(changed, name='second.csv')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(TelemetrySession.objects.count(), 2)

    def test_identical_bytes_from_another_device_are_not_swallowed(self):
        # The same export on two instruments would be a coincidence worth
        # looking at, not a resend. The check is scoped to the device.
        other = FieldDevice.objects.create(
            device_id='PUNDIT-FILE-002', device_type='pundit',
            assigned_project=self.project, is_active=True)
        self._upload(UPV_CSV)
        body = {'device': str(other.id), 'project': str(self.project.id),
                'file': SimpleUploadedFile('export.csv', UPV_CSV),
                'test_type': 'Pulse Velocity'}

        response = self.client.post(
            reverse('telemetry-session-from-file'), body, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(TelemetrySession.objects.count(), 2)
