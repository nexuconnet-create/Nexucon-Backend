from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.compliance.models import (
    NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement,
    ComplianceReview, ComplianceCertificate, EscalationRule
)
from apps.compliance.services import ComplianceService
from apps.audit.models import AuditEvent
from apps.notifications.models import Notification

User = get_user_model()

class ComplianceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='compliance_officer',
            email='officer@government.gov.ng',
            password='Password123!',
            first_name='John',
            last_name='Doe'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Downtown Metro Station',
            reference_number='PRJ-2026-METRO',
            lga='Ikeja',
            status='Active'
        )

    def test_create_ncr_creates_linked_capa_and_audit_and_notification(self):
        """Test logging an NCR automatically generates a linked CAPA, audit event, and notification."""
        ncr = ComplianceService.create_ncr({
            "project_id": self.project.id,
            "title": "Improper Scaffold Tie-offs at Sector 4",
            "severity": "Major",
            "category": "Safety",
            "reported_by_name": "J. Doe (Safety)"
        }, self.user)

        self.assertIsNotNone(ncr.id)
        self.assertEqual(ncr.status, 'Open')
        self.assertEqual(ncr.escalation_level, 1)
        self.assertEqual(ncr.capas.count(), 1)
        self.assertEqual(ncr.capas.first().status, 'todo')

        # Verify audit event
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(ncr.id), action="COMPLIANCE_NCR_LOGGED").exists())
        # Verify notification
        self.assertTrue(Notification.objects.filter(entity_id=str(ncr.id), category="COMPLIANCE").exists())

    def test_escalate_ncr_levels(self):
        """Test advancing regulatory escalation matrix levels."""
        ncr = ComplianceService.create_ncr({"project_id": self.project.id, "title": "Safety Deviation"}, self.user)
        self.assertEqual(ncr.escalation_level, 1)

        # Escalate to level 2
        escalated = ComplianceService.escalate_ncr(ncr, self.user)
        self.assertEqual(escalated.escalation_level, 2)

        # Escalate to director level (level 4)
        escalated_dir = ComplianceService.escalate_ncr(ncr, self.user, target_level=4)
        self.assertEqual(escalated_dir.escalation_level, 4)
        self.assertEqual(escalated_dir.severity, 'Critical')

    def test_close_ncr_closes_linked_capas(self):
        """Test closing an NCR resolves all active linked CAPAs."""
        ncr = ComplianceService.create_ncr({"project_id": self.project.id, "title": "Material Test Defect"}, self.user)
        self.assertEqual(ncr.capas.first().status, 'todo')

        closed_ncr = ComplianceService.close_ncr(ncr, "Re-tested and passed.", self.user)
        self.assertEqual(closed_ncr.status, 'Closed')
        self.assertIsNotNone(closed_ncr.resolved_at)
        self.assertEqual(ncr.capas.first().status, 'closed')

    def test_capa_kanban_transitions(self):
        """Test transitioning CAPAs across Kanban board columns."""
        capa = ComplianceService.create_capa({
            "project_id": self.project.id,
            "title": "Fix dust control barrier",
            "priority": "High"
        }, self.user)

        self.assertEqual(capa.status, 'todo')
        updated = ComplianceService.transition_capa(capa, 'in-progress', 'Started repair', self.user)
        self.assertEqual(updated.status, 'in-progress')

        closed = ComplianceService.transition_capa(capa, 'closed', 'Repair complete and inspected', self.user)
        self.assertEqual(closed.status, 'closed')
        self.assertIsNotNone(closed.closed_at)

    def test_advance_review_stage(self):
        """Test advancing statutory compliance review stages and progress %."""
        review = ComplianceReview.objects.create(
            project=self.project,
            title='Annual Structural Integrity Audit',
            review_type='Building Code',
            stage='Initiation',
            progress=20
        )
        updated = ComplianceService.advance_review_stage(review, 'Audit in Progress', 'Site cores drilled and inspected', self.user)
        self.assertEqual(updated.stage, 'Audit in Progress')
        self.assertEqual(updated.progress, 50)
        self.assertEqual(updated.findings_summary, 'Site cores drilled and inspected')

    def test_escalation_rules_and_toggle(self):
        """Test retrieving and toggling escalation rules."""
        res = self.client.get('/api/v1/compliance/escalation-rules/')
        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(res.data), 0)

        rule_id = res.data[0]['id']
        toggle_res = self.client.post(f'/api/v1/compliance/escalation-rules/{rule_id}/toggle-active/')
        self.assertEqual(toggle_res.status_code, 200)
        self.assertFalse(toggle_res.data['is_active'])

    def test_issue_and_verify_certificate(self):
        """Test issuing a certificate generates tamper-proof QR hash and verification endpoint works."""
        cert = ComplianceService.issue_certificate({
            "project_id": self.project.id,
            "title": "Site Fire Safety Approval",
            "category": "Safety",
            "authority": "National Fire Dept"
        }, self.user)

        self.assertIsNotNone(cert.id)
        self.assertTrue(cert.qr_verification_hash.startswith('0x7b2a'))

        res = self.client.get(f'/api/v1/compliance/certificates/{cert.id}/verify/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['is_valid'])
        self.assertEqual(res.data['qr_verification_hash'], cert.qr_verification_hash)

    def test_compliance_overview_and_report_endpoints(self):
        """Test overview scorecard and report generation endpoints."""
        res = self.client.get('/api/v1/compliance/stats/overview/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('overall_score', res.data)
        self.assertIn('open_ncrs_count', res.data)
        self.assertIn('valid_certificates_count', res.data)

        report_res = self.client.get('/api/v1/compliance/stats/generate-report/')
        self.assertEqual(report_res.status_code, 200)
        self.assertIn('scorecard', report_res.data)
        self.assertIn('report_download_url', report_res.data)
