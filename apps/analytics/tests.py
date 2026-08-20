from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.analytics.models import (
    GeneratedReport, DepartmentPerformanceMetric,
    OfficerPerformanceRecord, RiskAssessmentAlert
)
from apps.analytics.services import AnalyticsService

User = get_user_model()

class AnalyticsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='analytics_officer',
            email='analytics@government.gov.ng',
            password='Password123!',
            first_name='Director',
            last_name='General'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Central Metro Transit Hub',
            reference_number='PRJ-2026-HUB',
            lga='Ikeja',
            status='Active'
        )

    def test_generate_report_custom_modules(self):
        """Test generating a custom PDF/CSV report with multi-module inclusion."""
        report = AnalyticsService.generate_report({
            "title": "Quarterly Agency Safety & EVM Audit",
            "format": "PDF",
            "modules_included": ["Project Performance", "Inspection Analytics", "Compliance & Regulatory"],
            "period_start": "2026-07-01",
            "period_end": "2026-09-30"
        }, self.user)

        self.assertIsNotNone(report.id)
        self.assertEqual(report.status, 'Ready')
        self.assertEqual(report.format, 'PDF')
        self.assertEqual(len(report.modules_included), 3)
        self.assertTrue(report.file_url.endswith('.pdf'))

    def test_get_executive_kpis(self):
        """Test computing cross-module executive scorecard metrics."""
        res = self.client.get('/api/v1/analytics/overview/executive-kpis/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('active_projects_count', res.data)
        self.assertIn('issued_permits_count', res.data)
        self.assertIn('completed_inspections_count', res.data)
        self.assertIn('total_revenue_collected', res.data)

    def test_department_performance_metrics(self):
        """Test department turnaround SLAs and efficiency metrics."""
        res = self.client.get('/api/v1/analytics/departments/')
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.data), 4)
        dept_names = [d['department_name'] for d in res.data]
        self.assertIn('Structural Engineering', dept_names)

    def test_officer_performance_league_table(self):
        """Test inspector & reviewer performance rankings."""
        res = self.client.get('/api/v1/analytics/officers/')
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.data), 4)
        self.assertEqual(res.data[0]['rank'], 1)

    def test_risk_assessments_and_mitigation(self):
        """Test structural collapse risk alert mitigation workflow."""
        alerts = AnalyticsService.get_risk_assessments()
        first_alert = alerts.first()
        self.assertIsNotNone(first_alert)

        res = self.client.post(f'/api/v1/analytics/risk/{first_alert.id}/mitigate/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Mitigated')

    def test_financial_summary(self):
        """Test revenue and fee collections endpoint."""
        res = self.client.get('/api/v1/analytics/overview/financial-summary/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_revenue', res.data)
        self.assertIn('permit_fees', res.data)
        self.assertIn('monthly_breakdown', res.data)
