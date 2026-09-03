import datetime
import io
import os
import tempfile
import uuid
from unittest import mock
from unittest.mock import patch

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.http import Http404
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ApiKey
from apps.audit.models import AuditEvent
from apps.common.ai_service import AIService
from apps.notifications.models import WebhookEndpoint
from apps.reports.models import QualityReport

from .models import (
    ScanSession, ScanMetadata, ProcessingTask, Defect, ThermalAnomaly,
    BIMAlignmentResult, ScanFile, ProgressValidationResult, ScanPlan,
    ComplianceCheck, Scanner, GnssTelemetry, ComplianceCertificate, StopWorkFlag,
    HardwareAlert,
)
from . import utils
from .selectors import ScanSelector
from .serializers import (
    SessionResponseSerializer, ScanMetadataSerializer, ScanFileSerializer,
    DefectSerializer, ScanUploadRequestSerializer, ScannerHeartbeatSerializer,
    StopWorkFlagSerializer,
)
from .services import ScanService, DataFusionService, run_bim_alignment

User = get_user_model()

# ---------------------------------------------------------------------------
# Shared test infrastructure
#
# The development environment configures Cloudflare R2 as the default Django
# storage backend, so an un-mocked upload would hit the real bucket. Every
# test case below overrides storage with the local filesystem backend pointed
# at a throwaway directory: uploads still write real bytes through the real
# storage API, but no test ever touches the network. Only external services
# (AI providers, the Celery broker, remote object storage, transactional
# email) are mocked; all model rows are created through the real ORM.
# ---------------------------------------------------------------------------
_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='nexucon_scans_tests_media_')

local_storage_settings = override_settings(
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    MEDIA_ROOT=_TEST_MEDIA_ROOT,
    MEDIA_URL='/media/',
)


def _synchronous_thread(*call_args, **kwargs):
    """Replacement for threading.Thread that runs the target inline.

    The AI-processing and BIM-alignment views dispatch their pipelines on a
    daemon thread; running them inline keeps the tests deterministic while
    still executing the real pipeline code.
    """
    target = kwargs.get('target')
    if target is None and call_args:
        target = call_args[0]
    if target is not None:
        target(*kwargs.get('args', ()))
    thread = mock.Mock()
    thread.is_alive.return_value = False
    return thread


