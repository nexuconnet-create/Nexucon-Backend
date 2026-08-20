from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService

User = get_user_model()

class AuditTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='auditor',
            email='audit@government.gov.ng',
            password='Password123!',
            first_name='Internal',
            last_name='Auditor'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_append_only_immutability(self):
        """Test that updating or deleting an existing AuditEvent raises PermissionDenied."""
        event = AuditService.log_event(
            action="TEST_ACTION",
            resource_type="TestResource",
            resource_id="123",
            user=self.user
        )

        with self.assertRaises(PermissionDenied):
            event.action = "MUTATED_ACTION"
            event.save()

        with self.assertRaises(PermissionDenied):
            event.delete()

    def test_log_event_with_hash_seal(self):
        """Test generating audit event with cryptographic signature hash."""
        event = AuditService.log_event(
            action="PERMIT_REVOKED",
            resource_type="Permit",
            resource_id="PRM-001",
            user=self.user,
            previous_state={"status": "Active"},
            new_state={"status": "Revoked"}
        )

        self.assertIsNotNone(event.id)
        self.assertTrue(event.signature_hash.startswith("0x"))
        self.assertEqual(event.severity, "Normal")
        self.assertTrue(event.is_verified)

    def test_verify_hash_chain(self):
        """Test executing tamper-proof hash chain verification."""
        AuditService.log_event("ACTION_1", "Res1", "1", user=self.user)
        AuditService.log_event("ACTION_2", "Res2", "2", user=self.user)

        res = self.client.post('/api/v1/audit/events/verify-chain/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'VALID')
        self.assertEqual(res.data['chain_integrity'], '100.0% VERIFIED')
        self.assertGreaterEqual(res.data['total_blocks_checked'], 2)

    def test_compute_diff(self):
        """Test calculating before vs after JSON state diff."""
        event = AuditService.log_event(
            action="STATUS_CHANGED",
            resource_type="Compliance",
            resource_id="NCR-1",
            user=self.user,
            previous_state={"status": "Open", "remedy_assigned": False},
            new_state={"status": "Under Remediation", "remedy_assigned": True}
        )

        res = self.client.get(f'/api/v1/audit/events/{event.id}/diff/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['changes_count'], 2)
        fields_changed = [c['field'] for c in res.data['changes']]
        self.assertIn('status', fields_changed)
        self.assertIn('remedy_assigned', fields_changed)

    def test_audit_summary_endpoint(self):
        """Test retrieving audit summary and security counters."""
        res = self.client.get('/api/v1/audit/events/summary/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_records', res.data)
        self.assertIn('chain_status', res.data)
        self.assertIn('active_sessions', res.data)
