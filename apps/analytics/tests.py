from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.monitoring.models import ConstructionMilestone
from apps.analytics.models import GeneratedReport, RiskAssessmentAlert
from apps.analytics.services import AnalyticsService, StructuralRiskService, PerformanceAnalyticsService

User = get_user_model()

class AnalyticsTestCase(TestCase):
    def setUp(self):
        self.director_user = User.objects.create_user(
            username='director_analytics',
            email='director@government.gov.ng',
            password='Password123!',
            first_name='Director',
            last_name='General'
        )
        self.contractor_user = User.objects.create_user(
            username='contractor_user',
            email='contractor@buildcorp.ng',
            password='Password123!',
            first_name='John',
            last_name='Contractor'
        )
        # Give contractor standard non-privileged role
        if hasattr(self.contractor_user, 'role'):
            self.contractor_user.role = 'CONTRACTOR'
            self.contractor_user.save()

        self.client = APIClient()
        self.client.force_authenticate(user=self.director_user)

        self.project = Project.objects.create(
            name='Eko Atlantic Marina Tower',
            reference_number='PRJ-2026-EKO',
            lga='Victoria Island',
            status='Active'
        )

        ConstructionMilestone.objects.create(
            project=self.project,
            name='Foundation Works',
            target_date='2026-03-31',
            status='Verified'
        )

    def test_performance_analytics_aggregation(self):
        """Test performance analytics returns portfolio projects and SPI/CPI indices."""
        res = self.client.get('/api/v1/analytics/performance/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_projects', res.data)
        self.assertIn('projects', res.data)
        self.assertTrue(len(res.data['projects']) > 0)

    def test_structural_risk_index_engine(self):
        """Test deterministic structural risk engine returns scores and traceable contributors."""
        res = self.client.get('/api/v1/analytics/risk/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('average_risk_score', res.data)
        self.assertIn('hotspot_structures', res.data)
        if len(res.data['hotspot_structures']) > 0:
            first_hotspot = res.data['hotspot_structures'][0]
            self.assertIn('contributors', first_hotspot)
            self.assertIn('risk_score', first_hotspot)

    def test_construction_progress_analytics(self):
        """Test construction progress returns EVM metrics and milestone timeline."""
        res = self.client.get('/api/v1/analytics/progress/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('planned_progress_percentage', res.data)
        self.assertIn('evm', res.data)
        self.assertIn('timeline', res.data)

    def test_inspection_and_compliance_analytics(self):
        """Test inspection and compliance data endpoints."""
        res_insp = self.client.get('/api/v1/analytics/inspections/')
        self.assertEqual(res_insp.status_code, 200)
        self.assertIn('pass_rate_percentage', res_insp.data)
        self.assertIn('defect_categories', res_insp.data)

        res_comp = self.client.get('/api/v1/analytics/compliance/')
        self.assertEqual(res_comp.status_code, 200)
        self.assertIn('compliance_rate_percentage', res_comp.data)
        self.assertIn('open_ncrs_count', res_comp.data)

    def test_industry_analytics_permission_enforcement(self):
        """Test analytics.view_industry server-side enforcement (403 for unauthorized users)."""
        # Director has access
        res_auth = self.client.get('/api/v1/analytics/industry/')
        self.assertEqual(res_auth.status_code, 200)
        self.assertIn('sector_distribution', res_auth.data)

        # Unauthenticated gets 401
        self.client.force_authenticate(user=None)
        res_unauth = self.client.get('/api/v1/analytics/industry/')
        self.assertEqual(res_unauth.status_code, 401)

        # Restricted contractor gets 403
        self.client.force_authenticate(user=self.contractor_user)
        res_forbidden = self.client.get('/api/v1/analytics/industry/')
        self.assertEqual(res_forbidden.status_code, 403)

    def test_report_generation_and_download(self):
        """Test generating report and fetching download url."""
        self.client.force_authenticate(user=self.director_user)
        res_create = self.client.post('/api/v1/analytics/reports/', {
            "title": "Comprehensive Building Safety Report Q3",
            "format": "PDF",
            "modules_included": ["Project Performance", "Structural Risk Assessment"]
        })
        self.assertEqual(res_create.status_code, 201)
        report_id = res_create.data['id']

        res_dl = self.client.get(f'/api/v1/analytics/reports/{report_id}/download/')
        self.assertEqual(res_dl.status_code, 200)
        self.assertIn('download_url', res_dl.data)
