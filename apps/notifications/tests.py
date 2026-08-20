from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.notifications.models import Notification, NotificationPreference
from apps.notifications.services import NotificationService

User = get_user_model()

class NotificationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='alert_officer',
            email='alerts@government.gov.ng',
            password='Password123!',
            first_name='Safety',
            last_name='Supervisor'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_send_notification_and_email_dispatch(self):
        """Test creating notification and safe Resend email dispatch."""
        notif = NotificationService.send_notification({
            "category": "CRITICAL",
            "title": "Imminent Wall Collapse Hazard",
            "message": "Immediate excavation halt requested at Sector 3.",
            "priority": "Critical",
            "location": "Sector 3 Deep Foundation",
            "action_required": "Dispatch emergency structural crew.",
            "recipient_email": "alerts@government.gov.ng"
        }, self.user)

        self.assertIsNotNone(notif.id)
        self.assertEqual(notif.priority, 'Critical')
        self.assertEqual(notif.category, 'CRITICAL')
        self.assertFalse(notif.is_read)

    def test_mark_as_read(self):
        """Test marking single notification as read."""
        notif = Notification.objects.create(
            category='APPLICATIONS',
            title='Zoning Variance Application',
            message='New application submitted.',
            priority='Medium'
        )

        res = self.client.post(f'/api/v1/notifications/{notif.id}/read/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['is_read'])
        self.assertIsNotNone(res.data['read_at'])

    def test_mark_all_as_read(self):
        """Test bulk marking notifications as read."""
        Notification.objects.create(category='INSPECTIONS', title='Walkthrough 1', message='Test 1')
        Notification.objects.create(category='INSPECTIONS', title='Walkthrough 2', message='Test 2')

        res = self.client.post('/api/v1/notifications/mark-all-read/', {"category": "INSPECTIONS"})
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.data['marked_read_count'], 2)

    def test_acknowledge_critical_incident(self):
        """Test acknowledging critical work stoppage."""
        notif = Notification.objects.create(
            category='CRITICAL',
            title='Trench Wall Collapse',
            message='Evacuate workers.',
            priority='Critical'
        )

        res = self.client.post(f'/api/v1/notifications/{notif.id}/acknowledge/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['is_acknowledged'])
        self.assertTrue(res.data['is_read'])

    def test_sound_site_alarm(self):
        """Test broadcasting site-wide audible emergency alarm."""
        res = self.client.post('/api/v1/notifications/sound-alarm/', {
            "location": "North Substation Yard",
            "reason": "Gas leak detected by atmospheric sensor."
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['category'], 'CRITICAL')
        self.assertIn('EMERGENCY: Site Alarm Triggered', res.data['title'])

    def test_ping_assignee(self):
        """Test sending reminder ping / email to assignee."""
        notif = Notification.objects.create(
            category='OVERDUE',
            title='Review Overdue',
            message='SLA exceeded by 5 days.',
            priority='High'
        )

        res = self.client.post(f'/api/v1/notifications/{notif.id}/ping/', {"method": "Email"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Success')

    def test_unread_counts(self):
        """Test retrieving unread notification counters."""
        res = self.client.get('/api/v1/notifications/unread-counts/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_unread', res.data)
        self.assertIn('critical', res.data)
        self.assertIn('applications', res.data)
