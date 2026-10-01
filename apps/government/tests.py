import uuid
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from apps.government.models import Agency, District
from apps.projects.models import Project
from apps.evidence.models import EvidenceRecord

User = get_user_model()

@override_settings(SECURE_SSL_REDIRECT=False)
class InspectorMeAndEvidenceTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.inspector_user = User.objects.create_user(
            username='inspector.test',
            email='inspector.test@nexucon.net',
            password='TestPassword123!',
            first_name='Babatunde',
            last_name='Fashola'
        )
        self.agency = Agency.objects.create(name='LASBCA', code='LASBCA')
        self.district = District.objects.create(name='Lekki-Epe Zonal Directorate', code='LEK-EPE')

        # Create stakeholder Inspector record
        from apps.stakeholders.models import Inspector
        self.stakeholder_ins = Inspector.objects.create(
            user=self.inspector_user,
            name='Babatunde Fashola',
            inspector_id='LAG-INS-7788',
            assigned_zone='Lekki-Epe Zonal Directorate'
        )

        self.project = Project.objects.create(
            name='Eko Atlantic Marina Tower',
            reference_number='PRJ-EKO-001',
            status='Active'
        )

        # Create test evidence record
        self.evidence = EvidenceRecord.objects.create(
            project=self.project,
            source_type='scan_defect',
            structural_element_id='COL-C12'
        )

    def test_inspector_me_endpoint_returns_accreditation(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/government/inspectors/me/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['badge_number'], 'LAG-INS-7788')
        self.assertEqual(data['full_name'], 'Babatunde Fashola')
        self.assertEqual(data['directorate'], 'Lekki-Epe Zonal Directorate')
        self.assertEqual(data['accreditation_status'], 'ACTIVE')
        self.assertFalse(data['is_suspended'])

    def test_unaccredited_user_returns_404(self):
        regular_user = User.objects.create_user(
            username='civilian',
            email='civilian@example.com',
            password='Password123!'
        )
        self.client.force_authenticate(user=regular_user)
        res = self.client.get('/api/v1/government/inspectors/me/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        data = res.json()
        self.assertEqual(data['reason'], 'NOT_ACCREDITED')

    def test_evidence_records_with_photo_source_type_filter(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/evidence/records/?source_type=photo')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_evidence_records_with_all_filter(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/evidence/records/?source_type=ALL')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
