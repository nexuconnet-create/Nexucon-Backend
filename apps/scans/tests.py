from unittest.mock import patch
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth.models import User
from apps.projects.models import Project
from rest_framework_simplejwt.tokens import RefreshToken
from .models import ScanSession, ScanMetadata
import uuid


class ScanIntegrationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpassword')
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

    @patch("apps.storage.cloudinary_service.CloudinaryService.upload_file")
    def test_upload_data_endpoint(self, mock_upload):
        mock_upload.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.jpg"
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('upload_lidar', kwargs={'session_id': str(session.id)})

        from django.core.files.uploadedfile import SimpleUploadedFile
        file_obj = SimpleUploadedFile("file.txt", b"file_content")
        response = self.client.post(url, {"file": file_obj})
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("url", response.data)
        self.assertTrue(response.data["url"].startswith("https://res.cloudinary.com/"))

    def test_align_bim_endpoint(self):
        session = ScanSession.objects.create(scanner_id="test_scanner")
        url = reverse('align_bim', kwargs={'session_id': str(session.id)})

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "COMPLETED")

        # Verify deviation analysis retrieval
        dev_url = reverse('deviation_analysis', kwargs={'session_id': str(session.id)})
        response = self.client.get(dev_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["mean_deviation"], 0.0)

    def test_list_scans_by_project(self):
        # Create a scan linked to the project
        session = ScanSession.objects.create(scanner_id='scanner_001', project=self.project)
        url = reverse('project_scans', kwargs={'project_id': self.project_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(str(session.id) == item['id'] for item in response.data))
