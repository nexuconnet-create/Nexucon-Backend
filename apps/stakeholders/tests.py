from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient
from apps.stakeholders.models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, BlacklistRecord, StakeholderMeeting, StakeholderMessage
)
from apps.stakeholders.services import StakeholderService

User = get_user_model()

class StakeholderTestCase(TestCase):
    def setUp(self):
        # Agency Head user (Director General)
        self.agency_head = User.objects.create_superuser(
            username='director_general',
            email='dg@government.gov.ng',
            password='Password123!',
            first_name='Babatunde',
            last_name='Sanwo'
        )
        # Regular field officer (non-agency head)
        self.regular_officer = User.objects.create_user(
            username='field_officer',
            email='officer@government.gov.ng',
            password='Password123!',
            first_name='John',
            last_name='Doe'
        )
        self.client = APIClient()

    def test_schedule_meeting_agency_head_authorized(self):
        """Test that Agency Head can successfully schedule an official meeting."""
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/meetings/', {
            "title": "High-Rise Safety & BIM Review",
            "agenda": "Review slab deflection and MEP coordination.",
            "project_name": "Nexus Tower (Phase 1)",
            "date": "Oct 28, 2026",
            "time_slot": "10:00 AM - 11:30 AM",
            "meeting_type": "Video Call"
        })
        self.assertEqual(res.status_code, 201)
        self.assertIn('MTG-', res.data['meeting_reference'])
        self.assertEqual(res.data['status'], 'Scheduled')

    def test_schedule_meeting_non_agency_head_forbidden(self):
        """Test that non-agency-head users receive PermissionDenied when attempting to schedule."""
        self.client.force_authenticate(user=self.regular_officer)
        # Directly test service RBAC enforcement
        with self.assertRaises(PermissionDenied):
            StakeholderService.schedule_meeting({
                "title": "Unauthorized Meeting",
                "agenda": "Test agenda"
            }, user=self.regular_officer)

    def test_start_meeting_call_room(self):
        """Test launching live audio/video call room."""
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title="Slab Review Session",
            agenda="Discuss foundation",
            status="Scheduled"
        )

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/start/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'In Progress')
        self.assertTrue(res.data['room_id'].startswith('room-'))

    def test_send_stakeholder_message(self):
        """Test sending message into coordination channel."""
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/messages/', {
            "channel_name": "Site Safety & Inspections",
            "project_name": "Central Metro Transit Hub",
            "message_text": "Please submit revised soil test logs.",
            "is_urgent": True
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['is_urgent'])

    def test_toggle_blacklist(self):
        """Test blacklisting a recurring offender."""
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/blacklist/toggle/', {
            "entity_type": "Contractor",
            "entity_id": "CON-912",
            "entity_name": "StoneBridge Foundations",
            "reason": "Repeated non-compliance with trench safety protocols.",
            "status": "Blacklisted"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Blacklisted')

    def test_reassign_inspector_zone(self):
        """Test reassigning inspector jurisdiction zone."""
        self.client.force_authenticate(user=self.agency_head)
        inspector = Inspector.objects.create(
            inspector_id="INS-001",
            name="Marcus Chen",
            assigned_zone="Zone A"
        )

        res = self.client.post(f'/api/v1/stakeholders/inspectors/{inspector.id}/reassign-zone/', {
            "zone": "Zone C (East Corridor)"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['assigned_zone'], 'Zone C (East Corridor)')

    def test_validate_license_via_api(self):
        """Test external API verification of license."""
        self.client.force_authenticate(user=self.agency_head)
        contractor = Contractor.objects.create(
            contractor_id="CON-304",
            name="Apex Construction",
            license_number="LIC-COREN-992"
        )

        res = self.client.post(f'/api/v1/stakeholders/contractors/{contractor.id}/validate-license/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'VALID')
        self.assertTrue(res.data['is_verified'])

    def test_stakeholder_stats(self):
        """Test summary statistics endpoint."""
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.get('/api/v1/stakeholders/stats/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('active_inspectors', res.data)
        self.assertIn('global_pass_rate', res.data)
