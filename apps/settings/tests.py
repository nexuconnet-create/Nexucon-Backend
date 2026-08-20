from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.settings.models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription
)

User = get_user_model()

class SettingsAndIntegrationsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='admin_director',
            email='director@government.gov.ng',
            password='Password123!',
            first_name='Agency',
            last_name='Director'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # ---------------- INTEGRATIONS TESTS ----------------

    def test_force_sync_tersus_device(self):
        device = TersusDevice.objects.create(
            device_id="T-S1-TEST1",
            name="Tersus Rover Test",
            status="Active"
        )
        res = self.client.post(f'/api/v1/integrations/tersus/{device.id}/force-sync/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Active')

    def test_sync_bim_platform(self):
        bim = BIMIntegration.objects.create(
            provider="Autodesk Construction Cloud Test",
            status="Connected",
            synced_models_count=10
        )
        res = self.client.post(f'/api/v1/integrations/bim/{bim.id}/sync/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['synced_models_count'], 13)

    def test_sync_document_system(self):
        dms = DocumentSystemIntegration.objects.create(
            name="Cloudflare R2 Test",
            bucket_or_drive_name="nexucondocument",
            synced_files_count=100
        )
        res = self.client.post(f'/api/v1/integrations/documents/{dms.id}/sync/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['synced_files_count'], 112)

    def test_verify_government_api(self):
        gov = GovernmentAPIIntegration.objects.create(
            api_key_identifier="cac_test",
            name="CAC Test Registry",
            endpoint_url="https://api.cac.gov.ng/test",
            status="degraded"
        )
        res = self.client.post(f'/api/v1/integrations/government/{gov.id}/test-connection/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'connected')

    def test_generate_api_key(self):
        res = self.client.post('/api/v1/integrations/api-keys/', {
            "name": "Drone Surveillance Gateway",
            "app_type": "Server-to-Server",
            "volume_tier": "Medium (50k/day)"
        })
        self.assertEqual(res.status_code, 201)
        self.assertIn('raw_key', res.data)
        self.assertTrue(res.data['raw_key'].startswith('nx_live_'))

    def test_integration_logs_filter(self):
        IntegrationLog.objects.create(
            service_name="Tersus GNSS",
            event_name="Telemetry Stream",
            status="Success"
        )
        res = self.client.get('/api/v1/integrations/logs/?service=Tersus')
        self.assertEqual(res.status_code, 200)
        results = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertTrue(any(l['service_name'] == 'Tersus GNSS' for l in results))

    def test_integration_stats(self):
        res = self.client.get('/api/v1/integrations/stats/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_requests_24h', res.data)

    # ---------------- SETTINGS TESTS ----------------

    def test_staff_user_list_and_invite(self):
        # List staff
        res = self.client.get('/api/v1/settings/users/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(len(res.data) >= 1)

        # Invite new staff
        invite_res = self.client.post('/api/v1/settings/users/', {
            "name": "Engr. Folake Balogun",
            "email": "folake.b@agency.gov.ng",
            "role": "Lead Inspector",
            "department": "Structural Engineering"
        })
        self.assertEqual(invite_res.status_code, 201)
        self.assertEqual(invite_res.data['email'], "folake.b@agency.gov.ng")

    def test_staff_user_toggle_status(self):
        staff = User.objects.create_user(
            username='officer_tunde',
            email='tunde@agency.gov.ng',
            password='Password123!',
            is_active=True
        )
        res = self.client.post(f'/api/v1/settings/users/{staff.id}/toggle-status/')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['is_active'])

    def test_custom_role_create_and_matrix_update(self):
        # Create role
        create_res = self.client.post('/api/v1/settings/roles/', {
            "name": "Geotechnical Reviewer",
            "description": "Reviews soil boring tests and foundation permits"
        })
        self.assertEqual(create_res.status_code, 201)

        # Matrix get
        mat_res = self.client.get('/api/v1/settings/roles/matrix/')
        self.assertEqual(mat_res.status_code, 200)
        self.assertIn('permission_modules', mat_res.data)

        # Matrix batch update
        up_res = self.client.post('/api/v1/settings/roles/matrix/', {
            "updates": [
                {
                    "role_name": "City Planner",
                    "module": "Permits & Approvals",
                    "permission_name": "Approve/Reject Permits",
                    "is_granted": True
                }
            ]
        }, format='json')
        self.assertEqual(up_res.status_code, 200)
        self.assertEqual(up_res.data['status'], 'success')

    def test_approval_workflow_create(self):
        res = self.client.post('/api/v1/settings/workflows/', {
            "name": "Drainage Clearance Workflow",
            "description": "Review chain for coastal drainage permits",
            "steps": [
                {"title": "Hydraulic Survey", "role": "Drainage Engineer", "icon": "HardHat"},
                {"title": "Director Authorization", "role": "Director", "icon": "CheckCircle2"}
            ]
        }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['name'], "Drainage Clearance Workflow")
        self.assertEqual(len(res.data['steps']), 2)

    def test_inspection_template_create_and_add_item(self):
        res = self.client.post('/api/v1/settings/templates/', {
            "name": "Scaffolding Safety Checklist",
            "department": "Safety",
            "items": [
                {"title": "Are base jacks level on firm foundation?", "field_type": "Pass/Fail Toggle", "is_required": True}
            ]
        }, format='json')
        self.assertEqual(res.status_code, 201)
        tpl_id = res.data['id']

        # Add item
        item_res = self.client.post(f'/api/v1/settings/templates/{tpl_id}/items/', {
            "title": "Upload photo of guardrails and toe boards",
            "field_type": "Photo Upload",
            "is_required": False
        }, format='json')
        self.assertEqual(item_res.status_code, 201)
        self.assertEqual(item_res.data['field_type'], "Photo Upload")

    def test_compliance_standards_update_thresholds(self):
        res = self.client.post('/api/v1/settings/standards/update-thresholds/', {
            "thresholds": {
                "noise_daytime_db": 80.0,
                "max_concrete_slump_in": 5.5
            }
        }, format='json')
        self.assertEqual(res.status_code, 200)
        slump_std = next((s for s in res.data if s['key'] == 'max_concrete_slump_in'), None)
        self.assertIsNotNone(slump_std)
        self.assertEqual(slump_std['num_value'], 5.5)

    def test_statutory_document_create(self):
        res = self.client.post('/api/v1/settings/statutes/', {
            "code": "LASG-BUILD-2025",
            "name": "Lagos State Building Regulation 2025",
            "connected_features": ["High-Rise Setbacks", "Soil Reports"]
        }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['code'], "LASG-BUILD-2025")

    def test_notification_preferences_update(self):
        res = self.client.post('/api/v1/settings/notifications/update-preference/', {
            "category": "Permits & Approvals",
            "event_label": "New Permit Application",
            "channel": "email",
            "enabled": False
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['email'])

    def test_notification_routing_rule_create_and_delete(self):
        # Create
        res = self.client.post('/api/v1/settings/routing-rules/', {
            "trigger_event": "Soil Liquefaction Detected",
            "primary_recipient": "Lead Geotechnical Engineer",
            "sla_timeline": "Within 30 mins",
            "escalation_target": "Director of Civil Engineering"
        }, format='json')
        self.assertEqual(res.status_code, 201)
        rule_id = res.data['id']

        # Delete
        del_res = self.client.delete(f'/api/v1/settings/routing-rules/{rule_id}/')
        self.assertEqual(del_res.status_code, 204)

    def test_webhook_create_and_delete(self):
        res = self.client.post('/api/v1/settings/webhooks/', {
            "name": "ERP Financial Bridge",
            "target_url": "https://erp.agency.gov.ng/api/v1/permits",
            "events": ["permit.created", "permit.approved"]
        }, format='json')
        self.assertEqual(res.status_code, 201)
        hook_id = res.data['id']

        del_res = self.client.delete(f'/api/v1/settings/webhooks/{hook_id}/')
        self.assertEqual(del_res.status_code, 204)
