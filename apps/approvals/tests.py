from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from decimal import Decimal
from apps.projects.models import Project
from apps.approvals.models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria
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
            "value_amount": 5000000.0
        }, self.user)
        self.assertEqual(req_low.doa_level_required, 'Director')

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

    def test_document_multi_signing(self):
        """Test multi-signatory document workflow."""
        doc_req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "Master Subcontractor Agreement v3",
            "request_type": "Document",
            "signatories_required": 2,
            "signatories_completed": 0
        }, self.user)

        # First signature
        res1 = self.client.post(f'/api/v1/approvals/requests/{doc_req.id}/sign/')
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.data['signatories_completed'], 1)
        self.assertEqual(res1.data['status'], 'Pending')

        # Second signature
        res2 = self.client.post(f'/api/v1/approvals/requests/{doc_req.id}/sign/')
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.data['signatories_completed'], 2)
        self.assertEqual(res2.data['status'], 'Approved')

    def test_evaluate_technical_criterion(self):
        """Test evaluating a technical review criterion."""
        tech_req = ApprovalService.create_request({
            "project_id": self.project.id,
            "title": "HVAC Zone 4 Load Calculations",
            "request_type": "Technical"
        }, self.user)

        criterion = tech_req.criteria.first()
        self.assertIsNotNone(criterion)

        res = self.client.post(f'/api/v1/approvals/criteria/{criterion.id}/evaluate/', {
            "status": "pass",
            "notes": "Exceeds minimum requirements by 12%."
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'pass')
