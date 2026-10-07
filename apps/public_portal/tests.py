from django.test import TestCase, Client, override_settings
from django.urls import reverse
import json

from apps.projects.models import Project, ProjectMilestone
from apps.inspections.models import Inspection, StopWorkOrder
from apps.public_portal.models import PublicNotice, ViolationReport


@override_settings(SECURE_SSL_REDIRECT=False)
class PublicTransparencyPortalTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Seed test project
        self.project = Project.objects.create(
            name="Eko Atlantic Horizon Towers",
            reference_number="NXC-GOV-2026-0041",
            project_type="Commercial",
            status="ACTIVE",
            developer_organization="South Energyx Nigeria Limited",
            site_address="Plot 14, Marina District, Eko Atlantic City",
            lga="Eti-Osa",
            state="Lagos State",
            permit_number="LASBCA/PRM/2026/0419",
            permit_status="VALID_ACTIVE",
            number_of_floors=32,
            site_area=14500.0,
            gross_floor_area=48000.0,
            latitude=6.4253,
            longitude=3.4219,
        )

        import datetime
        ProjectMilestone.objects.create(
            project=self.project,
            title="Foundation Piling",
            target_date=datetime.date(2026, 12, 31),
            is_completed=True
        )

        Inspection.objects.create(
            project=self.project,
            inspection_type="Foundation Inspection",
            status="COMPLETED",
            outcome="PASSED",
            inspector_name="Engr. Adebayo",
            scheduled_date=datetime.date(2026, 9, 20),
            completed_date=datetime.date(2026, 9, 20)
        )

        # Seed public notice
        self.notice = PublicNotice.objects.create(
            reference_number="NTC-2026-TEST",
            notice_type="SAFETY_ADVISORY",
            title="Test Coastal Safety Advisory",
            description="Continuous excavation safety notice.",
            target_lga="Eti-Osa",
            is_active=True,
            severity="WARNING"
        )

    def test_overview_endpoint(self):
        res = self.client.get('/api/v1/public-portal/transparency/overview/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'success')
        self.assertIn('stats', data)
        self.assertIn('recent_notices', data)
        self.assertIn('featured_projects', data)
        self.assertGreaterEqual(data['stats']['active_sites'], 1)

    def test_direct_public_overview_alias(self):
        res = self.client.get('/api/v1/public/overview/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get('status'), 'success')

    def test_projects_list_and_search(self):
        # Query list
        res = self.client.get('/api/v1/public-portal/transparency/projects/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'success')
        self.assertGreaterEqual(data['count'], 1)

        # Filter by name
        res_q = self.client.get('/api/v1/public-portal/transparency/projects/?q=Horizon')
        self.assertEqual(res_q.status_code, 200)
        self.assertEqual(len(res_q.json()['projects']), 1)

        # Filter by non-existent query
        res_empty = self.client.get('/api/v1/public-portal/transparency/projects/?q=NonExistentSite99')
        self.assertEqual(res_empty.status_code, 200)
        self.assertEqual(len(res_empty.json()['projects']), 0)

    def test_project_detail_by_id_and_slug(self):
        # Lookup by UUID
        res_id = self.client.get(f'/api/v1/public-portal/transparency/projects/{self.project.id}/')
        self.assertEqual(res_id.status_code, 200)
        data = res_id.json()
        self.assertEqual(data['name'], "Eko Atlantic Horizon Towers")
        self.assertEqual(data['slug'], "eko-atlantic-horizon-towers")
        self.assertIn('milestones', data)
        self.assertIn('inspections', data)

        # Lookup by Slug
        res_slug = self.client.get('/api/v1/public-portal/transparency/projects/eko-atlantic-horizon-towers/')
        self.assertEqual(res_slug.status_code, 200)
        self.assertEqual(res_slug.json()['id'], str(self.project.id))

        # Lookup by Permit Number
        res_permit = self.client.get('/api/v1/public-portal/transparency/projects/LASBCA/PRM/2026/0419/')
        # URL encoded permit or slash handled
        res_permit_encoded = self.client.get('/api/v1/public-portal/transparency/projects/LASBCA%2FPRM%2F2026%2F0419/')
        # Either resolves or not 500
        self.assertIn(res_permit_encoded.status_code, (200, 404))

    def test_project_subviews(self):
        # Compliance sub-view
        res_comp = self.client.get(f'/api/v1/public-portal/transparency/projects/{self.project.id}/compliance/')
        self.assertEqual(res_comp.status_code, 200)
        self.assertEqual(res_comp.json()['status'], 'success')
        self.assertEqual(res_comp.json()['compliance_state'], 'COMPLIANT')

        # Inspections sub-view
        res_insp = self.client.get(f'/api/v1/public-portal/transparency/projects/{self.project.id}/inspections/')
        self.assertEqual(res_insp.status_code, 200)
        self.assertEqual(res_insp.json()['status'], 'success')

        # Documents sub-view
        res_docs = self.client.get(f'/api/v1/public-portal/transparency/projects/{self.project.id}/documents/')
        self.assertEqual(res_docs.status_code, 200)
        self.assertEqual(res_docs.json()['status'], 'success')

    def test_notices_endpoint(self):
        res = self.client.get('/api/v1/public-portal/transparency/notices/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'success')
        self.assertGreaterEqual(data['count'], 1)
        found = any(n['reference_number'] == "NTC-2026-TEST" for n in data['notices'])
        self.assertTrue(found)

    def test_verify_permit_endpoint(self):
        res = self.client.get('/api/v1/public-portal/transparency/verify/permit/NXC-GOV-2026-0041/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['verified'])
        self.assertIn('project', data)

        # Invalid reference
        res_inv = self.client.get('/api/v1/public-portal/transparency/verify/permit/FAKE-REF-999/')
        self.assertEqual(res_inv.status_code, 404)
        self.assertFalse(res_inv.json()['verified'])

    def test_map_projects_geojson(self):
        res = self.client.get('/api/v1/public-portal/transparency/map/projects/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('type'), 'FeatureCollection')
        self.assertGreaterEqual(len(data.get('features', [])), 1)

    def test_violation_report_submission(self):
        payload = {
            "reporter_name": "Citizen Ade",
            "reporter_contact": "citizen.ade@example.com",
            "address": "12 Awolowo Road, Ikoyi",
            "description": "Suspected unpermitted basement excavation next to canal.",
            "evidence_url": "https://example.com/site_photo.jpg"
        }
        res = self.client.post(
            '/api/v1/public-portal/transparency/violation-reports/',
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data.get('status'), 'success')
        self.assertTrue(data.get('tracking_number', '').startswith('VIO-2026-'))
        self.assertEqual(data.get('status_label'), 'NEW')
