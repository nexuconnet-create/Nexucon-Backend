from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from decimal import Decimal
from apps.projects.models import Project
from apps.approvals.models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria, ApprovalComment
from apps.approvals.services import ApprovalService

User = get_user_model()

class ApprovalsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='approver_director',
            email='director@government.gov.ng',
            password='Password123!',
            first_name='Sarah',
            last_name='Jenkins'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Riverside Commercial Complex',
            reference_number='PRJ-2026-RIVER',
            lga='Victoria Island',
            status='Active'
        )

    def test_create_request_doa_threshold_calculation(self):
        """Test Delegation of Authority threshold based on financial amount (>₦50M vs ≤₦50M)."""
        # Low value <= ₦50M
        req_low = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Night Shift Work Permit",
            "request_type": "Permit",
            "value_amount": 5000000.0,
            "source_entity_type": "PermitApplication",
            "source_entity_id": "PERMIT-1234"
        }, self.user)
        self.assertEqual(req_low.doa_level_required, 'Director')
        self.assertIsNotNone(req_low.source_version_hash)

        # High value > ₦50M
        req_high = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Phase 2 Environmental Impact Addendum",
            "request_type": "Document",
            "value_amount": 120000000.0
        }, self.user)
        self.assertEqual(req_high.doa_level_required, 'Permanent Secretary / Director General')

    def test_approve_request_generates_sha256_seal(self):
        """Test approving request generates cryptographic seal and creates decision record."""
        req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Structural Steel Shop Drawings (Z3)",
            "request_type": "Technical"
        }, self.user)

        res = self.client.post(f'/api/v1/approvals/requests/{req.id}/approve/', {
            "notes": "Reviewed connection calculations and verified compliance.",
            "pin": "1234"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['request']['status'], 'Approved')
        self.assertTrue(res.data['decision']['signature_hash'].startswith('0x8f2c'))

    def test_conditional_approval(self):
        """Test issuing conditional approval when conditions are specified."""
        req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Riverside Commercial Complex Permit",
            "request_type": "Permit"
        }, self.user)

        res = self.client.post(f'/api/v1/approvals/requests/{req.id}/approve/', {
            "conditions": "Submit revised load-bearing calculations for Floor 3 before construction."
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['request']['status'], 'Conditional')

    def test_reject_request(self):
        """Test rejecting request with mandatory justification."""
        req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Crane Erection Permit",
            "request_type": "Permit"
        }, self.user)

        res = self.client.post(f'/api/v1/approvals/requests/{req.id}/reject/', {
            "reason": "Denied due to forecasted high wind warnings. Resubmit after weather clears."
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['request']['status'], 'Rejected')

    def test_reviewer_assignment_and_revision_request(self):
        """Test assigning reviewer and requesting formal revisions."""
        req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Architectural Elevation Plan V2",
            "request_type": "Document"
        }, self.user)

        # Assign Reviewer
        res_assign = self.client.post(f'/api/v1/approvals/requests/{req.id}/assign/', {
            "reviewer_name": "Engr. David Adeleke"
        })
        self.assertEqual(res_assign.status_code, 200)
        self.assertEqual(res_assign.data['assigned_to_name'], "Engr. David Adeleke")

        # Request Revision
        res_rev = self.client.post(f'/api/v1/approvals/requests/{req.id}/request-revision/', {
            "revision_notes": "Setback distance does not comply with 3-meter minimum requirement."
        })
        self.assertEqual(res_rev.status_code, 200)
        self.assertEqual(res_rev.data['request']['status'], 'Awaiting Fix')

    def test_compliance_gate_check(self):
        """Test compliance gating logic against project NCRs."""
        req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Foundation Pouring Authorization",
            "request_type": "Permit"
        }, self.user)

        res = self.client.get(f'/api/v1/approvals/requests/{req.id}/compliance-gate/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['gate_passed'])

    def test_document_multi_signing(self):
        """Test multi-signatory document workflow."""
        doc_req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Master Subcontractor Agreement v3",
            "request_type": "Document",
            "signatories_required": 2,
            "signatories_completed": 0
        }, self.user)

        # Sign 1
        res1 = self.client.post(f'/api/v1/approvals/requests/{doc_req.id}/sign/')
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.data['signatories_completed'], 1)
        self.assertEqual(res1.data['status'], 'Pending')

        # Sign 2
        res2 = self.client.post(f'/api/v1/approvals/requests/{doc_req.id}/sign/')
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.data['signatories_completed'], 2)
        self.assertEqual(res2.data['status'], 'Approved')
