from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .models import Notification, EmailDelivery, NotificationPreference
from .services import NotificationService

User = get_user_model()

class NotificationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='director_notif',
            email='director.notif@government.gov.ng',
            password='Password123!',
            first_name='Director',
            last_name='General'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_notification_creation_and_api_list(self):
        notif = Notification.objects.create(
            recipient=self.user,
            category='APPLICATIONS',
            title='New Permit Application Submitted',
            message='Eko Atlantic Phase 2 building permit submitted.',
            priority='High'
        )
        res = self.client.get('/api/v1/notifications/')
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.data), 1)

    def test_mark_notification_as_read(self):
        notif = Notification.objects.create(
            recipient=self.user,
            category='INSPECTIONS',
            title='Inspection Scheduled',
            message='Foundation inspection scheduled.',
            is_read=False
        )
        res = self.client.post(f'/api/v1/notifications/{notif.id}/read/')
        self.assertEqual(res.status_code, 200)
        notif.refresh_from_db()
        self.assertTrue(notif.is_read)

    def test_acknowledge_emergency_notification(self):
        notif = Notification.objects.create(
            recipient=self.user,
            category='EMERGENCY',
            title='Emergency Scaffold Alert',
            message='Scaffold failure detected at Sector 4.',
            is_acknowledged=False
        )
        res = self.client.post(f'/api/v1/notifications/{notif.id}/acknowledge/')
        self.assertEqual(res.status_code, 200)
        notif.refresh_from_db()
        self.assertTrue(notif.is_acknowledged)

    def test_unread_counts_endpoint(self):
        res = self.client.get('/api/v1/notifications/unread-counts/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_unread', res.data)
        self.assertIn('emergency', res.data)

    def test_notification_preferences_get_and_update(self):
        res = self.client.get('/api/v1/notifications/preferences/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['email_enabled'])

        res_update = self.client.put('/api/v1/notifications/preferences/', {
            "email_applications": False,
            "email_emergency": True
        })
        self.assertEqual(res_update.status_code, 200)
        self.assertFalse(res_update.data['email_applications'])

    def test_notification_service_dispatch_and_email_deduplication(self):
        """Test dispatching event creates notification and EmailDelivery without duplicate sends."""
        notif = NotificationService.dispatch_event(
            event_type="APPROVAL_REQUIRED",
            title="Technical Approval Required: APR-991",
            message="Structural review requires your signature.",
            category="APPROVALS",
            priority="High",
            recipient=self.user,
            entity_type="ApprovalRequest",
            entity_id="apr-991"
        )
        self.assertIsNotNone(notif)
        
        deliveries = EmailDelivery.objects.filter(recipient_user=self.user)
        self.assertEqual(deliveries.count(), 1)
        self.assertEqual(deliveries.first().template_key, "approval_required")

        # Second identical dispatch with same idempotency key should not create second email
        notif2 = NotificationService.dispatch_event(
            event_type="APPROVAL_REQUIRED",
            title="Technical Approval Required: APR-991",
            message="Structural review requires your signature.",
            category="APPROVALS",
            priority="High",
            recipient=self.user,
            entity_type="ApprovalRequest",
            entity_id="apr-991"
        )
        self.assertEqual(EmailDelivery.objects.filter(recipient_user=self.user).count(), 1)

    def test_respond_to_notification_directive(self):
        """Test submitting official directive response from sidepop drawer."""
        notif = Notification.objects.create(
            recipient=self.user,
            category='COMPLIANCE',
            title='Non-conformance NCR-019 Flagged',
            message='Rebar spacing defect flagged at Section 3.',
            priority='High',
            is_read=False
        )
        res = self.client.post(f'/api/v1/notifications/{notif.id}/respond/', {
            'comment': 'Directing site engineer to re-verify rebar ties before 5 PM.'
        })
        self.assertEqual(res.status_code, 200)
        notif.refresh_from_db()
        self.assertTrue(notif.is_read)
        self.assertEqual(notif.metadata.get('last_directive'), 'Directing site engineer to re-verify rebar ties before 5 PM.')
        self.assertEqual(len(notif.metadata.get('responses', [])), 1)
