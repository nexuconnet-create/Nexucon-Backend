import datetime
import os
import urllib.error
from contextlib import contextmanager
from unittest import mock

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient
from apps.audit.models import AuditEvent
from apps.settings.models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription, AgencyProfile
)
from apps.settings.services import (
    GovernmentAPIProvider, TersusProvider, BIMProvider, DocumentProvider,
    BaseIntegrationProvider, IntegrationService, SettingsService, _http_probe,
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
        with env_without('TERSUS_API_URL', 'TERSUS_API_KEY'):
            res = self.client.post(f'/api/v1/integrations/tersus/{device.id}/force-sync/')
        self.assertEqual(res.status_code, 200)
        # No credentials → the sync is skipped (logged Pending) and the
        # device row is returned unchanged; no telemetry is fabricated.
        self.assertEqual(res.data['status'], 'Active')
        self.assertTrue(
            IntegrationLog.objects.filter(service_name='Tersus GNSS', status='Pending').exists())

    def test_sync_bim_platform(self):
        bim = BIMIntegration.objects.create(
            provider="Autodesk Construction Cloud Test",
            status="Connected",
            synced_models_count=10
        )
        with env_without('TRIMBLE_CLIENT_ID', 'TRIMBLE_CLIENT_SECRET', 'TRIMBLE_API_BASE'):
            res = self.client.post(f'/api/v1/integrations/bim/{bim.id}/sync/')
        self.assertEqual(res.status_code, 200)
        # Autodesk credentials are not wired (and Trimble vars are unset) — no
        # sync runs, so the model count must be unchanged, never incremented.
        self.assertEqual(res.data['synced_models_count'], 10)
        bim.refresh_from_db()
        self.assertEqual(bim.synced_models_count, 10)
        # The skipped attempt is logged honestly as Pending.
        self.assertTrue(
            IntegrationLog.objects.filter(service_name=bim.provider, status='Pending').exists())

    def test_sync_document_system(self):
        dms = DocumentSystemIntegration.objects.create(
            name="Cloudflare R2 Test",
            bucket_or_drive_name="nexucondocument",
            synced_files_count=100
        )
        with env_without(
            'CLOUDFLARE_R2_ACCESS_KEY_ID', 'CLOUDFLARE_R2_SECRET_ACCESS_KEY',
            'CLOUDFLARE_R2_ENDPOINT_URL', 'CLOUDFLARE_R2_BUCKET_NAME',
        ):
            res = self.client.post(f'/api/v1/integrations/documents/{dms.id}/sync/')
        self.assertEqual(res.status_code, 200)
        # Without R2 credentials no files are scanned or counted — the stored
        # count is untouched, never incremented by a fabricated delta.
        self.assertEqual(res.data['synced_files_count'], 100)
        dms.refresh_from_db()
        self.assertEqual(dms.synced_files_count, 100)

    def test_verify_government_api(self):
        gov = GovernmentAPIIntegration.objects.create(
            api_key_identifier="cac_test",
            name="CAC Test Registry",
            endpoint_url="https://api.cac.gov.ng/test",
            status="connected"
        )
        with env_without('GOV_CAC_API_TOKEN'):
            # test-connection returns the serialized record; a previously
            # 'connected' row must be flipped to pending_credentials, never
            # left claiming a connection that cannot be verified.
            res = self.client.post(f'/api/v1/integrations/government/{gov.id}/test-connection/')
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data['status'], 'pending_credentials')
            gov.refresh_from_db()
            self.assertEqual(gov.status, 'pending_credentials')

            # The health action reports the provider-level verdict.
            health = self.client.post(f'/api/v1/integrations/government/{gov.id}/health/')
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.data['status'], 'PENDING_CREDENTIALS')
            self.assertIsNone(health.data['response_time_ms'])

    def test_generate_api_key(self):
        res = self.client.post('/api/v1/integrations/api-keys/', {
            "name": "Drone Surveillance Gateway",
            "app_type": "Server-to-Server",
            "volume_tier": "Medium (50k/day)"
        })
        self.assertEqual(res.status_code, 201)
        self.assertIn('raw_key', res.data)
        self.assertTrue(res.data['raw_key'].startswith('nx_live_'))

    def test_integration_health_checks(self):
        device = TersusDevice.objects.create(
            device_id="T-S1-HEALTH",
            name="Tersus Health Unit",
            status="Offline"
        )
        with env_without('TERSUS_API_URL', 'TERSUS_API_KEY'):
            res = self.client.get(f'/api/v1/integrations/tersus/{device.id}/health/')
        self.assertEqual(res.status_code, 200)
        # Without Tersus credentials no probe is made: the health check
        # reports PENDING_CREDENTIALS instead of a fabricated HEALTHY status,
        # and the device is not marked Active.
        self.assertEqual(res.data['status'], 'PENDING_CREDENTIALS')
        self.assertEqual(res.data['provider'], 'Tersus GNSS')
        self.assertIsNone(res.data['response_time_ms'])
        device.refresh_from_db()
        self.assertEqual(device.status, 'Offline')
        self.assertTrue(
            IntegrationLog.objects.filter(service_name='Tersus GNSS', status='Pending').exists())

    def test_api_key_rotate_and_revoke(self):
        cred_res = self.client.post('/api/v1/integrations/api-keys/', {
            "name": "Rotating Key Test",
            "app_type": "OAuth 2.0 App",
            "volume_tier": "Standard"
        })
        key_id = cred_res.data['id']

        # Rotate
        rot_res = self.client.post(f'/api/v1/integrations/api-keys/{key_id}/rotate/')
        self.assertEqual(rot_res.status_code, 200)
        self.assertIn('raw_key', rot_res.data)

        # Revoke
        rev_res = self.client.post(f'/api/v1/integrations/api-keys/{key_id}/revoke/')
        self.assertEqual(rev_res.status_code, 200)
        self.assertEqual(rev_res.data['status'], 'Revoked')

    def test_verify_government_entity_lookup(self):
        with env_without('GOV_CAC_API_TOKEN'):
            res = self.client.post('/api/v1/integrations/government/verify-entity/', {
                "provider_code": "CAC",
                "query_identifier": "RC-1849204"
            })
        self.assertEqual(res.status_code, 200)
        # Without the agency's API token the lookup cannot run — verified must
        # be False and the missing credential named, never a fabricated
        # "ACTIVE & COMPLIANT" registry response.
        self.assertFalse(res.data['verified'])
        self.assertEqual(res.data['status'], 'PENDING_CREDENTIALS')
        self.assertIn('GOV_CAC_API_TOKEN', res.data['reason'])

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

    def test_get_and_update_agency_profile(self):
        # Get profile
        res = self.client.get('/api/v1/settings/profile/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['agency_code'], 'LASG-MPPUD-01')

        # Update profile
        update_res = self.client.post('/api/v1/settings/profile/', {
            "agency_name": "Lagos State Ministry of Physical Planning (Updated)",
            "official_email": "info@mppud.lagosstate.gov.ng"
        }, format='json')
        self.assertEqual(update_res.status_code, 200)
        self.assertEqual(update_res.data['agency_name'], "Lagos State Ministry of Physical Planning (Updated)")
        self.assertEqual(update_res.data['official_email'], "info@mppud.lagosstate.gov.ng")

    def test_report_templates_list_and_set_default(self):
        # List templates
        res = self.client.get('/api/v1/settings/report-templates/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(len(res.data) >= 4)

        # Set default
        set_res = self.client.post('/api/v1/settings/report-templates/RPT-STAT-02/set-default/')
        self.assertEqual(set_res.status_code, 200)
        self.assertTrue(set_res.data['is_active_default'])

        # Get active
        active_res = self.client.get('/api/v1/settings/report-templates/active/')
        self.assertEqual(active_res.status_code, 200)
        self.assertEqual(active_res.data['id'], 'RPT-STAT-02')


@contextmanager
def env_without(*names):
    """Temporarily remove the given environment variables (restored on exit)."""
    with mock.patch.dict(os.environ):
        for name in names:
            os.environ.pop(name, None)
        yield


class HonestIntegrationProviderTestCase(TestCase):
    """
    Integration providers must report PENDING_CREDENTIALS — never a
    fabricated success — when the real integration credentials are not
    configured in the environment (apps/settings/services.py).
    """

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='honesty_admin',
            email='honesty.director@government.gov.ng',
            password='Password123!',
            first_name='Honest',
            last_name='Director'
        )

    def test_government_verify_entity_without_token_is_pending(self):
        GovernmentAPIIntegration.objects.create(
            api_key_identifier="cac_pending",
            name="CAC Registry (Pending)",
            provider_code="CAC",
            endpoint_url="https://api.cac.gov.ng/test"
        )
        with env_without('GOV_CAC_API_TOKEN'):
            result = GovernmentAPIProvider.verify_entity('CAC', 'RC-1849204', user=self.user)

        self.assertEqual(result['status'], 'PENDING_CREDENTIALS')
        self.assertFalse(result['verified'])
        self.assertIn('GOV_CAC_API_TOKEN', result['reason'])

        # Honest logging: a Pending event recording that no call was made —
        # never a fabricated Success log.
        pending = IntegrationLog.objects.get(
            event_name__contains='RC-1849204', status='Pending')
        self.assertIn('No network call was made', pending.details)
        self.assertFalse(IntegrationLog.objects.filter(
            event_name__contains='RC-1849204', status='Success').exists())

    def test_government_test_connection_without_token_is_pending(self):
        gov = GovernmentAPIIntegration.objects.create(
            api_key_identifier="cac_pending_conn",
            name="CAC Registry (Pending Connection)",
            provider_code="CAC",
            endpoint_url="https://api.cac.gov.ng/test",
            status="connected"
        )
        with env_without('GOV_CAC_API_TOKEN'), \
                mock.patch('apps.settings.services._http_probe') as probe:
            result = GovernmentAPIProvider.test_connection(gov.id, user=self.user)

        probe.assert_not_called()
        self.assertEqual(result['status'], 'PENDING_CREDENTIALS')
        self.assertIsNone(result['response_time_ms'])

        # The integration is NOT reported or persisted as connected.
        gov.refresh_from_db()
        self.assertNotEqual(gov.status, 'connected')
        self.assertEqual(gov.status, 'pending_credentials')

        self.assertTrue(IntegrationLog.objects.filter(
            service_name='CAC Registry (Pending Connection)', status='Pending').exists())
        self.assertFalse(IntegrationLog.objects.filter(
            service_name='CAC Registry (Pending Connection)', status='Success').exists())

    def test_tersus_test_connection_without_credentials_is_pending(self):
        device = TersusDevice.objects.create(
            device_id="T-NOAUTH-01",
            name="Tersus No-Creds Rover",
            status="Offline"
        )
        with env_without('TERSUS_API_URL', 'TERSUS_API_KEY'), \
                mock.patch('apps.settings.services._http_probe') as probe:
            result = TersusProvider.test_connection(device.device_id, user=self.user)

        probe.assert_not_called()
        self.assertEqual(result['status'], 'PENDING_CREDENTIALS')
        self.assertIsNone(result['response_time_ms'])

        # The device is never marked Active without a real probe.
        device.refresh_from_db()
        self.assertEqual(device.status, 'Offline')

        pending = IntegrationLog.objects.get(
            service_name='Tersus GNSS',
            event_name__contains='Tersus No-Creds Rover',
            status='Pending')
        self.assertIn('No network call was made', pending.details)
        self.assertFalse(IntegrationLog.objects.filter(
            service_name='Tersus GNSS',
            event_name__contains='Tersus No-Creds Rover',
            status='Success').exists())

    def test_document_probe_without_r2_credentials_is_pending(self):
        dms = DocumentSystemIntegration.objects.create(
            name="Cloudflare R2 Honest Store",
            storage_provider="Cloudflare R2",
            bucket_or_drive_name="nexucon-docs",
            endpoint_url="",
            status="Disconnected"
        )
        r2_vars = ('CLOUDFLARE_R2_ACCESS_KEY_ID', 'CLOUDFLARE_R2_SECRET_ACCESS_KEY',
                   'CLOUDFLARE_R2_ENDPOINT_URL', 'CLOUDFLARE_R2_BUCKET_NAME')
        with env_without(*r2_vars):
            # Low-level probe: pending, no latency, explicit "no call made".
            state, latency_ms, detail = DocumentProvider._probe_storage(dms)
            self.assertEqual(state, 'PENDING_CREDENTIALS')
            self.assertIsNone(latency_ms)
            self.assertIn('No network call was made', detail)

            result = DocumentProvider.test_connection(dms.id, user=self.user)

        self.assertEqual(result['status'], 'PENDING_CREDENTIALS')
        self.assertIsNone(result['response_time_ms'])

        # The DMS is never marked Active without a real probe.
        dms.refresh_from_db()
        self.assertEqual(dms.status, 'Disconnected')

        self.assertTrue(IntegrationLog.objects.filter(
            service_name='Cloudflare R2 Honest Store', status='Pending').exists())
        self.assertFalse(IntegrationLog.objects.filter(
            service_name='Cloudflare R2 Honest Store', status='Success').exists())

    def test_bim_test_connection_without_trimble_credentials_is_pending(self):
        bim = BIMIntegration.objects.create(
            provider="Trimble Connect (Honest)",
            status="Disconnected",
            synced_models_count=3
        )
        with env_without('TRIMBLE_CLIENT_ID', 'TRIMBLE_CLIENT_SECRET', 'TRIMBLE_API_BASE'), \
                mock.patch('apps.settings.services._http_probe') as probe:
            result = BIMProvider.test_connection(bim.id, user=self.user)

        probe.assert_not_called()
        self.assertEqual(result['status'], 'PENDING_CREDENTIALS')
        self.assertIsNone(result['response_time_ms'])

        # The integration is never set to 'Connected' without a real probe.
        bim.refresh_from_db()
        self.assertEqual(bim.status, 'Disconnected')

        self.assertTrue(IntegrationLog.objects.filter(
            service_name='Trimble Connect (Honest)', status='Pending').exists())
        self.assertFalse(IntegrationLog.objects.filter(
            service_name='Trimble Connect (Honest)', status='Success').exists())


def env_with(**kwargs):
    """Temporarily set the given environment variables (restored on exit)."""
    return mock.patch.dict(os.environ, kwargs)


class HTTPProbeTestCase(TestCase):
    """_http_probe reports exactly what the (mocked) network did."""

    def test_successful_probe_reports_status_and_latency(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with mock.patch('urllib.request.urlopen', return_value=response):
            ok, http_status, latency_ms, detail = _http_probe(
                'https://api.example.test/health')
        self.assertTrue(ok)
        self.assertEqual(http_status, 200)
        self.assertGreaterEqual(latency_ms, 0)
        self.assertEqual(detail, '')

    def test_http_error_reports_the_real_status_code(self):
        error = urllib.error.HTTPError(
            'https://api.example.test/health', 404, 'Not Found', None, None)
        with mock.patch('urllib.request.urlopen', side_effect=error):
            ok, http_status, latency_ms, detail = _http_probe(
                'https://api.example.test/health')
        self.assertFalse(ok)
        self.assertEqual(http_status, 404)
        self.assertEqual(detail, 'HTTP 404')

    def test_network_failure_reports_the_error(self):
        with mock.patch('urllib.request.urlopen',
                        side_effect=OSError('dns lookup failed')):
            ok, http_status, latency_ms, detail = _http_probe(
                'https://api.example.test/health')
        self.assertFalse(ok)
        self.assertIsNone(http_status)
        self.assertIn('dns lookup failed', detail)


class ProviderContractTestCase(TestCase):
    """The base provider interface must not silently do nothing."""

    def test_base_provider_methods_raise_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            BaseIntegrationProvider.test_connection('x')
        with self.assertRaises(NotImplementedError):
            BaseIntegrationProvider.health_check('x')
        with self.assertRaises(NotImplementedError):
            BaseIntegrationProvider.sync('x')


class TersusProviderProbeTestCase(TestCase):
    """Tersus health/sync with credentials configured (probe mocked)."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='tersus_probe_admin', email='tersus.probe@government.gov.ng',
            password='Password123!')
        self.device = TersusDevice.objects.create(
            device_id='T-PROBE-01', name='Tersus Probe Rover',
            status='Offline', satellites_tracked=14, rtk_fix_status='Fixed')

    def test_connection_healthy_marks_device_active_from_real_probe(self):
        env = env_with(TERSUS_API_URL='https://api.tersus.example.test',
                       TERSUS_API_KEY='real-key')
        with env, mock.patch('apps.settings.services._http_probe',
                             return_value=(True, 200, 42, '')) as probe:
            result = TersusProvider.test_connection(self.device.device_id, user=self.user)

        probe.assert_called_once()
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['response_time_ms'], 42)
        self.assertEqual(result['satellites'], 14)
        self.assertEqual(result['fix_status'], 'Fixed')

        self.device.refresh_from_db()
        self.assertEqual(self.device.status, 'Active')
        self.assertIsNotNone(self.device.last_sync)
        self.assertTrue(AuditEvent.objects.filter(
            action='INTEGRATION_TESTED',
            resource_id=str(self.device.id)).exists())
        log = IntegrationLog.objects.get(
            event_name__contains='Tersus Probe Rover', status='Success')
        self.assertEqual(log.http_status_code, 200)
        self.assertEqual(log.duration_ms, 42)

    def test_connection_unreachable_never_marks_device_active(self):
        env = env_with(TERSUS_API_URL='https://api.tersus.example.test',
                       TERSUS_API_KEY='real-key')
        with env, mock.patch('apps.settings.services._http_probe',
                             return_value=(False, None, 800, 'dns failure')):
            result = TersusProvider.test_connection(self.device.device_id, user=self.user)

        self.assertEqual(result['status'], 'UNREACHABLE')
        self.assertEqual(result['response_time_ms'], 800)
        self.device.refresh_from_db()
        self.assertEqual(self.device.status, 'Offline')
        self.assertTrue(IntegrationLog.objects.filter(
            event_name__contains='Tersus Probe Rover', status='Failed').exists())

    def test_sync_updates_last_sync_only_on_successful_probe(self):
        env = env_with(TERSUS_API_URL='https://api.tersus.example.test',
                       TERSUS_API_KEY='real-key')
        with env, mock.patch('apps.settings.services._http_probe',
                             return_value=(True, 200, 30, '')):
            device = TersusProvider.sync(self.device.device_id, user=self.user)
        self.assertIsNotNone(device.last_sync)

        with env, mock.patch('apps.settings.services._http_probe',
                             return_value=(False, 500, 60, 'HTTP 500')):
            result = TersusProvider.sync(self.device.device_id, user=self.user)
        self.assertTrue(IntegrationLog.objects.filter(
            event_name__contains='sync for Tersus Probe Rover',
            status='Failed').exists())


class BIMProviderProbeTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='bim_probe_admin', email='bim.probe@government.gov.ng',
            password='Password123!')
        self.bim = BIMIntegration.objects.create(
            provider='Trimble Connect (Probe)', status='Disconnected',
            synced_models_count=5)

    def _env(self):
        return env_with(TRIMBLE_CLIENT_ID='cid', TRIMBLE_CLIENT_SECRET='secret',
                        TRIMBLE_API_BASE='https://app.connect.example.test')

    def test_connection_healthy_marks_connected(self):
        with self._env(), mock.patch('apps.settings.services._http_probe',
                                     return_value=(True, 200, 55, '')):
            result = BIMProvider.test_connection(str(self.bim.id), user=self.user)

        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['response_time_ms'], 55)
        self.bim.refresh_from_db()
        self.assertEqual(self.bim.status, 'Connected')
        self.assertIsNotNone(self.bim.last_successful_sync)
        # Model counts are never touched by a health check.
        self.assertEqual(self.bim.synced_models_count, 5)

    def test_connection_unreachable_keeps_disconnected(self):
        with self._env(), mock.patch('apps.settings.services._http_probe',
                                     return_value=(False, 503, 120, 'HTTP 503')):
            result = BIMProvider.test_connection(str(self.bim.id), user=self.user)

        self.assertEqual(result['status'], 'UNREACHABLE')
        self.bim.refresh_from_db()
        self.assertEqual(self.bim.status, 'Disconnected')

    def test_sync_dispatches_real_celery_task(self):
        with self._env(), mock.patch(
                'apps.digital_eye.tasks.trimble_sync.delay') as task_delay:
            bim = BIMProvider.sync(str(self.bim.id), user=self.user)

        task_delay.assert_called_once()
        self.assertEqual(bim.synced_models_count, 5)  # count unchanged here
        self.assertTrue(IntegrationLog.objects.filter(
            event_name__contains='Model sync dispatched',
            status='Success').exists())
        self.assertTrue(AuditEvent.objects.filter(
            action='INTEGRATION_SYNC_DISPATCHED').exists())


class DocumentProviderProbeTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='dms_probe_admin', email='dms.probe@government.gov.ng',
            password='Password123!')

    def _r2_env(self):
        return env_with(
            CLOUDFLARE_R2_ACCESS_KEY_ID='key', CLOUDFLARE_R2_SECRET_ACCESS_KEY='secret',
            CLOUDFLARE_R2_ENDPOINT_URL='https://r2.example.test',
            CLOUDFLARE_R2_BUCKET_NAME='nexucon-docs')

    def test_r2_head_bucket_success_marks_active(self):
        dms = DocumentSystemIntegration.objects.create(
            name='R2 Probe Store', storage_provider='Cloudflare R2',
            bucket_or_drive_name='nexucon-docs', endpoint_url='',
            status='Disconnected')
        with self._r2_env(), mock.patch('boto3.client') as boto_client:
            result = DocumentProvider.test_connection(str(dms.id), user=self.user)

        boto_client.assert_called_once()
        boto_client.return_value.head_bucket.assert_called_once_with(
            Bucket='nexucon-docs')
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertIsNotNone(result['response_time_ms'])
        dms.refresh_from_db()
        self.assertEqual(dms.status, 'Active')
        self.assertTrue(AuditEvent.objects.filter(
            action='INTEGRATION_TESTED', resource_id=str(dms.id)).exists())

    def test_r2_head_bucket_failure_is_unreachable(self):
        from botocore.exceptions import ClientError
        dms = DocumentSystemIntegration.objects.create(
            name='R2 Broken Store', storage_provider='Cloudflare R2',
            bucket_or_drive_name='nexucon-docs', endpoint_url='',
            status='Disconnected')
        with self._r2_env(), mock.patch('boto3.client') as boto_client:
            boto_client.return_value.head_bucket.side_effect = ClientError(
                {'Error': {'Code': '403', 'Message': 'Forbidden'}}, 'HeadBucket')
            result = DocumentProvider.test_connection(str(dms.id), user=self.user)

        self.assertEqual(result['status'], 'UNREACHABLE')
        dms.refresh_from_db()
        self.assertEqual(dms.status, 'Disconnected')
        self.assertTrue(IntegrationLog.objects.filter(
            service_name='R2 Broken Store', status='Failed').exists())

    def test_cloudinary_ping_success(self):
        dms = DocumentSystemIntegration.objects.create(
            name='Cloudinary Media', storage_provider='Cloudinary',
            bucket_or_drive_name='media', endpoint_url='', status='Disconnected')
        env = env_with(CLOUDINARY_CLOUD_NAME='nexucon', CLOUDINARY_API_KEY='k',
                       CLOUDINARY_API_SECRET='s')
        with env, mock.patch('cloudinary.api.ping') as ping:
            result = DocumentProvider.test_connection(str(dms.id), user=self.user)

        ping.assert_called_once()
        self.assertEqual(result['status'], 'HEALTHY')
        dms.refresh_from_db()
        self.assertEqual(dms.status, 'Active')

    def test_plain_endpoint_probe_uses_real_http(self):
        dms = DocumentSystemIntegration.objects.create(
            name='SharePoint Bridge', storage_provider='SharePoint',
            bucket_or_drive_name='sites/nexucon', endpoint_url='https://tenant.example.test',
            status='Disconnected')
        with mock.patch('apps.settings.services._http_probe',
                        return_value=(True, 200, 75, '')):
            result = DocumentProvider.test_connection(str(dms.id), user=self.user)
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['response_time_ms'], 75)

        with mock.patch('apps.settings.services._http_probe',
                        return_value=(False, None, 75, 'timeout')):
            result = DocumentProvider.test_connection(str(dms.id), user=self.user)
        self.assertEqual(result['status'], 'UNREACHABLE')

    def test_provider_without_endpoint_or_credentials_is_pending(self):
        dms = DocumentSystemIntegration.objects.create(
            name='Generic DMS', storage_provider='Unclassified',
            bucket_or_drive_name='bucket', endpoint_url='', status='Disconnected')
        state, latency_ms, detail = DocumentProvider._probe_storage(dms)
        self.assertEqual(state, 'PENDING_CREDENTIALS')
        self.assertIsNone(latency_ms)
        self.assertIn('No network call was made', detail)

    def test_sync_only_touches_last_sync_on_healthy_probe(self):
        dms = DocumentSystemIntegration.objects.create(
            name='R2 Sync Store', storage_provider='Cloudflare R2',
            bucket_or_drive_name='nexucon-docs', endpoint_url='',
            status='Active', synced_files_count=10)
        with self._r2_env(), mock.patch('boto3.client'):
            returned = DocumentProvider.sync(str(dms.id), user=self.user)
        self.assertEqual(returned.synced_files_count, 10)
        self.assertTrue(AuditEvent.objects.filter(
            action='INTEGRATION_SYNC_COMPLETED',
            resource_id=str(dms.id)).exists())


class GovernmentAPIProviderProbeTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='gov_probe_admin', email='gov.probe@government.gov.ng',
            password='Password123!')
        self.gov = GovernmentAPIIntegration.objects.create(
            api_key_identifier='cac_probe', name='CAC Probe Registry',
            provider_code='CAC', endpoint_url='https://api.cac.gov.ng/test')

    def test_connection_healthy_persists_connected(self):
        with env_with(GOV_CAC_API_TOKEN='real-token'), \
                mock.patch('apps.settings.services._http_probe',
                           return_value=(True, 200, 65, '')) as probe:
            result = GovernmentAPIProvider.test_connection(
                str(self.gov.id), user=self.user)

        probe.assert_called_once()
        self.assertEqual(result['status'], 'HEALTHY')
        self.assertEqual(result['response_time_ms'], 65)
        self.gov.refresh_from_db()
        self.assertEqual(self.gov.status, 'connected')
        self.assertTrue(AuditEvent.objects.filter(
            action='INTEGRATION_TESTED', resource_id=str(self.gov.id)).exists())

    def test_connection_unreachable_is_reported(self):
        self.gov.status = 'pending_credentials'
        self.gov.save()
        with env_with(GOV_CAC_API_TOKEN='real-token'), \
                mock.patch('apps.settings.services._http_probe',
                           return_value=(False, 502, 300, 'HTTP 502')):
            result = GovernmentAPIProvider.test_connection(
                str(self.gov.id), user=self.user)

        self.assertEqual(result['status'], 'UNREACHABLE')
        self.gov.refresh_from_db()
        self.assertEqual(self.gov.status, 'pending_credentials')

    def test_verify_entity_without_configured_endpoint_is_pending_configuration(self):
        # Token configured but no e-GIS integration row (hence no endpoint URL)
        # — the lookup must report PENDING_CONFIGURATION, never a verdict.
        with env_with(GOV_EGIS_API_TOKEN='real-token'):
            result = GovernmentAPIProvider.verify_entity(
                'EGIS', 'LR/12345/1998', user=self.user)
        self.assertEqual(result['status'], 'PENDING_CONFIGURATION')
        self.assertFalse(result['verified'])
        self.assertIn('No Lagos e-GIS Land Registry', result['reason'])

    def test_verify_entity_lookup_completed_returns_agency_response_verbatim(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"status": "ACTIVE", "companyName": "Acme Ltd"}'
        with env_with(GOV_CAC_API_TOKEN='real-token'), \
                mock.patch('urllib.request.urlopen', return_value=response):
            result = GovernmentAPIProvider.verify_entity(
                'CAC', 'RC-1849204', user=self.user)

        self.assertEqual(result['status'], 'LOOKUP_COMPLETED')
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(result['agency_response'],
                         {'status': 'ACTIVE', 'companyName': 'Acme Ltd'})
        self.assertTrue(IntegrationLog.objects.filter(
            event_name__contains='RC-1849204', status='Success').exists())
        self.assertTrue(AuditEvent.objects.filter(
            action='REGULATORY_ENTITY_LOOKUP_COMPLETED').exists())

    def test_verify_entity_agency_rejection_is_reported(self):
        error = urllib.error.HTTPError(
            'https://api.cac.gov.ng/test/RC-1849204', 403, 'Forbidden', None, None)
        with env_with(GOV_CAC_API_TOKEN='real-token'), \
                mock.patch('urllib.request.urlopen', side_effect=error):
            result = GovernmentAPIProvider.verify_entity(
                'CAC', 'RC-1849204', user=self.user)

        self.assertEqual(result['status'], 'AGENCY_REJECTED')
        self.assertFalse(result['verified'])
        self.assertTrue(IntegrationLog.objects.filter(
            event_name__contains='RC-1849204', status='Failed').exists())

    def test_verify_entity_network_failure_is_unreachable(self):
        with env_with(GOV_CAC_API_TOKEN='real-token'), \
                mock.patch('urllib.request.urlopen',
                           side_effect=urllib.error.URLError('timed out')):
            result = GovernmentAPIProvider.verify_entity(
                'CAC', 'RC-1849204', user=self.user)

        self.assertEqual(result['status'], 'UNREACHABLE')
        self.assertFalse(result['verified'])


class IntegrationStatsTestCase(TestCase):
    def test_stats_count_real_logs_and_integrations(self):
        IntegrationService.log_integration_event(
            'Tersus GNSS', 'ping ok', status='Success', duration_ms=10)
        IntegrationService.log_integration_event(
            'Tersus GNSS', 'ping failed', status='Failed', duration_ms=10)
        old_log = IntegrationService.log_integration_event(
            'Tersus GNSS', 'stale ping', status='Failed', duration_ms=10)
        IntegrationLog.objects.filter(pk=old_log.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=2))

        TersusDevice.objects.create(device_id='T-STATS-1', name='Active rover',
                                    status='Active')
        TersusDevice.objects.create(device_id='T-STATS-2', name='Idle rover',
                                    status='Offline')
        BIMIntegration.objects.create(provider='Stats BIM', status='Connected')
        DocumentSystemIntegration.objects.create(name='Stats DMS', status='Active')
        WebhookSubscription.objects.create(name='Stats hook',
                                           target_url='https://hooks.example.test/x',
                                           status='Active')

        stats = IntegrationService.get_integration_stats()
        self.assertEqual(stats['total_requests_24h'], '2')  # stale log excluded
        self.assertEqual(stats['failed_requests_rate'], '50.0%')
        self.assertEqual(stats['active_webhooks'], 1)
        self.assertEqual(stats['active_devices_count'], 1)
        self.assertEqual(stats['total_devices_count'], 2)
        self.assertEqual(stats['connected_bim_count'], 1)
        self.assertEqual(stats['active_dms_count'], 1)

    def test_stats_without_logs_have_no_failure_rate(self):
        stats = IntegrationService.get_integration_stats()
        self.assertEqual(stats['total_requests_24h'], '0')
        self.assertIsNone(stats['failed_requests_rate'])


class StaffDirectoryServiceTestCase(TestCase):
    def setUp(self):
        self.director = User.objects.create_superuser(
            username='staff_admin', email='staff.admin@government.gov.ng',
            password='Password123!', first_name='Staff', last_name='Admin')

    def test_staff_users_include_pending_invitations_not_yet_accepted(self):
        UserInvitation.objects.create(
            email='pending.engineer@government.gov.ng',
            name='Engr. Pending Engineer', role='Lead Inspector',
            department='Structural Engineering', status='Pending')

        users = SettingsService.get_staff_users()
        pending = [u for u in users
                   if u['email'] == 'pending.engineer@government.gov.ng']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'Pending')
        self.assertEqual(pending[0]['lastLogin'], 'Invite Sent')
        self.assertEqual(pending[0]['role'], 'Lead Inspector')

    def test_staff_users_deduplicate_accepted_invites(self):
        # A pending invitation for an email that is already a real user must
        # not be listed twice.
        User.objects.create_user(
            username='dup@government.gov.ng', email='dup@government.gov.ng',
            password='Password123!', first_name='Dup', last_name='User')
        UserInvitation.objects.create(
            email='dup@government.gov.ng', name='Dup User',
            status='Pending')

        users = SettingsService.get_staff_users()
        self.assertEqual(
            len([u for u in users if u['email'] == 'dup@government.gov.ng']), 1)

    def test_staff_users_filters(self):
        User.objects.create_user(
            username='zaha@government.gov.ng', email='zaha@government.gov.ng',
            password='Password123!', first_name='Zaha', last_name='Hadid')
        UserInvitation.objects.create(
            email='invite.search@government.gov.ng', name='Searchable Invite',
            role='City Planner', status='Pending')

        by_name = SettingsService.get_staff_users(search='zaha hadid')
        self.assertEqual(len(by_name), 1)
        self.assertEqual(by_name[0]['email'], 'zaha@government.gov.ng')

        by_invite = SettingsService.get_staff_users(search='searchable')
        self.assertEqual(len(by_invite), 1)
        self.assertEqual(by_invite[0]['status'], 'Pending')

        self.assertEqual(SettingsService.get_staff_users(search='nonexistent'), [])
        # Default department is 'Urban Planning' for every user.
        self.assertTrue(SettingsService.get_staff_users(department='urban planning'))
        self.assertEqual(SettingsService.get_staff_users(department='marine'), [])
        # Superusers carry the 'System Administrator' role label.
        self.assertEqual(
            len(SettingsService.get_staff_users(role='system administrator')), 1)
        self.assertEqual(SettingsService.get_staff_users(role='diver'), [])

    def test_invite_user_creates_user_and_invitation(self):
        with mock.patch('apps.notifications.email_service.'
                        'EmailService.send_invitation_email') as send:
            invitation = SettingsService.invite_user(
                'new.engineer@government.gov.ng', 'Engr. New Engineer',
                role='Reviewer', department='Urban Planning',
                invited_by=self.director)

        send.assert_called_once()
        self.assertEqual(invitation.status, 'Pending')
        self.assertIsNotNone(invitation.expires_at)

        user = User.objects.get(email='new.engineer@government.gov.ng')
        self.assertEqual(user.first_name, 'Engr.')
        self.assertEqual(user.last_name, 'New Engineer')
        self.assertFalse(user.is_verified)
        self.assertTrue(AuditEvent.objects.filter(
            action='INVITE_STAFF_USER', resource_id=str(invitation.id)).exists())

    def test_invite_user_resets_existing_user(self):
        User.objects.create_user(
            username='existing@government.gov.ng', email='existing@government.gov.ng',
            password='OldPassword123!')
        with mock.patch('apps.notifications.email_service.'
                        'EmailService.send_invitation_email'):
            invitation = SettingsService.invite_user(
                'existing@government.gov.ng', 'Existing Staff')

        self.assertEqual(invitation.email, 'existing@government.gov.ng')
        user = User.objects.get(email='existing@government.gov.ng')
        self.assertEqual(user.first_name, 'Existing')

    def test_invite_user_survives_email_dispatch_failure(self):
        with mock.patch('apps.notifications.email_service.'
                        'EmailService.send_invitation_email',
                        side_effect=RuntimeError('Resend unavailable')):
            invitation = SettingsService.invite_user(
                'mailer.down@government.gov.ng', 'Mailer Down')
        self.assertEqual(invitation.status, 'Pending')
        self.assertTrue(User.objects.filter(
            email='mailer.down@government.gov.ng').exists())

    def test_accept_invitation_unknown_email_fails(self):
        result = SettingsService.accept_invitation('nobody@nowhere.gov.ng')
        self.assertFalse(result['success'])
        self.assertIn('No invitation found', result['message'])

    def test_accept_invitation_activates_user_and_returns_jwt(self):
        with mock.patch('apps.notifications.email_service.'
                        'EmailService.send_invitation_email'):
            SettingsService.invite_user(
                'activate.me@government.gov.ng', 'Activate Me',
                role='City Planner', department='Transport',
                invited_by=self.director)

        result = SettingsService.accept_invitation(
            'activate.me@government.gov.ng', password='BrandNewPass123!',
            full_name='Activated Me')

        self.assertTrue(result['success'])
        self.assertIn('access', result)
        self.assertIn('refresh', result)
        self.assertEqual(result['user']['role_name'], 'City Planner')
        self.assertEqual(result['user']['department'], 'Transport')
        self.assertTrue(result['user']['is_verified'])

        user = User.objects.get(email='activate.me@government.gov.ng')
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_verified)
        self.assertTrue(user.check_password('BrandNewPass123!'))
        invitation = UserInvitation.objects.get(email='activate.me@government.gov.ng')
        self.assertEqual(invitation.status, 'Accepted')

    def test_accept_invitation_without_invitation_record_creates_account(self):
        User.objects.create_user(
            username='plain.user@government.gov.ng',
            email='plain.user@government.gov.ng', password='Password123!')
        result = SettingsService.accept_invitation(
            'plain.user@government.gov.ng', password='Rotated123!')
        self.assertTrue(result['success'])
        user = User.objects.get(email='plain.user@government.gov.ng')
        self.assertTrue(user.is_verified)
        self.assertTrue(user.check_password('Rotated123!'))

    def test_validate_inspector_invitation_valid_and_invalid_code(self):
        invitation = SettingsService.invite_user(
            email='inspector.test@government.gov.ng',
            name='Engr. Test Inspector',
            role='Inspector',
            department='Building Inspectorate',
            invite_code='TEST-1234'
        )

        # 1. Successful validation with valid token and code
        res = SettingsService.validate_inspector_invitation(token=str(invitation.id), invite_code='TEST-1234')
        self.assertTrue(res['valid'])
        self.assertEqual(res['email'], 'inspector.test@government.gov.ng')
        self.assertEqual(res['role'], 'Inspector')
        self.assertIsNotNone(res['temporary_password'])

        # 2. Rejection with invalid code
        bad_res = SettingsService.validate_inspector_invitation(token=str(invitation.id), invite_code='WRONG-CODE')
        self.assertFalse(bad_res['valid'])
        self.assertEqual(bad_res['error_code'], 'INVALID_CODE')

        # 3. Accept invitation
        accept_res = SettingsService.accept_invitation(
            email='inspector.test@government.gov.ng',
            token=str(invitation.id),
            password='PermPassword123!'
        )
        self.assertTrue(accept_res['success'])

        # 4. Rejection after acceptance (single-use token)
        accepted_res = SettingsService.validate_inspector_invitation(token=str(invitation.id), invite_code='TEST-1234')
        self.assertFalse(accepted_res['valid'])
        self.assertEqual(accepted_res['error_code'], 'ALREADY_ACCEPTED')


class SettingsServiceDomainTestCase(TestCase):
    """Roles, workflows, templates, standards, notifications, webhooks."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='domain_admin', email='domain.admin@government.gov.ng',
            password='Password123!')

    # ---------------- roles ----------------

    def test_create_custom_role_duplicates_are_rejected(self):
        SettingsService.create_custom_role('Unique Reviewer')
        with self.assertRaises(ValidationError):
            SettingsService.create_custom_role('unique reviewer')

    def test_get_roles_matrix_reflects_stored_permissions(self):
        SettingsService.update_role_permission(
            'City Planner', 'Permits & Approvals', 'Approve/Reject Permits', True)
        matrix = SettingsService.get_roles_matrix()
        modules = {m['module']: m for m in matrix['permission_modules']}
        permits = modules['Permits & Approvals']['permissions']
        approve = next(p for p in permits if 'Approve/Reject' in p['name'])
        self.assertTrue(approve['planner'])

    def test_update_role_permission_creates_missing_role(self):
        rp = SettingsService.update_role_permission(
            'Newly Seen Role', 'System & Audit', 'View Audit Records', True)
        self.assertTrue(rp.is_granted)
        self.assertTrue(CustomRole.objects.filter(name='Newly Seen Role').exists())

    # ---------------- workflows ----------------

    def test_create_workflow_with_plain_string_steps(self):
        wf = SettingsService.create_workflow(
            'String Step Workflow', ['First review', 'Final approval'],
            description='Chained approvals')
        self.assertEqual(wf.steps.count(), 2)
        first, second = wf.steps.all().order_by('step_order')
        self.assertEqual(first.title, 'First review')
        self.assertEqual(first.role, 'Reviewer')
        self.assertEqual(second.step_order, 2)

        listed = SettingsService.get_workflows()
        self.assertIn(wf, listed)

    # ---------------- templates ----------------

    def test_create_template_with_string_items_and_append(self):
        tpl = SettingsService.create_template(
            'String Template', 'Safety', ['Guardrail present', 'Harness anchored'])
        self.assertEqual(tpl.items.count(), 2)

        item = SettingsService.add_checklist_item(str(tpl.id), 'Ladder secured',
                                                  field_type='Pass/Fail Toggle')
        self.assertEqual(item.item_order, 3)
        self.assertTrue(item.is_required)

        listed = SettingsService.get_templates()
        self.assertIn(tpl, listed)

        self.assertTrue(SettingsService.delete_template(str(tpl.id)))
        self.assertFalse(InspectionTemplate.objects.filter(pk=tpl.pk).exists())

    # ---------------- standards & statutes ----------------

    def test_standards_and_statutory_documents(self):
        listed = SettingsService.get_standards()
        self.assertTrue(listed.filter(key='noise_daytime_db').exists())

        updated = SettingsService.update_standards({
            'noise_daytime_db': 82.5,
            'unknown_key': 1.0,       # silently skipped
            'max_concrete_slump_in': 'not-a-number',  # bad value skipped
        })
        updated_keys = {s.key for s in updated}
        self.assertEqual(updated_keys, {'noise_daytime_db'})
        self.assertEqual(
            ComplianceStandard.objects.get(key='noise_daytime_db').num_value, 82.5)

        doc = SettingsService.add_statutory_document(
            'TEST-REG-01', 'Test Regulation', ['Feature A', 'Feature B'],
            document_url='https://example.test/reg')
        self.assertIn(doc, SettingsService.get_statutory_documents())

    # ---------------- notification preferences ----------------

    def test_notification_preferences_grouping(self):
        prefs = SettingsService.get_notification_preferences()
        categories = {p['category'] for p in prefs}
        self.assertEqual(categories, {'Critical Safety Incidents',
                                      'Permits & Approvals', 'Field Inspections'})
        critical = next(p for p in prefs
                        if p['category'] == 'Critical Safety Incidents')
        self.assertTrue(critical['items'])  # seeded locked preference present

    def test_update_notification_preference_sms_and_new_entries(self):
        pref = SettingsService.update_notification_preference(
            'Permits & Approvals', 'New Permit Application', 'sms', True)
        self.assertTrue(pref.sms)

        # Unknown (category, event) pairs are created with safe defaults.
        new_pref = SettingsService.update_notification_preference(
            'Permits & Approvals', 'Permit Withdrawn', 'email', False)
        self.assertFalse(new_pref.email)
        self.assertTrue(new_pref.in_app)

    def test_locked_critical_channels_cannot_be_disabled(self):
        with self.assertRaises(PermissionDenied):
            SettingsService.update_notification_preference(
                'Critical Safety Incidents', 'In-App Dashboard Alerts',
                'in_app', False)

    # ---------------- routing rules & webhooks ----------------

    def test_routing_rules_and_webhooks_crud(self):
        rule = SettingsService.add_routing_rule(
            'Test Trigger', 'Chief Inspector', 'Within 1 hour', 'Director')
        self.assertIn(rule, SettingsService.get_routing_rules())
        self.assertTrue(SettingsService.delete_routing_rule(str(rule.id)))
        self.assertFalse(NotificationRoutingRule.objects.filter(pk=rule.pk).exists())

        hook = SettingsService.create_webhook('Analytics Bridge',
                                              'https://hooks.example.test/a', None)
        # No events supplied -> the documented default event list.
        self.assertIn('permit.created', hook.events)
        self.assertIn(hook, SettingsService.get_webhooks())
        self.assertTrue(SettingsService.delete_webhook(str(hook.id)))

    def test_agency_profile_is_created_on_demand(self):
        AgencyProfile.objects.all().delete()
        profile = SettingsService.get_agency_profile()
        self.assertIsNotNone(profile.id)
        self.assertEqual(profile.agency_code, 'LASG-MPPUD-01')

