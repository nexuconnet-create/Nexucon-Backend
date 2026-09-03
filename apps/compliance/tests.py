from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from unittest import mock

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


class ComplianceViewsAuthTestCase(TestCase):
    """Read-only access is public; every write requires authentication."""

    def test_anonymous_can_read_but_not_write(self):
        client = APIClient()

        listing = client.get('/api/v1/compliance/ncrs/')
        self.assertEqual(listing.status_code, 200)

        created = client.post('/api/v1/compliance/ncrs/',
                              {'title': 'Anonymous NCR'}, format='json')
        self.assertEqual(created.status_code, 401)

        cert = client.post('/api/v1/compliance/certificates/',
                           {'title': 'Forged certificate'}, format='json')
        self.assertEqual(cert.status_code, 401)


class ComplianceNCRViewsTestCase(ComplianceTestCase):
    def _post_ncr(self, payload):
        return self.client.post('/api/v1/compliance/ncrs/', payload, format='json')

    def test_create_ncr_via_api_builds_linked_capa(self):
        response = self._post_ncr({
            'project': str(self.project.id),
            'title': 'Unapproved concrete pour at Level 3',
            'description': 'Slab poured without approved mix design.',
            'severity': 'Major',
            'category': 'Quality',
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'Open')
        self.assertEqual(response.data['escalation_level'], 1)
        self.assertEqual(response.data['linked_capa_ref'],
                         response.data['capas'][0]['capa_reference'])
        # The serializer-level escalation matrix text is derived from level.
        self.assertEqual(response.data['escalation_action_text'], 'Reminder Sent (Auto)')

        ncr = NonConformanceReport.objects.get(pk=response.data['id'])
        self.assertEqual(ncr.capas.count(), 1)
        self.assertEqual(ncr.capas.first().priority, 'High')

    def test_create_ncr_requires_a_valid_project(self):
        response = self._post_ncr({
            'project': '00000000-0000-0000-0000-000000000000',
            'title': 'Ghost project NCR',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('project', response.data['errors'])

    def test_create_ncr_requires_a_title(self):
        response = self._post_ncr({'project': str(self.project.id)})
        self.assertEqual(response.status_code, 400)
        self.assertIn('title', response.data['errors'])

    def test_list_filters_severity_category_status_and_search(self):
        ComplianceService.create_ncr({
            'project_id': self.project.id,
            'title': 'Scaffold tie-off deviation',
            'severity': 'Major', 'category': 'Safety', 'status': 'Open',
        }, self.user)
        ComplianceService.create_ncr({
            'project_id': self.project.id,
            'title': 'Effluent discharge breach',
            'severity': 'Critical', 'category': 'Environmental', 'status': 'Closed',
        }, self.user)

        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'severity': 'critical'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'category': 'Safety'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'status': 'Open'}).data), 1)
        # 'all' is the explicit no-filter sentinel.
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'severity': 'all'}).data), 2)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'search': 'effluent'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/ncrs/', {'search': 'scaffold tie'}).data), 1)

    def test_list_filters_by_project(self):
        other_project = Project.objects.create(
            name='Second Site', reference_number='PRJ-2026-B', status='Active')
        ComplianceService.create_ncr(
            {'project_id': self.project.id, 'title': 'Site A NCR'}, self.user)
        ComplianceService.create_ncr(
            {'project_id': other_project.id, 'title': 'Site B NCR'}, self.user)

        response = self.client.get(
            '/api/v1/compliance/ncrs/', {'project': str(self.project.id)})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['title'], 'Site A NCR')

    def test_escalate_endpoint(self):
        ncr = ComplianceService.create_ncr(
            {'project_id': self.project.id, 'title': 'Late rectification'}, self.user)
        response = self.client.post(
            f'/api/v1/compliance/ncrs/{ncr.id}/escalate/',
            {'escalation_level': 3}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['escalation_level'], 3)
        self.assertEqual(response.data['escalation_action_text'],
                         'Escalate to Sr. Officer')
        ncr.refresh_from_db()
        self.assertIsNotNone(ncr.last_escalated_at)
        self.assertTrue(AuditEvent.objects.filter(
            action='COMPLIANCE_NCR_ESCALATED', resource_id=str(ncr.id)).exists())

    def test_close_endpoint_resolves_capas(self):
        ncr = ComplianceService.create_ncr(
            {'project_id': self.project.id, 'title': 'Ready to close'}, self.user)
        response = self.client.post(
            f'/api/v1/compliance/ncrs/{ncr.id}/close/',
            {'resolution_notes': 'Rectified and re-inspected.'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'Closed')
        self.assertEqual(response.data['escalation_action_text'], 'Resolved')
        self.assertEqual(response.data['capas'][0]['status'], 'closed')


class ComplianceCAPAViewsTestCase(ComplianceTestCase):
    def test_create_capa_via_api(self):
        response = self.client.post('/api/v1/compliance/capas/', {
            'project': str(self.project.id),
            'title': 'Replace damaged silt fence',
            'priority': 'Medium',
            'assignee_name': 'E. Contractor',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'todo')
        self.assertEqual(response.data['project_name'], 'Downtown Metro Station')
        capa = CorrectiveActionPlan.objects.get(pk=response.data['id'])
        self.assertEqual(capa.priority, 'Medium')
        self.assertTrue(AuditEvent.objects.filter(
            action='COMPLIANCE_CAPA_CREATED', resource_id=str(capa.id)).exists())

    def test_create_capa_requires_valid_project(self):
        response = self.client.post('/api/v1/compliance/capas/', {
            'project': 'not-a-uuid', 'title': 'Bad project CAPA',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('project', response.data['errors'])

    def test_list_filters_priority_status_and_ncr(self):
        ncr = ComplianceService.create_ncr(
            {'project_id': self.project.id, 'title': 'Parent NCR'}, self.user)
        linked = ncr.capas.first()
        CorrectiveActionPlan.objects.create(
            project=self.project, title='Standalone action', priority='Low',
            status='in-progress')

        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/capas/', {'priority': 'low'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/capas/', {'status': 'todo'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/capas/', {'status': 'all'}).data), 2)
        by_ncr = self.client.get(
            '/api/v1/compliance/capas/', {'ncr': str(ncr.id)}).data
        self.assertEqual(len(by_ncr), 1)
        self.assertEqual(by_ncr[0]['id'], str(linked.id))

    def test_transition_requires_status(self):
        capa = ComplianceService.create_capa(
            {'project_id': self.project.id, 'title': 'Untouched'}, self.user)
        response = self.client.post(
            f'/api/v1/compliance/capas/{capa.id}/transition/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_transition_moves_capa_across_board(self):
        capa = ComplianceService.create_capa(
            {'project_id': self.project.id, 'title': 'Board mover'}, self.user)
        response = self.client.post(
            f'/api/v1/compliance/capas/{capa.id}/transition/',
            {'status': 'review', 'verification_notes': 'Awaiting QA signoff'},
            format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'review')

        capa.refresh_from_db()
        self.assertEqual(capa.verification_notes, 'Awaiting QA signoff')
        self.assertTrue(AuditEvent.objects.filter(
            action='COMPLIANCE_CAPA_TRANSITIONED', resource_id=str(capa.id)).exists())


class ComplianceRequirementsAndReviewsTestCase(ComplianceTestCase):
    def test_requirements_list_seeds_defaults_and_filters(self):
        response = self.client.get('/api/v1/compliance/requirements/')
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data), 4)

        by_category = self.client.get(
            '/api/v1/compliance/requirements/', {'category': 'Building Codes'}).data
        self.assertTrue(all(r['category'] == 'Building Codes' for r in by_category))
        self.assertGreaterEqual(len(by_category), 1)

        by_status = self.client.get(
            '/api/v1/compliance/requirements/', {'status': 'Pending Assessment'}).data
        self.assertGreaterEqual(len(by_status), 4)

        searched = self.client.get(
            '/api/v1/compliance/requirements/', {'search': 'LASBCA'}).data
        self.assertGreaterEqual(len(searched), 1)

    def test_update_status_action_records_check_date(self):
        ComplianceService.seed_default_requirements()
        requirement = RegulatoryRequirement.objects.first()
        response = self.client.post(
            f'/api/v1/compliance/requirements/{requirement.id}/update-status/',
            {'status': 'Compliant'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'Compliant')

        requirement.refresh_from_db()
        self.assertEqual(requirement.status, 'Compliant')
        self.assertEqual(requirement.last_checked, timezone.localdate())

    def test_reviews_list_filters(self):
        ComplianceReview.objects.create(
            project=self.project, title='Fire safety audit',
            review_type='Safety', stage='Initiation')
        ComplianceReview.objects.create(
            project=self.project, title='Structural code verification',
            review_type='Building Code', stage='Reporting')

        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/reviews/', {'stage': 'reporting'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/reviews/', {'type': 'safety'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/reviews/', {'search': 'structural'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/reviews/',
            {'project': str(self.project.id)}).data), 2)

    def test_advance_stage_requires_stage(self):
        review = ComplianceReview.objects.create(
            project=self.project, title='Quarterly audit')
        response = self.client.post(
            f'/api/v1/compliance/reviews/{review.id}/advance-stage/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_advance_stage_updates_progress(self):
        review = ComplianceReview.objects.create(
            project=self.project, title='Quarterly audit', stage='Initiation')
        response = self.client.post(
            f'/api/v1/compliance/reviews/{review.id}/advance-stage/',
            {'stage': 'Completed', 'findings_summary': 'All clauses satisfied.'},
            format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['stage'], 'Completed')
        self.assertEqual(response.data['progress'], 100)
        review.refresh_from_db()
        self.assertEqual(review.findings_summary, 'All clauses satisfied.')


class ComplianceEscalationAndCertificateViewsTestCase(ComplianceTestCase):
    def test_escalation_rules_category_filter(self):
        ComplianceService.seed_default_escalation_rules()
        response = self.client.get(
            '/api/v1/compliance/escalation-rules/', {'category': 'safety'})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data), 1)
        self.assertTrue(all(r['trigger_category'] == 'Safety' for r in response.data))

    def test_certificate_create_via_api(self):
        response = self.client.post('/api/v1/compliance/certificates/', {
            'project': str(self.project.id),
            'title': 'Environmental clearance',
            'category': 'Environmental',
            'authority': 'LASEPA',
            'expiry_date': '2028-09-03',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['qr_verification_hash'].startswith('0x7b2a'))
        self.assertEqual(response.data['status'], 'Active')
        self.assertTrue(AuditEvent.objects.filter(
            action='COMPLIANCE_CERTIFICATE_ISSUED').exists())

    def test_certificate_requires_valid_project(self):
        response = self.client.post('/api/v1/compliance/certificates/', {
            'project': 'not-a-uuid', 'title': 'Bad certificate',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('project', response.data['errors'])

    def test_certificate_list_filters(self):
        ComplianceService.issue_certificate({
            'project_id': self.project.id, 'title': 'Fire safety',
            'category': 'Safety', 'authority': 'Fire Service',
        }, self.user)
        expiring = ComplianceService.issue_certificate({
            'project_id': self.project.id, 'title': 'Structural fitness',
            'category': 'Building Code', 'authority': 'LASBCA',
        }, self.user)
        expiring.status = 'Expired'
        expiring.save()

        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/certificates/', {'status': 'active'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/certificates/', {'category': 'Safety'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/certificates/', {'search': 'structural fitness'}).data), 1)
        self.assertEqual(len(self.client.get(
            '/api/v1/compliance/certificates/', {'status': 'all'}).data), 2)

    def test_verify_reports_invalid_for_non_active_certificates(self):
        revoked = ComplianceService.issue_certificate({
            'project_id': self.project.id, 'title': 'Revoked clearance',
        }, self.user)
        revoked.status = 'Revoked'
        revoked.save()

        response = self.client.get(
            f'/api/v1/compliance/certificates/{revoked.id}/verify/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['is_valid'])
        self.assertEqual(response.data['certificate_reference'],
                         revoked.certificate_reference)


class ComplianceServiceBackstopsTestCase(ComplianceTestCase):
    """Service-level validation backstops and resilience paths."""

    def test_create_ncr_rejects_bogus_project(self):
        with self.assertRaises(ValidationError):
            ComplianceService.create_ncr(
                {'project_id': '00000000-0000-0000-0000-000000000000',
                 'title': 'No project'}, self.user)

    def test_create_capa_rejects_bogus_project(self):
        with self.assertRaises(ValidationError):
            ComplianceService.create_capa(
                {'project_id': '00000000-0000-0000-0000-000000000000',
                 'title': 'No project'}, self.user)

    def test_issue_certificate_rejects_bogus_project(self):
        with self.assertRaises(ValidationError):
            ComplianceService.issue_certificate(
                {'project_id': '00000000-0000-0000-0000-000000000000',
                 'title': 'No project'}, self.user)

    def test_create_ncr_survives_audit_and_notification_failures(self):
        # Auditing and notification must never break the compliance record.
        with mock.patch.object(AuditEvent.objects, 'create',
                               side_effect=RuntimeError('audit down')), \
             mock.patch.object(Notification.objects, 'create',
                               side_effect=RuntimeError('notifications down')):
            ncr = ComplianceService.create_ncr(
                {'project_id': self.project.id, 'title': 'Resilient NCR'},
                self.user)
        self.assertIsNotNone(ncr.id)
        self.assertEqual(NonConformanceReport.objects.count(), 1)

    def test_issue_certificate_with_upload_uses_real_storage_service(self):
        uploaded = mock.Mock(name='certificate.pdf')
        with mock.patch(
                'apps.documents.services.DocumentStorageService.upload_file_to_r2',
                return_value={'file_url': 'https://r2.example/certs/cert.pdf',
                              'signature_hash': '0x7b2asignaturee41'}) as upload:
            cert = ComplianceService.issue_certificate(
                {'project_id': self.project.id,
                 'title': 'Uploaded certificate'}, self.user,
                file_obj=uploaded)

        upload.assert_called_once()
        self.assertEqual(cert.certificate_file_url,
                         'https://r2.example/certs/cert.pdf')
        self.assertEqual(cert.qr_verification_hash, '0x7b2asignaturee41')

    def test_overview_stats_without_assessed_requirements_has_no_score(self):
        # Fresh seed: 0 of 4 requirements compliant -> an honest 0%, and no
        # invented NCR/CAPA/certificate counts.
        stats = ComplianceService.get_overview_stats()
        self.assertEqual(stats['overall_score'], '0%')
        self.assertEqual(stats['open_ncrs_count'], 0)
        self.assertEqual(stats['pending_capas_count'], 0)
        self.assertEqual(stats['valid_certificates_count'], 0)

    def test_overview_stats_counts_real_records_only(self):
        ComplianceService.seed_default_requirements()
        ncr = ComplianceService.create_ncr(
            {'project_id': self.project.id, 'title': 'Counted NCR',
             'severity': 'Critical'}, self.user)
        ComplianceService.issue_certificate(
            {'project_id': self.project.id, 'title': 'Counted cert'}, self.user)

        stats = ComplianceService.get_overview_stats()
        self.assertEqual(stats['open_ncrs_count'], 1)
        self.assertEqual(stats['critical_ncrs_count'], 1)
        self.assertEqual(stats['pending_capas_count'], 1)  # NCR auto-CAPA
        self.assertEqual(stats['valid_certificates_count'], 1)

        # Closing the NCR moves the counters.
        ComplianceService.close_ncr(ncr, 'done', self.user)
        stats = ComplianceService.get_overview_stats()
        self.assertEqual(stats['open_ncrs_count'], 0)
        self.assertEqual(stats['pending_capas_count'], 0)
