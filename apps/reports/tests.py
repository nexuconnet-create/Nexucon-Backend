from unittest.mock import patch
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
from apps.scans.models import ScanSession, Defect, ThermalAnomaly
from apps.reports.models import QualityReport
import uuid

User = get_user_model()

class ReportIntegrationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testreportuser", email="testreportuser@test.com", password="testpassword"
        )
        refresh = RefreshToken.for_user(self.user)
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

        # Create a completed scan session
        self.session = ScanSession.objects.create(
            scanner_id="TER-S1-TEST",
            status="completed",
            sensors_used=["lidar", "rgb"],
        )

        # Add a structural defect
        Defect.objects.create(
            session=self.session,
            type="crack",
            severity="high",
            status="OPEN",
            description="Test crack",
        )

        self.generate_report_url = reverse(
            "generate_report", kwargs={"session_id": str(self.session.id)}
        )
        self.download_report_url = reverse(
            "download_report",
            kwargs={"session_id": str(self.session.id), "file_format": "pdf"},
        )

    @patch("apps.storage.cloudinary_service.CloudinaryService.upload_file")
    @patch("apps.reports.tasks.generate_report_task.delay")
    def test_generate_report(self, mock_delay, mock_pdf):
        mock_pdf.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.pdf"
        """
        POST to generate_report should synchronously build the QA/QC report and
        return 201 with the full report payload.  The async Celery task is mocked
        so the test never needs a broker.
        """
        mock_delay.return_value = None  # simulate task successfully enqueued

        response = self.client.post(self.generate_report_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["defect_count"], 1)
        self.assertEqual(response.data["anomaly_count"], 0)
        self.assertIn("recommendations", response.data)
        self.assertTrue(len(response.data["recommendations"]) > 0)

        # Verify QualityReport persisted in DB
        self.assertTrue(
            QualityReport.objects.filter(scan=self.session).exists(),
            "QualityReport was not saved to the database.",
        )

        # -----------------------------------------------------------------
        # GET download_report — should return 200 and the PDF since it's generated on the fly
        # -----------------------------------------------------------------
        response = self.client.get(self.download_report_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')

    @patch("apps.storage.cloudinary_service.CloudinaryService.upload_file")
    @patch("apps.reports.tasks.generate_report_task.delay")
    def test_generate_report_idempotent(self, mock_delay, mock_pdf):
        mock_pdf.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.pdf"
        """
        A second POST when a completed report already exists returns 200
        (not 201) and does not create a duplicate report.
        """
        mock_delay.return_value = None
        self.client.post(self.generate_report_url)  # creates the report

        response = self.client.post(self.generate_report_url)  # idempotent call
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            QualityReport.objects.filter(scan=self.session).count(),
            1,
            "Expected exactly one report after two POSTs.",
        )

    def test_download_report_before_generation(self):
        """
        GET download_report before any report has been generated must return 404.
        """
        response = self.client.get(self.download_report_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("error", response.data)