@local_storage_settings
class BaseScansAPITest(APITestCase):
    """Authenticated API test bed with one real project and scan session."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='scanuser', email='scanuser@test.com', password='testpassword'
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.project = Project.objects.create(name='Scans Coverage Project')
        self.session = ScanSession.objects.create(
            scanner_id='scanner_cov_001', project=self.project
        )


@local_storage_settings
class ScanIntegrationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', email='testuser@test.com', password='testpassword')
        refresh = RefreshToken.for_user(self.user)
        self.token = str(refresh.access_token)
        self.project = Project.objects.create(name='Test Project')
        self.project_id = str(self.project.id)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

        self.start_session_url = reverse('start_session')

    def test_start_session(self):
        payload = {
            "project_id": self.project_id,
            "scanner_id": "scanner_001",
            "timestamp": "2026-08-01T12:00:00Z",
            "sensors_used": ["lidar", "rgb", "thermal", "rtk_gps"],
            "expected_size_mb": 1500
        }
        response = self.client.post(self.start_session_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", response.data)
        self.assertEqual(response.data["status"], "initialized")

    def test_start_session_unauthorized(self):
        self.client.credentials()  # clear token
        response = self.client.post(self.start_session_url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_submit_metadata(self):
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('submit_metadata', kwargs={'session_id': str(session.id)})

        payload = {
            "location": {
                "latitude": 45.123,
                "longitude": -75.123,
                "elevation": 12.5
            },
            "operator_id": "op123",
            "notes": "Test scan"
        }

        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["operator_id"], "op123")

        # Verify saved in DB
        session.refresh_from_db()
        self.assertTrue(hasattr(session, 'metadata'))
        self.assertEqual(session.metadata.latitude, 45.123)

    @patch("apps.processing.tasks.process_scan_pipeline.delay")
    def test_finalize_upload(self, mock_delay):
        """FinalizeUploadView transitions status and queues the pipeline."""
        mock_delay.return_value = None  # no broker needed
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('finalize_upload', kwargs={'session_id': str(session.id)})

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "processing")

        # Verify status in DB
        session.refresh_from_db()
        self.assertEqual(session.status, "processing")
        mock_delay.assert_called_once_with(str(session.id))

    def test_finalize_already_processing(self):
        session = ScanSession.objects.create(scanner_id="test_scanner", status="processing")
        url = reverse('finalize_upload', kwargs={'session_id': str(session.id)})

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_session_id(self):
        url = reverse('submit_metadata', kwargs={'session_id': str(uuid.uuid4())})
        response = self.client.post(url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_upload_data_endpoint(self):
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('upload_lidar', kwargs={'session_id': str(session.id)})

        from django.core.files.uploadedfile import SimpleUploadedFile
        file_obj = SimpleUploadedFile("file.las", b"file_content")
        response = self.client.post(url, {"file": file_obj})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("url", response.data)
        self.assertTrue(response.data["url"])

        # A real ScanFile row must exist, carrying the actual uploaded bytes
        # and filename — not a fabricated registry entry.
        scan_file = session.files.filter(file_type='lidar').first()
        self.assertIsNotNone(scan_file)
        self.assertEqual(scan_file.file_name, "file.las")
        self.assertEqual(scan_file.file_size_bytes, len(b"file_content"))
        self.assertEqual(scan_file.file_url, response.data["url"])

    def test_align_bim_endpoint(self):
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('align_bim', kwargs={'session_id': str(session.id)})

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "COMPLETED")

        # Deviation analysis retrieval — a session with no BIM file and no
        # point cloud has no measured deviations, so the values are null
        # rather than fabricated zeros.
        dev_url = reverse('deviation_analysis', kwargs={'session_id': str(session.id)})
        response = self.client.get(dev_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["mean_deviation"])

    def test_list_scans_by_project(self):
        # Create a scan linked to the project
        session = ScanSession.objects.create(scanner_id='scanner_001', project=self.project)
        url = reverse('project_scans', kwargs={'project_id': self.project_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(str(session.id) == item['id'] for item in response.data))


# ---------------------------------------------------------------------------
# Session CRUD endpoints
# ---------------------------------------------------------------------------

class ScanSessionApiTests(BaseScansAPITest):
    def test_start_session_without_project(self):
        payload = {"scanner_id": "scanner_x", "sensors_used": ["lidar"]}
        response = self.client.post(reverse('start_session'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["project"])
        session = ScanSession.objects.get(id=response.data["id"])
        self.assertIsNone(session.project)
        self.assertEqual(session.status, 'initialized')

    def test_start_session_missing_scanner_id(self):
        response = self.client.post(reverse('start_session'), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # This view answers with the raw serializer errors (no exception raised).
        self.assertIn('scanner_id', response.data)

    def test_start_session_unknown_project_returns_400(self):
        """A well-formed but nonexistent project UUID must be a 400, not a 500."""
        payload = {"scanner_id": "scanner_x", "project_id": str(uuid.uuid4())}
        response = self.client.post(reverse('start_session'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ScanSession.objects.filter(scanner_id="scanner_x").exists())

    def test_start_session_malformed_project_id_returns_400(self):
        payload = {"scanner_id": "scanner_x", "project_id": "not-a-uuid"}
        response = self.client.post(reverse('start_session'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_sessions_ordered_newest_first(self):
        newest = ScanSession.objects.create(scanner_id='newest')
        response = self.client.get(reverse('list_sessions'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in response.data]
        self.assertIn(str(self.session.id), ids)
        self.assertIn(str(newest.id), ids)
        self.assertEqual(ids[0], str(newest.id))

    def test_list_sessions_filters_by_project_param(self):
        """?project=<uuid> scopes the list to that project's sessions only."""
        other_project = Project.objects.create(name='Other Filter Project')
        mine = ScanSession.objects.create(scanner_id='mine', project=self.project)
        foreign = ScanSession.objects.create(scanner_id='foreign', project=other_project)
        response = self.client.get(reverse('list_sessions'), {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in response.data]
        self.assertIn(str(mine.id), ids)
        self.assertIn(str(self.session.id), ids)
        self.assertNotIn(str(foreign.id), ids)

    def test_list_sessions_unknown_project_returns_empty_list(self):
        response = self.client.get(reverse('list_sessions'), {'project': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_scan_detail(self):
        response = self.client.get(
            reverse('scan_detail', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['scanner_id'], 'scanner_cov_001')
        self.assertEqual(response.data['name'], 'scanner_cov_001')
        self.assertEqual(response.data['project_name'], 'Scans Coverage Project')
        self.assertEqual(response.data['project'], self.project.id)

    def test_scan_detail_not_found(self):
        response = self.client.get(
            reverse('scan_detail', kwargs={'session_id': str(uuid.uuid4())})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_scan_status(self):
        response = self.client.get(
            reverse('scan_status', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['session_id'], str(self.session.id))
        self.assertEqual(response.data['status'], 'initialized')
        self.assertIn('updated_at', response.data)

    def test_project_scans_only_returns_requested_project_sessions(self):
        other_project = Project.objects.create(name='Other Project')
        mine = ScanSession.objects.create(scanner_id='mine', project=self.project)
        foreign = ScanSession.objects.create(scanner_id='foreign', project=other_project)
        response = self.client.get(
            reverse('project_scans', kwargs={'project_id': str(self.project.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item['id'] for item in response.data]
        self.assertIn(str(mine.id), ids)
        self.assertIn(str(self.session.id), ids)
        self.assertNotIn(str(foreign.id), ids)

    def test_project_scans_empty_project(self):
        fresh = Project.objects.create(name='Empty Project')
        response = self.client.get(
            reverse('project_scans', kwargs={'project_id': str(fresh.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class ScansAuthRequiredTests(APITestCase):
    """Every scans endpoint requires authentication (device / user JWT)."""

    def test_list_sessions_requires_auth(self):
        response = self.client.get(reverse('list_sessions'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_start_session_requires_auth(self):
        response = self.client.post(reverse('start_session'), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fleet_status_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('fleet_status')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_gnss_telemetry_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('gnss_telemetry')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_qa_insights_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('qa_insights')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_integration_settings_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('integration_settings')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_compliance_checks_list_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('compliance_checks_list')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_api_docs_requires_auth(self):
        self.assertEqual(
            self.client.get(reverse('api_docs')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_trimble_endpoints_require_auth(self):
        self.assertEqual(
            self.client.get(reverse('trimble_auth')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(reverse('trimble_callback')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_scan_detail_requires_auth(self):
        session = ScanSession.objects.create(scanner_id='locked')
        self.assertEqual(
            self.client.get(
                reverse('scan_detail', kwargs={'session_id': str(session.id)})
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_viewsets_require_auth(self):
        self.assertEqual(
            self.client.get(reverse('scan_plan-list')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(reverse('scanner-list')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


# ---------------------------------------------------------------------------
# Metadata submission
# ---------------------------------------------------------------------------

class SubmitMetadataExtraTests(BaseScansAPITest):
    def _url(self):
        return reverse('submit_metadata', kwargs={'session_id': str(self.session.id)})

    def test_invalid_location_returns_400(self):
        response = self.client.post(
            self._url(), {"location": {"latitude": "not-a-number"}}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ScanMetadata.objects.filter(session=self.session).exists())

    def test_duplicate_metadata_returns_400(self):
        ScanMetadata.objects.create(session=self.session, latitude=1.0, longitude=2.0)
        response = self.client.post(self._url(), {"operator_id": "op"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already exists', str(response.data))

    def test_nested_location_flattened_to_columns(self):
        payload = {
            "location": {"latitude": 6.5, "longitude": 3.4, "elevation": 12.0},
            "operator_id": "op-7",
            "notes": "site survey",
        }
        response = self.client.post(self._url(), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.session.refresh_from_db()
        self.assertEqual(self.session.metadata.latitude, 6.5)
        self.assertEqual(self.session.metadata.longitude, 3.4)
        self.assertEqual(self.session.metadata.elevation, 12.0)
        self.assertEqual(self.session.metadata.operator_id, "op-7")

    def test_metadata_without_location(self):
        response = self.client.post(self._url(), {"operator_id": "op"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.metadata.latitude)
        self.assertEqual(self.session.metadata.operator_id, "op")


# ---------------------------------------------------------------------------
# Finalize / processing dispatch
# ---------------------------------------------------------------------------

class FinalizeUploadExtraTests(BaseScansAPITest):
    def _url(self):
        return reverse('finalize_upload', kwargs={'session_id': str(self.session.id)})

    @patch('apps.processing.tasks.process_scan_pipeline.delay')
    def test_broker_unavailable_returns_503_and_resets_status(self, mock_delay):
        mock_delay.side_effect = RuntimeError('broker unreachable')
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn('could not be queued', response.data['error'])
        # The session must not be left stranded in a status nothing services.
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'initialized')

    @patch('apps.processing.tasks.process_scan_pipeline.delay')
    def test_finalize_writes_audit_event(self, mock_delay):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action='FINALIZE_SCAN_UPLOADS', resource_id=str(self.session.id)
            ).exists()
        )


# ---------------------------------------------------------------------------
# File upload endpoints
# ---------------------------------------------------------------------------

class UploadEndpointsTests(BaseScansAPITest):
    UPLOAD_VIEWS = [
        'upload_lidar', 'upload_rgb', 'upload_thermal',
        'upload_gps', 'upload_gaussian_splat', 'upload_bim',
    ]

    def _upload(self, view_name, filename, content=b'payload'):
        url = reverse(view_name, kwargs={'session_id': str(self.session.id)})
        return self.client.post(url, {'file': SimpleUploadedFile(filename, content)})

    def test_upload_without_file_returns_400(self):
        for view_name in self.UPLOAD_VIEWS:
            with self.subTest(view=view_name):
                url = reverse(view_name, kwargs={'session_id': str(self.session.id)})
                response = self.client.post(url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_upload_rgb_sets_session_url_and_creates_file(self):
        response = self._upload('upload_rgb', 'photo.jpg')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.session.refresh_from_db()
        self.assertTrue(self.session.rgb_url)
        scan_file = self.session.files.filter(file_type='rgb').first()
        self.assertIsNotNone(scan_file)
        self.assertEqual(scan_file.file_name, 'photo.jpg')
        self.assertEqual(scan_file.file_size_bytes, len(b'payload'))
        self.assertEqual(scan_file.file_url, response.data['url'])

    def test_upload_thermal_sets_session_url_and_creates_file(self):
        response = self._upload('upload_thermal', 'thermal.jpg')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.session.refresh_from_db()
        self.assertTrue(self.session.thermal_url)
        self.assertTrue(
            self.session.files.filter(file_type='thermal', file_name='thermal.jpg').exists()
        )

    def test_upload_gps_returns_201_with_serializer_payload(self):
        response = self._upload('upload_gps', 'track.gpx')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['file_type'], 'gps')
        self.assertEqual(response.data['file_name'], 'track.gpx')
        self.assertIn('content_url', response.data)

    def test_upload_gaussian_splat_returns_201(self):
        response = self._upload('upload_gaussian_splat', 'splat.ply')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['file_type'], 'gaussian_splat')
        self.assertTrue(
            self.session.files.filter(file_type='gaussian_splat').exists()
        )

    def test_upload_bim_returns_201(self):
        response = self._upload('upload_bim', 'model.ifc', b'IFC2X3;')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['file_type'], 'bim')
        self.assertTrue(self.session.files.filter(file_type='bim').exists())

    def test_upload_unknown_session_returns_404(self):
        url = reverse('upload_lidar', kwargs={'session_id': str(uuid.uuid4())})
        response = self.client.post(url, {'file': SimpleUploadedFile('x.las', b'x')})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_scan_files_list_scoped_to_session(self):
        other = ScanSession.objects.create(scanner_id='other_session')
        ScanFile.objects.create(
            session=other, file_type='lidar', file_url='http://example.test/o.las'
        )
        ScanFile.objects.create(
            session=self.session, file_type='rgb', file_url='http://example.test/m.jpg'
        )
        response = self.client.get(
            reverse('list_scan_files', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['file_type'], 'rgb')

    def test_delete_scan_file(self):
        scan_file = ScanFile.objects.create(
            session=self.session, file_type='lidar', file_url='http://example.test/a.las'
        )
        url = reverse(
            'delete_scan_file',
            kwargs={'session_id': str(self.session.id), 'file_id': str(scan_file.id)},
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ScanFile.objects.filter(id=scan_file.id).exists())

    def test_delete_scan_file_wrong_session_returns_404(self):
        scan_file = ScanFile.objects.create(
            session=ScanSession.objects.create(scanner_id='other'),
            file_type='lidar', file_url='http://example.test/a.las',
        )
        url = reverse(
            'delete_scan_file',
            kwargs={'session_id': str(self.session.id), 'file_id': str(scan_file.id)},
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(ScanFile.objects.filter(id=scan_file.id).exists())


class ScanFileContentTests(BaseScansAPITest):
    def _make_file(self, url, name='cloud.las'):
        return ScanFile.objects.create(
            session=self.session, file_type='lidar', file_url=url,
            file_name=name, file_size_bytes=4,
        )

    def _content_url(self, scan_file):
        return reverse(
            'scan_file_content',
            kwargs={'session_id': str(self.session.id), 'file_id': str(scan_file.id)},
        )

    def test_local_media_file_redirects_to_media_route(self):
        scan_file = self._make_file('/media/scans/x/cloud.las')
        response = self.client.get(self._content_url(scan_file))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'http://testserver/media/scans/x/cloud.las')

    def test_remote_file_streamed_through_api(self):
        remote = 'https://r2.cloudflarestorage.com/bucket/scans/x/cloud.las?sig=abc'
        scan_file = self._make_file(remote)
        upstream = mock.Mock(
            status_code=200,
            headers={'Content-Type': 'application/octet-stream', 'Content-Length': '4'},
        )
        upstream.iter_content.return_value = iter([b'abcd'])
        with mock.patch('apps.scans.utils.refresh_storage_url', return_value=remote), \
                mock.patch('requests.get', return_value=upstream) as mock_get:
            response = self.client.get(self._content_url(scan_file))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/octet-stream')
        self.assertIn('cloud.las', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content), b'abcd')
        mock_get.assert_called_once()

    def test_storage_error_returns_502(self):
        import requests as requests_lib
        scan_file = self._make_file('https://r2.cloudflarestorage.com/bucket/x/cloud.las')
        upstream = mock.Mock(status_code=404, headers={})
        with mock.patch('apps.scans.utils.refresh_storage_url',
                        return_value=scan_file.file_url), \
                mock.patch('requests.get', return_value=upstream):
            response = self.client.get(self._content_url(scan_file))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

        with mock.patch('apps.scans.utils.refresh_storage_url',
                        return_value=scan_file.file_url), \
                mock.patch('requests.get',
                           side_effect=requests_lib.RequestException('boom')):
            response = self.client.get(self._content_url(scan_file))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_unknown_file_returns_404(self):
        response = self.client.get(
            reverse(
                'scan_file_content',
                kwargs={'session_id': str(self.session.id), 'file_id': str(uuid.uuid4())},
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# AI processing endpoints
# ---------------------------------------------------------------------------

@mock.patch('threading.Thread', side_effect=_synchronous_thread)
class AiProcessingTests(BaseScansAPITest):
    def _ai_findings(self):
        visual = [{
            'type': 'crack', 'severity': 'HIGH', 'description': 'Vertical crack',
            'confidence_score': 0.91, 'location_x': 1.5, 'location_y': 2.5,
            'location_z': 0.0, 'grid_zone': 'B2',
            'image_bbox': {'xmin': 0.1, 'ymin': 0.2, 'xmax': 0.4, 'ymax': 0.6},
        }]
        thermal = [{
            'temperature_variance': 6.4, 'severity': 'medium',
            'description': 'Hot spot', 'confidence': 0.77, 'grid_zone': 'A1',
        }]
        return visual, thermal

    def _url(self, view_name='start_ai_processing'):
        return reverse(view_name, kwargs={'session_id': str(self.session.id)})

    def test_start_persists_real_findings(self, mock_thread):
        visual, thermal = self._ai_findings()
        ScanSession.objects.filter(id=self.session.id).update(
            rgb_url='http://example.test/rgb.jpg',
            thermal_url='http://example.test/thermal.jpg',
        )
        QualityReport.objects.create(scan=self.session)  # stale snapshot must be dropped
        with mock.patch.object(AIService, 'detect_visual_defects', return_value=visual), \
                mock.patch.object(AIService, 'detect_thermal_anomalies', return_value=thermal):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('id', response.data)

        task = ProcessingTask.objects.get(session=self.session, task_type='ai_analysis')
        self.assertEqual(task.status, 'completed')

        defect = self.session.defects.first()
        self.assertIsNotNone(defect)
        self.assertEqual(defect.type, 'crack')
        self.assertEqual(defect.severity, 'high')
        self.assertEqual(defect.confidence_score, 0.91)
        self.assertEqual(defect.grid_zone, 'B2')
        self.assertEqual(defect.image_url, 'http://example.test/rgb.jpg')
        self.assertAlmostEqual(defect.bbox_xmin, 0.1)
        self.assertAlmostEqual(defect.bbox_ymin, 0.2)
        self.assertAlmostEqual(defect.bbox_xmax, 0.4)
        self.assertAlmostEqual(defect.bbox_ymax, 0.6)

        anomaly = self.session.thermal_anomalies.first()
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly.temperature_variance, 6.4)
        self.assertEqual(anomaly.severity, 'medium')
        self.assertAlmostEqual(anomaly.confidence_score, 0.77)  # 'confidence' fallback key
        self.assertFalse(QualityReport.objects.filter(scan=self.session).exists())

    def test_start_without_media_completes_without_findings(self, mock_thread):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task = ProcessingTask.objects.get(session=self.session, task_type='ai_analysis')
        self.assertEqual(task.status, 'completed')
        self.assertEqual(self.session.defects.count(), 0)
        self.assertEqual(self.session.thermal_anomalies.count(), 0)

    def test_ai_failure_marks_task_failed(self, mock_thread):
        ScanSession.objects.filter(id=self.session.id).update(
            rgb_url='http://example.test/rgb.jpg'
        )
        with mock.patch.object(AIService, 'detect_visual_defects',
                               side_effect=RuntimeError('provider down')):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task = ProcessingTask.objects.get(session=self.session, task_type='ai_analysis')
        self.assertEqual(task.status, 'failed')

    def test_stream_runs_pipeline_and_emits_events(self, mock_thread):
        visual, thermal = self._ai_findings()
        ScanSession.objects.filter(id=self.session.id).update(
            rgb_url='http://example.test/rgb.jpg',
            thermal_url='http://example.test/thermal.jpg',
        )
        with mock.patch.object(AIService, 'detect_visual_defects', return_value=visual), \
                mock.patch.object(AIService, 'detect_thermal_anomalies', return_value=thermal):
            response = self.client.get(self._url('stream_ai_processing'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        body = b''.join(response.streaming_content).decode()
        self.assertIn('"progress": 100', body)
        self.assertIn('[DONE]', body)
        self.assertEqual(self.session.defects.count(), 1)
        self.assertEqual(self.session.thermal_anomalies.count(), 1)

    def test_processing_status_lists_tasks(self, mock_thread):
        ProcessingTask.objects.create(session=self.session, task_type='ai_analysis',
                                      status='completed')
        ProcessingTask.objects.create(session=self.session, task_type='bim_alignment',
                                      status='pending')
        response = self.client.get(self._url('ai_processing_status'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


class DefectAndAnomalyEndpointTests(BaseScansAPITest):
    def setUp(self):
        super().setUp()
        self.defect = Defect.objects.create(
            session=self.session, type='crack', severity='high',
            description='beam crack', confidence_score=0.9,
        )
        self.anomaly = ThermalAnomaly.objects.create(
            session=self.session, temperature_variance=4.5, severity='low',
            confidence_score=0.6,
        )

    def test_list_defects_scoped_to_session(self):
        other = ScanSession.objects.create(scanner_id='other')
        Defect.objects.create(session=other, type='spalling', severity='low')
        response = self.client.get(
            reverse('list_defects', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['type'], 'crack')

    def test_patch_defect(self):
        url = reverse(
            'defect_detail',
            kwargs={'session_id': str(self.session.id), 'defect_id': str(self.defect.id)},
        )
        response = self.client.patch(url, {'severity': 'critical'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.severity, 'critical')

    def test_patch_defect_invalid_value_returns_400(self):
        url = reverse(
            'defect_detail',
            kwargs={'session_id': str(self.session.id), 'defect_id': str(self.defect.id)},
        )
        response = self.client.patch(url, {'severity': 'catastrophic'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_defect_wrong_session_returns_404(self):
        other = ScanSession.objects.create(scanner_id='other')
        url = reverse(
            'defect_detail',
            kwargs={'session_id': str(other.id), 'defect_id': str(self.defect.id)},
        )
        response = self.client.patch(url, {'severity': 'low'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_thermal_anomalies_scoped_to_session(self):
        other = ScanSession.objects.create(scanner_id='other')
        ThermalAnomaly.objects.create(session=other, temperature_variance=1.0,
                                      severity='low')
        response = self.client.get(
            reverse('list_thermal_anomalies', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['temperature_variance'], 4.5)

    def test_patch_thermal_anomaly(self):
        url = reverse(
            'thermal_anomaly_detail',
            kwargs={'session_id': str(self.session.id),
                    'anomaly_id': str(self.anomaly.id)},
        )
        response = self.client.patch(url, {'status': 'RESOLVED'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.anomaly.refresh_from_db()
        self.assertEqual(self.anomaly.status, 'RESOLVED')

    def test_patch_thermal_anomaly_invalid_returns_400(self):
        url = reverse(
            'thermal_anomaly_detail',
            kwargs={'session_id': str(self.session.id),
                    'anomaly_id': str(self.anomaly.id)},
        )
        response = self.client.patch(url, {'temperature_variance': 'hot'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# BIM alignment / deviation / clash endpoints
# ---------------------------------------------------------------------------

class BimAlignmentApiTests(BaseScansAPITest):
    def _store_local_bim(self, filename, content=b'IFC2X3 data'):
        stored = default_storage.save(
            f'scans/{self.session.id}/bim/{filename}', ContentFile(content)
        )
        return f'/media/{stored}'

    def test_align_bim_with_local_bim_file(self):
        url = self._store_local_bim('model.ifc')
        ScanFile.objects.create(
            session=self.session, file_type='bim', file_name='model.ifc',
            file_url=url, file_size_bytes=13,
        )
        QualityReport.objects.create(scan=self.session)
        analysis = {
            'alignment': {'status': 'aligned'},
            'deviations': {
                'mean_mm': 12.0, 'max_mm': 110.0, 'min_mm': 2.0,
                'top': [{'x': 1.0, 'y': 2.0, 'z': 3.0, 'deviation_mm': 110.0}],
            },
            'clashes': [{
                'id': 'CLASH-01', 'element2_id': 'Wall-12', 'deviation_mm': 25.0,
                'severity': 'high', 'location': 'Zone A',
            }],
        }
        with mock.patch('apps.processing.services.BIMIFCService.analyze_session',
                        return_value=analysis) as mock_analyze:
            response = self.client.post(
                reverse('align_bim', kwargs={'session_id': str(self.session.id)})
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data['status'], 'COMPLETED')
        self.assertEqual(response.data['message'], 'BIM alignment completed.')
        mock_analyze.assert_called_once()

        alignment = BIMAlignmentResult.objects.get(session=self.session)
        self.assertEqual(alignment.alignment_status, 'SUCCESS')
        self.assertEqual(alignment.mean_deviation, 12.0)
        self.assertEqual(alignment.max_deviation, 110.0)
        self.assertEqual(len(alignment.top_deviations), 2)  # one deviation + one clash
        self.assertEqual(alignment.top_deviations[1]['id'], 'CLASH-01')
        self.assertEqual(alignment.clashes[0]['id'], 'CLASH-01')

        # Compliance checks derived from the real measured values: the mean,
        # the max, the top deviation and the clash penetration each get one.
        checks = self.session.compliance_checks.all()
        self.assertEqual(checks.count(), 4)
        self.assertEqual(checks.filter(status='pass').count(), 1)
        self.assertEqual(checks.filter(status='fail').count(), 3)
        self.assertFalse(QualityReport.objects.filter(scan=self.session).exists())

    def test_align_bim_unknown_session_404(self):
        response = self.client.post(
            reverse('align_bim', kwargs={'session_id': str(uuid.uuid4())})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@mock.patch('threading.Thread', side_effect=_synchronous_thread)
class StreamBimAlignmentTests(BaseScansAPITest):
    def test_stream_runs_alignment_and_emits_events(self, mock_thread):
        with mock.patch('apps.scans.services.run_bim_alignment') as mock_align:
            response = self.client.get(
                reverse('stream_bim_alignment',
                        kwargs={'session_id': str(self.session.id)})
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        mock_align.assert_called_once()
        body = b''.join(response.streaming_content).decode()
        self.assertIn('"progress": 100', body)
        self.assertIn('[DONE]', body)


class DeviationAnalysisTests(BaseScansAPITest):
    def _url(self):
        return reverse('deviation_analysis', kwargs={'session_id': str(self.session.id)})

    def test_without_alignment_returns_empty_object(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {})

    def test_with_alignment_returns_metrics_and_logs_audit(self):
        BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='SUCCESS',
            mean_deviation=7.5, max_deviation=19.0, min_deviation=1.0,
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['mean_deviation'], 7.5)
        self.assertEqual(response.data['max_deviation'], 19.0)
        self.assertTrue(
            AuditEvent.objects.filter(
                action='DEVIATION_ANALYSIS_VIEWED', resource_id=str(self.session.id)
            ).exists()
        )


class DeviationHeatmapTests(BaseScansAPITest):
    def _url(self):
        return reverse('deviation_heatmap', kwargs={'session_id': str(self.session.id)})

    def test_no_data_reports_unavailable(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['available'])
        self.assertEqual(response.data['alignment_status'], 'NO_DATA')
        self.assertEqual(response.data['hotspots'], [])
        self.assertEqual(response.data['hotspot_count'], 0)
        self.assertIsNone(response.data['mean_deviation'])

    def test_builds_hotspots_from_real_findings(self):
        Defect.objects.create(
            session=self.session, type='crack', severity='high',
            confidence_score=0.8, grid_zone='A1', description='Wide crack',
        )
        Defect.objects.create(
            session=self.session, type='spalling', severity='low',
            confidence_score=0.4, room_level='Level 2',
        )
        ThermalAnomaly.objects.create(
            session=self.session, temperature_variance=5.0, severity='medium',
            confidence_score=0.7,
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['hotspot_count'], 3)
        # Only the thermal variance maps to a real deviation figure.
        self.assertEqual(response.data['mean_deviation'], 5.0)
        self.assertEqual(response.data['max_deviation'], 5.0)

        alignment = BIMAlignmentResult.objects.get(session=self.session)
        self.assertEqual(alignment.top_deviations, response.data['hotspots'])
        defect_entry = alignment.top_deviations[0]
        self.assertEqual(defect_entry['element'], 'Crack')
        self.assertIsNone(defect_entry['deviation_mm'])
        self.assertEqual(defect_entry['location'], 'A1')
        anomaly_entry = alignment.top_deviations[2]
        self.assertEqual(anomaly_entry['element'], 'Thermal Variance')
        self.assertEqual(anomaly_entry['deviation_mm'], 5.0)

        # A second request serves the persisted alignment unchanged.
        response2 = self.client.get(self._url())
        self.assertEqual(response2.data['hotspot_count'], 3)

    def test_serves_existing_alignment_with_top_deviations(self):
        hotspots = [{'id': 'DEV-01', 'element': 'Column', 'deviation_mm': 22.0}]
        BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='SUCCESS',
            mean_deviation=22.0, max_deviation=22.0, min_deviation=22.0,
            top_deviations=hotspots,
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['hotspots'], hotspots)


class ClashDetectionTests(BaseScansAPITest):
    def _url(self):
        return reverse('clash_detection', kwargs={'session_id': str(self.session.id)})

    def test_not_run_without_alignment(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['clashes_found'], 0)
        self.assertEqual(response.data['status'], 'not_run')
        self.assertEqual(response.data['clashes'], [])

    def test_returns_persisted_clashes(self):
        BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='SUCCESS',
            clashes=[{'id': 'C1', 'element2_id': 'Beam-3'}],
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['clashes_found'], 1)
        self.assertEqual(response.data['status'], 'completed')
        self.assertEqual(response.data['clashes'][0]['id'], 'C1')


# ---------------------------------------------------------------------------
# Progress validation
# ---------------------------------------------------------------------------

class ProgressValidationTests(BaseScansAPITest):
    def _url(self):
        return reverse('progress_validation', kwargs={'session_id': str(self.session.id)})

    def test_existing_result_is_served(self):
        ProgressValidationResult.objects.create(
            session=self.session, progress_score=0.55, covered_area_sqm=120.5
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['progress_score'], 0.55)
        self.assertEqual(response.data['covered_area_sqm'], 120.5)
        self.assertIn('volume_metrics', response.data)

    def test_without_lidar_reports_null_score(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['progress_score'])
        self.assertEqual(response.data['status'], 'completed')
        self.assertIsNone(response.data['covered_area_sqm'])
        self.assertFalse(
            ProgressValidationResult.objects.filter(session=self.session).exists()
        )

    def test_computes_area_from_real_las_file(self):
        import laspy
        las = laspy.create(point_format=0, file_version='1.2')
        las.x = [0.0, 10.0]
        las.y = [0.0, 20.0]
        las.z = [0.0, 2.0]
        buffer = io.BytesIO()
        las.write(buffer)
        stored = default_storage.save(
            f'scans/{self.session.id}/lidar/site.las',
            ContentFile(buffer.getvalue()),
        )
        ScanFile.objects.create(
            session=self.session, file_type='lidar', file_name='site.las',
            file_url=f'/media/{stored}', file_size_bytes=buffer.tell(),
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Real bounding box: 10m x 20m = 200 sqm against the 500 sqm target.
        self.assertEqual(response.data['covered_area_sqm'], 200.0)
        self.assertEqual(response.data['progress_score'], 0.4)
        result = ProgressValidationResult.objects.get(session=self.session)
        self.assertEqual(result.progress_score, 0.4)

    def test_unknown_session_returns_404(self):
        response = self.client.get(
            reverse('progress_validation', kwargs={'session_id': str(uuid.uuid4())})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Fleet, telemetry and QA insights
# ---------------------------------------------------------------------------

class FleetStatusTests(BaseScansAPITest):
    def test_fleet_payload_from_real_devices_and_sessions(self):
        Scanner.objects.create(
            device_id='DEV-ONLINE', status='online', battery_level=88,
            latitude=6.1, longitude=3.1,
        )
        Scanner.objects.create(device_id='DEV-OFFLINE', status='offline')
        located_project = Project.objects.create(
            name='Located Project', latitude=7.2, longitude=4.9
        )
        active = ScanSession.objects.create(
            scanner_id='DEV-ONLINE', project=located_project, status='initialized'
        )
        ScanSession.objects.create(
            scanner_id='DEV-OFFLINE', project=located_project, status='completed'
        )
        ScanMetadata.objects.create(
            session=active, latitude=6.15, longitude=3.15, notes='survey'
        )

        response = self.client.get(reverse('fleet_status'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total'], 2)
        self.assertEqual(response.data['online'], 1)
        self.assertEqual(response.data['offline'], 1)
        self.assertEqual(response.data['idle'], 0)

        scanners = {s['device_id']: s for s in response.data['scanners']}
        online = scanners['DEV-ONLINE']
        self.assertEqual(online['battery_level'], 88)
        self.assertEqual(online['latitude'], 6.1)
        self.assertEqual(online['longitude'], 3.1)
        self.assertEqual(online['session_count'], 1)
        self.assertEqual(online['active_session_id'], str(active.id))
        self.assertEqual(online['latest_session_status'], 'initialized')

        offline = scanners['DEV-OFFLINE']
        self.assertEqual(offline['battery_level'], 0.0)
        # No device-reported position: falls back to the latest project site.
        self.assertEqual(offline['latitude'], 7.2)
        self.assertEqual(offline['longitude'], 4.9)
        self.assertIsNone(offline['active_session_id'])
        self.assertEqual(offline['latest_session_status'], 'completed')

        self.assertEqual(len(response.data['scan_locations']), 1)
        location = response.data['scan_locations'][0]
        self.assertEqual(location['session_id'], str(active.id))
        self.assertEqual(location['latitude'], 6.15)
        self.assertEqual(location['longitude'], 3.15)
        self.assertEqual(location['project_name'], 'Located Project')
        self.assertEqual(location['notes'], 'survey')

    def test_empty_fleet(self):
        response = self.client.get(reverse('fleet_status'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['scanners'], [])
        self.assertEqual(response.data['total'], 0)
        self.assertEqual(response.data['scan_locations'], [])


class GnssTelemetryApiTests(BaseScansAPITest):
    def test_list_telemetry(self):
        scanner = Scanner.objects.create(device_id='DEV-GNSS')
        GnssTelemetry.objects.create(
            session=self.session, scanner=scanner, fix_rate=95.5,
            fix_type='rtk_fixed', satellites=22, horizontal_accuracy_m=0.02,
            recorded_at=timezone.now(),
        )
        GnssTelemetry.objects.create(
            scanner=scanner, fix_rate=60.0, fix_type='rtk_float',
            recorded_at=timezone.now(),
        )
        response = self.client.get(reverse('gnss_telemetry'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        fix_rates = {item['fix_rate'] for item in response.data}
        self.assertEqual(fix_rates, {95.5, 60.0})


class QAInsightsTests(BaseScansAPITest):
    def _url(self, query=''):
        return reverse('qa_insights') + query

    def test_insights_from_real_telemetry(self):
        now = timezone.now()
        GnssTelemetry.objects.create(session=self.session, fix_rate=100.0, recorded_at=now)
        GnssTelemetry.objects.create(
            session=self.session, fix_rate=50.0,
            recorded_at=now - datetime.timedelta(days=2),
        )
        scanner = Scanner.objects.create(device_id='DEV-QA')
        HardwareAlert.objects.create(
            scanner=scanner, session=self.session, issue='IMU drift',
            severity='high', description='drift detected', timestamp=now,
        )
        HardwareAlert.objects.create(
            scanner=scanner, issue='Battery low', severity='low',
            description='replace pack', timestamp=now - datetime.timedelta(hours=1),
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['telemetry_available'])
        self.assertEqual(response.data['days'], 7)
        self.assertEqual(response.data['gnss_rtk_fix_rate'], 75.0)
        self.assertEqual(len(response.data['rtk_fix_trend']), 7)
        self.assertEqual(response.data['rtk_fix_trend'][-1], 100.0)
        self.assertEqual(response.data['rtk_fix_trend'][-3], 50.0)

        alerts = response.data['hardware_alerts']
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]['issue'], 'IMU drift')
        self.assertEqual(alerts[0]['scan'], self.session.scanner_id)
        self.assertEqual(alerts[1]['issue'], 'Battery low')
        self.assertEqual(alerts[1]['scan'], 'DEV-QA')

    def test_30_day_window(self):
        GnssTelemetry.objects.create(
            session=self.session, fix_rate=100.0,
            recorded_at=timezone.now() - datetime.timedelta(days=20),
        )
        response = self.client.get(self._url('?days=30'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['days'], 30)
        self.assertEqual(len(response.data['rtk_fix_trend']), 30)
        self.assertEqual(response.data['gnss_rtk_fix_rate'], 100.0)

    def test_invalid_days_falls_back_to_seven(self):
        for query in ('?days=eleven', '?days=15'):
            with self.subTest(query=query):
                response = self.client.get(self._url(query))
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data['days'], 7)

    def test_empty_state_is_honest(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['telemetry_available'])
        self.assertEqual(response.data['gnss_rtk_fix_rate'], 0.0)
        self.assertEqual(response.data['rtk_fix_trend'], [0.0] * 7)
        self.assertEqual(response.data['hardware_alerts'], [])


# ---------------------------------------------------------------------------
# Integration settings, Trimble stubs, API docs
# ---------------------------------------------------------------------------

class IntegrationSettingsTests(BaseScansAPITest):
    def test_get_creates_api_key_when_missing(self):
        response = self.client.get(reverse('integration_settings'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['api_key'].startswith('nex_live_'))
        self.assertEqual(ApiKey.objects.filter(is_active=True).count(), 1)
        self.assertEqual(response.data['webhook_url'], '')

    def test_get_returns_existing_webhook(self):
        WebhookEndpoint.objects.create(name='Ops hook', url='https://hooks.example.test/nx')
        response = self.client.get(reverse('integration_settings'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['webhook_url'], 'https://hooks.example.test/nx')

    def test_rotate_key_revokes_previous(self):
        old = ApiKey.objects.create(key=ApiKey.generate_key(), is_active=True)
        response = self.client.post(
            reverse('integration_settings'), {'action': 'rotate_key'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_key = response.data['api_key']
        self.assertNotEqual(new_key, old.key)
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertTrue(ApiKey.objects.filter(key=new_key, is_active=True).exists())

    def test_save_webhook_creates_then_updates(self):
        response = self.client.post(
            reverse('integration_settings'),
            {'action': 'save_webhook', 'webhook_url': 'https://hooks.example.test/a'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(WebhookEndpoint.objects.count(), 1)

        response = self.client.post(
            reverse('integration_settings'),
            {'action': 'save_webhook', 'webhook_url': 'https://hooks.example.test/b'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(WebhookEndpoint.objects.count(), 1)
        self.assertEqual(
            WebhookEndpoint.objects.first().url, 'https://hooks.example.test/b'
        )

    def test_unknown_action_returns_400(self):
        response = self.client.post(
            reverse('integration_settings'), {'action': 'nope'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class MiscIntegrationViewsTests(BaseScansAPITest):
    def test_sync_trimble(self):
        response = self.client.post(
            reverse('sync_trimble_connect', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'synced')

    def test_trimble_auth_url(self):
        response = self.client.get(reverse('trimble_auth'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['url'], 'https://identity.trimble.com/oauth/authorize')

    def test_trimble_callback(self):
        response = self.client.get(reverse('trimble_callback'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'authenticated')

    def test_api_docs(self):
        response = self.client.get(reverse('api_docs'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['base_url'].endswith('/api/v1'))
        self.assertIn('schema_url', response.data)
        self.assertEqual(response.data['authentication']['type'], 'JWT Bearer (SimpleJWT)')
        self.assertEqual(len(response.data['webhook']['events']), 7)


# ---------------------------------------------------------------------------
# Compliance checks, certificates, stop-work orders
# ---------------------------------------------------------------------------

class ComplianceApiTests(BaseScansAPITest):
    def _make_check(self, session, check_id, check_status):
        return ComplianceCheck.objects.create(
            id=check_id, session=session, element='Global Structure',
            rule='Mean Deviation ≤ 15mm', measured='5.0mm',
            status=check_status, confidence='95%',
        )

    def test_list_checks_without_session_filter(self):
        other = ScanSession.objects.create(scanner_id='other')
        self._make_check(self.session, 'CHK-A1', 'pass')
        self._make_check(other, 'CHK-B1', 'fail')
        response = self.client.get(reverse('compliance_checks_list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_list_checks_filtered_by_session(self):
        other = ScanSession.objects.create(scanner_id='other')
        self._make_check(self.session, 'CHK-A1', 'pass')
        self._make_check(other, 'CHK-B1', 'fail')
        response = self.client.get(
            reverse('compliance_checks_list'), {'session_id': str(self.session.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], 'CHK-A1')

    def test_issue_certificate_counts_real_checks(self):
        self._make_check(self.session, 'CHK-P1', 'pass')
        self._make_check(self.session, 'CHK-F1', 'fail')
        self._make_check(self.session, 'CHK-P2', 'pass')
        response = self.client.post(
            reverse('compliance_certificate', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['total_checks'], 3)
        self.assertEqual(response.data['passed_checks'], 2)
        self.assertEqual(response.data['failed_checks'], 1)
        self.assertTrue(response.data['certificate_number'].startswith('NX-CERT-'))
        certificate = ComplianceCertificate.objects.get(session=self.session)
        self.assertEqual(certificate.total_checks, 3)
        self.assertEqual(certificate.status, 'issued')

    def test_list_certificates(self):
        ComplianceCertificate.objects.create(
            session=self.session, certificate_number='NX-CERT-1',
            total_checks=1, passed_checks=1,
        )
        ComplianceCertificate.objects.create(
            session=self.session, certificate_number='NX-CERT-2',
            total_checks=2, passed_checks=0,
        )
        response = self.client.get(
            reverse('compliance_certificate', kwargs={'session_id': str(self.session.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]['certificate_number'], 'NX-CERT-2')


class StopWorkFlagTests(BaseScansAPITest):
    def _url(self):
        return reverse('stop_work_flag', kwargs={'session_id': str(self.session.id)})

    def test_list_flags_scoped_to_session(self):
        StopWorkFlag.objects.create(
            session=self.session, reason='Initial stop', flagged_by='a@b.test'
        )
        StopWorkFlag.objects.create(
            session=ScanSession.objects.create(scanner_id='other'), reason='Other'
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['reason'], 'Initial stop')
        self.assertEqual(response.data[0]['flagged_by_name'], 'a@b.test')

    def test_post_requires_reason(self):
        response = self.client.post(self._url(), {'reason': '   '}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(StopWorkFlag.objects.filter(session=self.session).exists())

    def test_post_rejects_unknown_check(self):
        response = self.client.post(
            self._url(), {'reason': 'stop', 'check_id': 'CHK-NOPE'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_creates_flag_and_notifies(self):
        with mock.patch(
            'apps.notifications.email_service.EmailService.send_email',
            return_value={'success': True},
        ) as mock_mail:
            response = self.client.post(
                self._url(), {'reason': 'Concrete deviation beyond tolerance'},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'active')
        self.assertTrue(response.data['notified'])
        flag = StopWorkFlag.objects.get(session=self.session)
        self.assertEqual(flag.flagged_by, self.user.email)
        self.assertEqual(flag.reason, 'Concrete deviation beyond tolerance')
        mock_mail.assert_called_once()
        self.assertTrue(
            AuditEvent.objects.filter(
                action='stop_work_flagged', resource_id=str(self.session.id)
            ).exists()
        )

    def test_post_with_known_check_links_it(self):
        check = ComplianceCheck.objects.create(
            id='CHK-LINK', session=self.session, element='Beam',
            rule='Element Deviation ≤ 10mm', measured='30.0mm',
            status='fail', confidence='90%',
        )
        with mock.patch(
            'apps.notifications.email_service.EmailService.send_email',
            return_value={'success': True},
        ):
            response = self.client.post(
                self._url(), {'reason': 'stop', 'check_id': check.id}, format='json'
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['check_id'], check.id)
        flag = StopWorkFlag.objects.get(session=self.session)
        self.assertEqual(flag.compliance_check_id, check.id)

    def test_duplicate_active_flag_returned_unchanged(self):
        StopWorkFlag.objects.create(
            session=self.session, reason='already active', status='active'
        )
        response = self.client.post(self._url(), {'reason': 'another'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('already exists', response.data['detail'])
        self.assertEqual(StopWorkFlag.objects.filter(session=self.session).count(), 1)

    def test_lift_active_flag(self):
        flag = StopWorkFlag.objects.create(
            session=self.session, reason='stop', status='active'
        )
        response = self.client.patch(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        flag.refresh_from_db()
        self.assertEqual(flag.status, 'lifted')
        self.assertIsNotNone(flag.lifted_at)

    def test_lift_without_active_flag_returns_404(self):
        response = self.client.patch(self._url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Scan plan and scanner viewsets
# ---------------------------------------------------------------------------

class ScanPlanApiTests(BaseScansAPITest):
    def setUp(self):
        super().setUp()
        self.located_project = Project.objects.create(
            name='Located Project', latitude=6.5, longitude=3.4
        )

    def test_create_plan_inherits_project_coordinates(self):
        payload = {
            'project': str(self.located_project.id),
            'title': 'Floor 3 sweep',
            'target_area': 'Floor 3, Zone A',
            'sensors_used': ['lidar'],
        }
        response = self.client.post(reverse('scan_plan-list'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['latitude'], 6.5)
        self.assertEqual(response.data['longitude'], 3.4)
        self.assertEqual(response.data['project_name'], 'Located Project')

    def test_update_backfills_missing_coordinates(self):
        plan = ScanPlan.objects.create(
            project=self.located_project, title='Plan', target_area='Zone B'
        )
        self.assertIsNone(plan.latitude)
        response = self.client.patch(
            reverse('scan_plan-detail', kwargs={'pk': str(plan.id)}),
            {'title': 'Renamed'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertEqual(plan.title, 'Renamed')
        self.assertEqual(plan.latitude, 6.5)
        self.assertEqual(plan.longitude, 3.4)

    def test_list_detail_delete(self):
        plan = ScanPlan.objects.create(
            project=self.project, title='P1', target_area='Zone A'
        )
        response = self.client.get(reverse('scan_plan-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        detail_url = reverse('scan_plan-detail', kwargs={'pk': str(plan.id)})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'P1')

        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            self.client.get(detail_url).status_code, status.HTTP_404_NOT_FOUND
        )


class ScannerApiTests(BaseScansAPITest):
    def test_register_and_list_scanner_with_activity(self):
        response = self.client.post(
            reverse('scanner-list'),
            {'device_id': 'NAVIS-V3-001', 'model': 'Tersus MVP S1',
             'status': 'online', 'battery_level': 76},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['device_id'], 'NAVIS-V3-001')
        self.assertEqual(response.data['battery_level'], 76)

        ScanSession.objects.create(scanner_id='NAVIS-V3-001', status='completed')
        response = self.client.get(reverse('scanner-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        entry = response.data[0]
        self.assertEqual(entry['session_count'], 1)
        self.assertEqual(entry['latest_session_status'], 'completed')
        # Fields only a device can know stay null until it reports them.
        self.assertIsNone(entry['latitude'])
        self.assertIsNone(entry['last_seen'])

    def test_update_and_delete_scanner(self):
        scanner = Scanner.objects.create(device_id='DEV-UPD', status='online')
        detail_url = reverse('scanner-detail', kwargs={'pk': str(scanner.id)})
        response = self.client.patch(detail_url, {'status': 'offline'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        scanner.refresh_from_db()
        self.assertEqual(scanner.status, 'offline')

        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Scanner.objects.filter(id=scanner.id).exists())

    def test_duplicate_device_id_rejected(self):
        Scanner.objects.create(device_id='DEV-DUP')
        response = self.client.post(
            reverse('scanner-list'), {'device_id': 'DEV-DUP'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Services layer
# ---------------------------------------------------------------------------

class ScanServiceTests(BaseScansAPITest):
    def test_start_session_creates_session_and_audit_event(self):
        session = ScanService.start_session(
            {'scanner_id': 'svc_scanner', 'project': self.project}
        )
        self.assertEqual(session.status, 'initialized')
        self.assertEqual(session.scanner_id, 'svc_scanner')
        self.assertEqual(session.project_id, self.project.id)
        self.assertTrue(
            AuditEvent.objects.filter(
                action='SESSION_CREATED', resource_id=str(session.id)
            ).exists()
        )

    @patch('apps.processing.tasks.process_scan_pipeline.delay')
    def test_finalize_upload_dispatches_pipeline(self, mock_delay):
        ScanService.finalize_upload(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'processing')
        mock_delay.assert_called_once_with(str(self.session.id))
        self.assertTrue(
            AuditEvent.objects.filter(
                action='SCAN_STATUS_CHANGED', resource_id=str(self.session.id)
            ).exists()
        )

    @patch('apps.processing.tasks.process_scan_pipeline.delay',
           side_effect=RuntimeError('no broker'))
    def test_finalize_upload_survives_broker_outage(self, mock_delay):
        """A broker outage must not lose the status transition."""
        ScanService.finalize_upload(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'processing')


class RunBimAlignmentServiceTests(BaseScansAPITest):
    def test_no_bim_file_creates_unaligned_result(self):
        alignment = run_bim_alignment(self.session)
        self.assertEqual(alignment.alignment_status, 'SUCCESS')
        self.assertEqual(
            alignment.transformation_matrix,
            {'status': 'unaligned', 'reason': 'no_point_cloud'},
        )
        self.assertIsNone(alignment.mean_deviation)
        self.assertEqual(alignment.top_deviations, [])
        # No geometric measurements exist, so no compliance checks are invented.
        self.assertEqual(
            ComplianceCheck.objects.filter(session=self.session).count(), 0
        )

    def test_existing_alignment_is_replaced(self):
        BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='FAILED', mean_deviation=999.0
        )
        alignment = run_bim_alignment(self.session)
        self.assertEqual(BIMAlignmentResult.objects.filter(session=self.session).count(), 1)
        self.assertIsNone(alignment.mean_deviation)
        self.assertEqual(alignment.alignment_status, 'SUCCESS')


class GenerateComplianceChecksTests(BaseScansAPITest):
    def _alignment(self, **kwargs):
        defaults = dict(session=self.session, alignment_status='SUCCESS')
        defaults.update(kwargs)
        return BIMAlignmentResult.objects.create(**defaults)

    def test_checks_from_real_deviation_metrics(self):
        alignment = self._alignment(
            mean_deviation=5.0, max_deviation=25.0, min_deviation=1.0,
            top_deviations=[
                {'element': 'Column B2', 'deviation_mm': 8.0, 'confidence': 0.8},
                {'element': 'Beam C1', 'deviation_mm': 30.0},
            ],
        )
        DataFusionService.generate_compliance_checks(self.session, alignment)
        checks = ComplianceCheck.objects.filter(session=self.session)
        self.assertEqual(checks.count(), 4)
        # mean 5mm <= 15mm -> pass
        mean_check = checks.get(element='Global Structure')
        self.assertEqual(mean_check.status, 'pass')
        # max 25mm > 20mm -> fail
        max_check = checks.get(element='Structural Extremes')
        self.assertEqual(max_check.status, 'fail')
        # element deviations: 8mm passes with its own confidence, 30mm fails
        column = checks.get(element='Column B2')
        self.assertEqual(column.status, 'pass')
        self.assertEqual(column.confidence, '80%')
        beam = checks.get(element='Beam C1')
        self.assertEqual(beam.status, 'fail')
        self.assertEqual(beam.confidence, '95%')

    def test_replaces_existing_checks(self):
        alignment = self._alignment(mean_deviation=5.0)
        ComplianceCheck.objects.create(
            id='CHK-STALE', session=self.session, element='Stale', rule='old',
            measured='0mm', status='pass', confidence='10%',
        )
        DataFusionService.generate_compliance_checks(self.session, alignment)
        self.assertFalse(
            ComplianceCheck.objects.filter(id='CHK-STALE').exists()
        )
        self.assertEqual(
            ComplianceCheck.objects.filter(session=self.session).count(), 1
        )

    def test_hotspots_without_measurement_are_skipped(self):
        alignment = self._alignment(
            mean_deviation=5.0, max_deviation=8.0,
            top_deviations=[{'element': 'Thermal Variance', 'deviation_mm': None}],
        )
        DataFusionService.generate_compliance_checks(self.session, alignment)
        # Only the mean and max checks; the unmeasured hotspot contributes none.
        self.assertEqual(
            ComplianceCheck.objects.filter(session=self.session).count(), 2
        )

    def test_no_metrics_no_checks(self):
        alignment = self._alignment()
        DataFusionService.generate_compliance_checks(self.session, alignment)
        self.assertEqual(
            ComplianceCheck.objects.filter(session=self.session).count(), 0
        )


class ApplyThermalOverlayTests(BaseScansAPITest):
    def test_skips_session_without_thermal_data(self):
        DataFusionService.apply_thermal_overlay(self.session)
        self.assertEqual(ThermalAnomaly.objects.filter(session=self.session).count(), 0)

    def test_creates_anomalies_from_ai_findings(self):
        ScanSession.objects.filter(id=self.session.id).update(
            thermal_url='http://example.test/thermal.jpg'
        )
        self.session.refresh_from_db()
        findings = [{
            'temperature_variance': 4.2, 'severity': 'high',
            'location_x': 1.0, 'location_y': 2.0, 'location_z': 3.0,
        }]
        with mock.patch.object(AIService, 'detect_thermal_anomalies',
                               return_value=findings):
            DataFusionService.apply_thermal_overlay(self.session)
        anomaly = ThermalAnomaly.objects.get(session=self.session)
        self.assertEqual(anomaly.temperature_variance, 4.2)
        self.assertEqual(anomaly.severity, 'high')
        self.assertEqual(anomaly.image_url, 'http://example.test/thermal.jpg')

        # Re-running over the same findings must not duplicate rows.
        with mock.patch.object(AIService, 'detect_thermal_anomalies',
                               return_value=findings):
            DataFusionService.apply_thermal_overlay(self.session)
        self.assertEqual(ThermalAnomaly.objects.filter(session=self.session).count(), 1)


class _FakeUrlopenResponse:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._lines)


class CalculateProgressTests(BaseScansAPITest):
    def test_returns_none_without_gaussian_splat(self):
        self.assertIsNone(DataFusionService.calculate_progress(self.session))

    def test_returns_existing_result_without_recomputing(self):
        ScanFile.objects.create(
            session=self.session, file_type='gaussian_splat',
            file_url='http://example.test/scan.ply',
        )
        existing = ProgressValidationResult.objects.create(
            session=self.session, progress_score=0.5, covered_area_sqm=100.0
        )
        result = DataFusionService.calculate_progress(self.session)
        self.assertEqual(result.id, existing.id)
        self.assertEqual(result.progress_score, 0.5)

    def test_parses_ply_point_cloud(self):
        ScanFile.objects.create(
            session=self.session, file_type='gaussian_splat',
            file_url='http://example.test/scan.ply',
        )
        ply_lines = [
            b'ply\n', b'format ascii 1.0\n', b'end_header\n',
            b'0 0 0\n', b'10 20 2\n', b'not numbers\n',
        ]
        with mock.patch('urllib.request.urlopen',
                        return_value=_FakeUrlopenResponse(ply_lines)) as mock_open:
            result = DataFusionService.calculate_progress(self.session)
        mock_open.assert_called_once()
        # Real parsed extents: 10 x 20 x 2 -> area 200 sqm, volume 400 m3.
        self.assertEqual(result.progress_score, 0.05)
        self.assertEqual(result.covered_area_sqm, 200.0)
        self.assertEqual(result.volume_metrics['total_volume_m3'], 400.0)
        self.assertEqual(result.volume_metrics['completion_percentage'], 5.0)

    def test_ply_fetch_failure_still_records_zero_progress(self):
        ScanFile.objects.create(
            session=self.session, file_type='gaussian_splat',
            file_url='http://example.test/scan.ply',
        )
        with mock.patch('urllib.request.urlopen', side_effect=OSError('unreachable')):
            result = DataFusionService.calculate_progress(self.session)
        self.assertEqual(result.progress_score, 0.0)


class GenerateComplianceChecksCommandTests(BaseScansAPITest):
    def test_command_generates_checks_for_existing_alignments(self):
        BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='SUCCESS',
            mean_deviation=8.0, max_deviation=12.0,
        )
        out = io.StringIO()
        call_command('generate_compliance_checks', stdout=out)
        self.assertEqual(
            ComplianceCheck.objects.filter(session=self.session).count(), 2
        )
        self.assertIn('Successfully generated', out.getvalue())


# ---------------------------------------------------------------------------
# Utils (object-storage URL handling, bboxes, BIM file resolution)
# ---------------------------------------------------------------------------

class ExtractObjectKeyTests(BaseScansAPITest):
    def test_extracts_key_from_presigned_url(self):
        bucket = settings.CLOUDFLARE_R2_BUCKET_NAME
        url = f'https://abc.r2.cloudflarestorage.com/{bucket}/scans/session1/rgb/photo.jpg?X-Amz-Signature=abc'
        self.assertEqual(utils.extract_object_key(url), 'scans/session1/rgb/photo.jpg')

    def test_key_without_known_bucket_prefix(self):
        url = 'https://example.s3.amazonaws.com/other-bucket/scans/a.las?sig=1'
        self.assertEqual(utils.extract_object_key(url), 'other-bucket/scans/a.las')

    def test_url_decodes_the_key(self):
        bucket = settings.CLOUDFLARE_R2_BUCKET_NAME
        url = f'https://abc.r2.cloudflarestorage.com/{bucket}/scans/a%20b.ifc'
        self.assertEqual(utils.extract_object_key(url), 'scans/a b.ifc')

    def test_non_http_and_empty_return_none(self):
        self.assertIsNone(utils.extract_object_key('/media/scans/a.las'))
        self.assertIsNone(utils.extract_object_key(''))
        self.assertIsNone(utils.extract_object_key(None))


class RefreshStorageUrlTests(BaseScansAPITest):
    REMOTE = 'https://abc.r2.cloudflarestorage.com/bucket/scans/a.las?X-Amz-Signature=old'

    def test_passthrough_for_local_and_empty(self):
        self.assertEqual(utils.refresh_storage_url('/media/scans/a.las'),
                         '/media/scans/a.las')
        self.assertIsNone(utils.refresh_storage_url(None))
        self.assertEqual(utils.refresh_storage_url(''), '')

    def test_unknown_host_returned_unchanged(self):
        url = 'https://cdn.example.com/scans/a.las'
        self.assertEqual(utils.refresh_storage_url(url), url)

    def test_re_signs_remote_object(self):
        with mock.patch.object(utils, 'default_storage') as storage_mock:
            storage_mock.url.return_value = (
                'https://abc.r2.cloudflarestorage.com/bucket/scans/a.las'
                '?X-Amz-Signature=new'
            )
            fresh = utils.refresh_storage_url(self.REMOTE)
        self.assertIn('Signature=new', fresh)

    def test_storage_error_falls_back_to_original(self):
        with mock.patch.object(utils, 'default_storage') as storage_mock:
            storage_mock.url.side_effect = Exception('boom')
            self.assertEqual(utils.refresh_storage_url(self.REMOTE), self.REMOTE)

    def test_empty_fresh_url_falls_back_to_original(self):
        with mock.patch.object(utils, 'default_storage') as storage_mock:
            storage_mock.url.return_value = ''
            self.assertEqual(utils.refresh_storage_url(self.REMOTE), self.REMOTE)


class ExtractImageBboxTests(BaseScansAPITest):
    def test_valid_bbox(self):
        item = {'image_bbox': {'xmin': 0.1, 'ymin': 0.2, 'xmax': 0.5, 'ymax': 0.8}}
        self.assertEqual(
            utils.extract_image_bbox(item),
            {'bbox_xmin': 0.1, 'bbox_ymin': 0.2, 'bbox_xmax': 0.5, 'bbox_ymax': 0.8},
        )

    def test_non_dict_item(self):
        self.assertEqual(utils.extract_image_bbox('nope'), {})

    def test_missing_bbox(self):
        self.assertEqual(utils.extract_image_bbox({'type': 'crack'}), {})

    def test_non_numeric_values(self):
        item = {'image_bbox': {'xmin': 'left', 'ymin': 0.2, 'xmax': 0.5, 'ymax': 0.8}}
        self.assertEqual(utils.extract_image_bbox(item), {})

    def test_out_of_range_values(self):
        item = {'image_bbox': {'xmin': 1.5, 'ymin': 0.2, 'xmax': 0.5, 'ymax': 0.8}}
        self.assertEqual(utils.extract_image_bbox(item), {})

    def test_inverted_box(self):
        item = {'image_bbox': {'xmin': 0.5, 'ymin': 0.2, 'xmax': 0.2, 'ymax': 0.8}}
        self.assertEqual(utils.extract_image_bbox(item), {})


class ResolveSessionBimFileTests(BaseScansAPITest):
    def test_no_bim_file(self):
        self.assertEqual(
            utils.resolve_session_bim_file(self.session), (None, None, False)
        )

    def test_local_bim_file_found_with_extension(self):
        stored = default_storage.save(
            f'scans/{self.session.id}/bim/model.rvt', ContentFile(b'rvt bytes')
        )
        ScanFile.objects.create(
            session=self.session, file_type='bim', file_url=f'/media/{stored}'
        )
        path, ext, is_temp = utils.resolve_session_bim_file(self.session)
        self.assertEqual(ext, '.rvt')
        self.assertFalse(is_temp)
        self.assertTrue(os.path.exists(path))

    def test_local_bim_file_missing_on_disk(self):
        ScanFile.objects.create(
            session=self.session, file_type='bim',
            file_url='/media/scans/missing/model.ifc',
        )
        self.assertEqual(
            utils.resolve_session_bim_file(self.session), (None, None, False)
        )

    def test_remote_bim_file_downloaded_to_temp(self):
        remote = 'https://abc.r2.cloudflarestorage.com/bucket/scans/x/model.ifc?sig=1'
        ScanFile.objects.create(session=self.session, file_type='bim', file_url=remote)
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.return_value = iter([b'ifc data'])
        with mock.patch('requests.get', return_value=response) as mock_get:
            path, ext, is_temp = utils.resolve_session_bim_file(self.session)
        mock_get.assert_called_once()
        self.assertTrue(is_temp)
        self.assertEqual(ext, '.ifc')
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'ifc data')
        os.remove(path)

    def test_remote_download_failure(self):
        ScanFile.objects.create(
            session=self.session, file_type='bim',
            file_url='https://abc.r2.cloudflarestorage.com/bucket/scans/x/model.ifc',
        )
        with mock.patch('requests.get', side_effect=Exception('network down')):
            self.assertEqual(
                utils.resolve_session_bim_file(self.session), (None, None, False)
            )


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

class ScanSelectorTests(BaseScansAPITest):
    def test_get_session_returns_session(self):
        found = ScanSelector.get_session(self.session.id)
        self.assertEqual(found.id, self.session.id)
        self.assertEqual(found.scanner_id, 'scanner_cov_001')

    def test_get_session_unknown_raises_http404(self):
        with self.assertRaises(Http404):
            ScanSelector.get_session(uuid.uuid4())

    def test_get_project_scans(self):
        other_project = Project.objects.create(name='Elsewhere')
        ScanSession.objects.create(scanner_id='foreign', project=other_project)
        scans = ScanSelector.get_project_scans(self.project.id)
        self.assertEqual(scans.count(), 1)
        self.assertEqual(scans.first().id, self.session.id)

    def test_get_defects(self):
        other = ScanSession.objects.create(scanner_id='other')
        Defect.objects.create(session=other, type='crack', severity='low')
        Defect.objects.create(session=self.session, type='corrosion', severity='high')
        defects = ScanSelector.get_defects(self.session.id)
        self.assertEqual(defects.count(), 1)
        self.assertEqual(defects.first().type, 'corrosion')

    def test_get_thermal_anomalies(self):
        ThermalAnomaly.objects.create(
            session=self.session, temperature_variance=3.3, severity='low'
        )
        anomalies = ScanSelector.get_thermal_anomalies(self.session.id)
        self.assertEqual(anomalies.count(), 1)
        self.assertEqual(anomalies.first().temperature_variance, 3.3)

    def test_get_bim_alignment(self):
        self.assertIsNone(ScanSelector.get_bim_alignment(self.session.id))
        alignment = BIMAlignmentResult.objects.create(
            session=self.session, alignment_status='SUCCESS'
        )
        self.assertEqual(ScanSelector.get_bim_alignment(self.session.id).id, alignment.id)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class SessionResponseSerializerTests(BaseScansAPITest):
    def _session_with_window(self, session_status, delta=None):
        session = ScanSession.objects.create(
            scanner_id='duration_scanner', status=session_status
        )
        base = timezone.now()
        update = {'updated_at': base}
        if delta is not None:
            update['created_at'] = base - delta
        else:
            update['created_at'] = base
        ScanSession.objects.filter(id=session.id).update(**update)
        session.refresh_from_db()
        return session

    def test_duration_in_progress_states(self):
        for session_status in ('initialized', 'uploading'):
            with self.subTest(status=session_status):
                session = self._session_with_window(session_status,
                                                    datetime.timedelta(minutes=30))
                self.assertEqual(
                    SessionResponseSerializer(session).data['duration'], 'In progress'
                )

    def test_duration_under_a_minute(self):
        session = self._session_with_window('completed', datetime.timedelta(seconds=30))
        self.assertEqual(SessionResponseSerializer(session).data['duration'], '<1 min')

    def test_duration_minutes(self):
        session = self._session_with_window('completed', datetime.timedelta(minutes=5))
        self.assertEqual(SessionResponseSerializer(session).data['duration'], '5 min')

    def test_duration_hours_and_minutes(self):
        session = self._session_with_window('completed', datetime.timedelta(minutes=125))
        self.assertEqual(
            SessionResponseSerializer(session).data['duration'], '2h 5m'
        )

    def test_duration_zero_window_is_none(self):
        session = self._session_with_window('completed')
        self.assertIsNone(SessionResponseSerializer(session).data['duration'])

    def test_operator_and_confidence_from_related_records(self):
        ScanMetadata.objects.create(session=self.session, operator_id='op-42')
        QualityReport.objects.create(scan=self.session, overall_ai_confidence=0.87)
        data = SessionResponseSerializer(self.session).data
        self.assertEqual(data['operator'], 'op-42')
        self.assertEqual(data['overall_ai_confidence'], 0.87)
        self.assertEqual(data['metadata']['operator_id'], 'op-42')
        self.assertEqual(data['project_name'], 'Scans Coverage Project')

    def test_upload_url_generated_via_storage_service(self):
        with mock.patch(
            'apps.storage.services.StorageService.generate_presigned_upload_url',
            return_value='https://r2.example.test/presigned',
        ) as mock_gen:
            data = SessionResponseSerializer(self.session).data
        self.assertEqual(data['upload_url'], 'https://r2.example.test/presigned')
        mock_gen.assert_called_once_with(str(self.session.id), 'raw_scan')

    def test_upload_url_none_when_storage_unavailable(self):
        with mock.patch(
            'apps.storage.services.StorageService.generate_presigned_upload_url',
            side_effect=ValueError('S3 Client not configured'),
        ):
            data = SessionResponseSerializer(self.session).data
        self.assertIsNone(data['upload_url'])


class ScanMetadataSerializerTests(BaseScansAPITest):
    def test_representation_nests_location(self):
        metadata = ScanMetadata.objects.create(
            session=self.session, latitude=6.5, longitude=3.4,
            operator_id='op', notes='n',
        )
        data = ScanMetadataSerializer(metadata).data
        self.assertEqual(data['location'], {'latitude': 6.5, 'longitude': 3.4})
        self.assertEqual(data['latitude'], 6.5)
        self.assertEqual(data['longitude'], 3.4)

    def test_representation_without_coordinates(self):
        metadata = ScanMetadata.objects.create(session=self.session, operator_id='op')
        data = ScanMetadataSerializer(metadata).data
        self.assertIsNone(data['location'])

    def test_update_merges_nested_location(self):
        metadata = ScanMetadata.objects.create(
            session=self.session, latitude=1.0, longitude=2.0, elevation=5.0
        )
        serializer = ScanMetadataSerializer(
            metadata, data={'location': {'latitude': 9.0}}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        metadata.refresh_from_db()
        self.assertEqual(metadata.latitude, 9.0)
        self.assertEqual(metadata.longitude, 2.0)
        self.assertEqual(metadata.elevation, 5.0)


class ScanFileSerializerTests(BaseScansAPITest):
    def test_content_url_and_local_file_url_passthrough(self):
        scan_file = ScanFile.objects.create(
            session=self.session, file_type='lidar',
            file_url='/media/scans/a.las', file_name='a.las',
        )
        data = ScanFileSerializer(scan_file).data
        self.assertEqual(
            data['content_url'],
            f'/api/v1/scans/{self.session.id}/files/{scan_file.id}/content/',
        )
        # Local media URLs carry no signature, so they pass through unchanged.
        self.assertEqual(data['file_url'], '/media/scans/a.las')


class StopWorkFlagSerializerTests(BaseScansAPITest):
    def test_unknown_check_id_rejected(self):
        serializer = StopWorkFlagSerializer(
            data={'session_id': str(self.session.id), 'reason': 'x',
                  'check_id': 'CHK-NOPE'}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('check_id', serializer.errors)

    def test_known_check_linked(self):
        check = ComplianceCheck.objects.create(
            id='CHK-SER', session=self.session, element='Slab',
            rule='Max Deviation ≤ 20mm', measured='30.0mm',
            status='fail', confidence='90%',
        )
        serializer = StopWorkFlagSerializer(
            data={'session_id': str(self.session.id), 'reason': 'x',
                  'check_id': check.id}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['compliance_check'], check)

    def test_flagged_by_name_fallback(self):
        flag = StopWorkFlag.objects.create(session=self.session, reason='x')
        self.assertEqual(
            StopWorkFlagSerializer(flag).data['flagged_by_name'], 'Compliance dashboard'
        )


class DefectSerializerTests(BaseScansAPITest):
    def test_create_with_image_upload_uses_cloudinary(self):
        image = SimpleUploadedFile('crack.jpg', b'image-bytes')
        with mock.patch(
            'apps.storage.cloudinary_service.CloudinaryService.upload_file',
            return_value='https://res.cloudinary.test/defects/crack.jpg',
        ) as mock_upload:
            serializer = DefectSerializer(
                data={'type': 'crack', 'severity': 'high', 'description': 'x',
                      'image': image}
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            defect = serializer.save(session=self.session)
        mock_upload.assert_called_once_with(image, folder='defects')
        self.assertEqual(defect.image_url, 'https://res.cloudinary.test/defects/crack.jpg')
        self.assertEqual(defect.type, 'crack')


class ScanUploadRequestSerializerTests(SimpleTestCase):
    def test_valid_payload(self):
        serializer = ScanUploadRequestSerializer(data={
            'session_id': str(uuid.uuid4()),
            'file_type': 'lidar',
            'file': SimpleUploadedFile('a.las', b'data'),
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_file_type(self):
        serializer = ScanUploadRequestSerializer(data={
            'session_id': str(uuid.uuid4()),
            'file_type': 'video',
            'file': SimpleUploadedFile('a.mp4', b'data'),
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('file_type', serializer.errors)

    def test_chunk_number_must_be_positive(self):
        serializer = ScanUploadRequestSerializer(data={
            'session_id': str(uuid.uuid4()),
            'file_type': 'lidar',
            'file': SimpleUploadedFile('a.las', b'data'),
            'chunk_number': 0,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('chunk_number', serializer.errors)


class ScannerHeartbeatSerializerTests(SimpleTestCase):
    def test_valid_payload(self):
        serializer = ScannerHeartbeatSerializer(data={
            'battery_level': 55, 'status': 'online',
            'firmware_version': '1.2.3', 'latitude': 6.5, 'longitude': 3.4,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_battery_out_of_range(self):
        serializer = ScannerHeartbeatSerializer(data={'battery_level': 150})
        self.assertFalse(serializer.is_valid())
        self.assertIn('battery_level', serializer.errors)

    def test_latitude_out_of_range(self):
        serializer = ScannerHeartbeatSerializer(data={'latitude': 95.0})
        self.assertFalse(serializer.is_valid())
        self.assertIn('latitude', serializer.errors)

    def test_unknown_status(self):
        serializer = ScannerHeartbeatSerializer(data={'status': 'sleeping'})
        self.assertFalse(serializer.is_valid())
        self.assertIn('status', serializer.errors)
