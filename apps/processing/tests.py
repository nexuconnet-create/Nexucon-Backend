from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from apps.scans.models import ScanSession
from django.contrib.auth.models import User

class ProcessingE2ETestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='testuser', password='password')
        self.client.force_authenticate(user=self.user)
        # Create a mock session
        self.session = ScanSession.objects.create(
            scanner_id="TEST-SCANNER-001",
            status="initialized"
        )
        
    def test_edge_sync_endpoint(self):
        """Test that edge sync adds defects correctly."""
        url = reverse('edge_sync', kwargs={'session_id': str(self.session.id)})
        payload = {
            "edge_defects": [
                {
                    "type": "crack",
                    "severity": "high",
                    "location_x": 1.5,
                    "location_y": 2.5,
                    "location_z": 3.0,
                    "description": "Deep crack detected by edge model",
                    "confidence_score": 0.95
                }
            ]
        }
        
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['synced_count'], 1)
        self.assertEqual(self.session.defects.count(), 1)
        self.assertEqual(self.session.defects.first().severity, 'high')

    def test_bim_alignment_pipeline(self):
        """Test the BIM alignment pipeline."""
        url = f"/api/v1/scans/{self.session.id}/align-bim/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("message", response.data)

    def test_clash_detection_pipeline(self):
        """Test the Clash detection pipeline."""
        url = f"/api/v1/scans/{self.session.id}/clash/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("clashes", response.data)
