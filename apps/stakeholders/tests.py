from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient
from apps.stakeholders.models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam, BlacklistRecord,
    StakeholderMeeting, StakeholderMessage, MessageTranslation, MeetingActionItem
)
from apps.stakeholders.services import StakeholderService
from apps.stakeholders.translation import TranslationService

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

    def test_add_meeting_action_item(self):
        """Test adding action items to meeting."""
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title="Council Review",
            agenda="Deliverables",
            status="Scheduled"
        )
        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/add-action-item/', {
            "title": "Submit GPR Core Samples",
            "assignee_name": "GeoTech Lab",
            "due_date": "Within 48 Hours"
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['title'], "Submit GPR Core Samples")

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

    def test_translate_message_yoruba_igbo_hausa(self):
        """Test translation into Yorùbá, Igbo, and Hausa and verify DB caching."""
        self.client.force_authenticate(user=self.agency_head)
        msg = StakeholderMessage.objects.create(
            sender_name="Lead Inspector",
            channel_name="General Council",
            message_text="Please submit the inspection report."
        )

        # 1. Translate to Yorùbá
        res_yo = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/', {
            "target_language": "yo"
        })
        self.assertEqual(res_yo.status_code, 200)
        self.assertEqual(res_yo.data['target_language'], 'yo')
        self.assertIn("ìròyìn àyẹ̀wò", res_yo.data['translated_content'].lower())
        self.assertFalse(res_yo.data['is_cached'])

        # Second call should be cached
        res_yo_cached = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/', {
            "target_language": "yo"
        })
        self.assertEqual(res_yo_cached.status_code, 200)
        self.assertTrue(res_yo_cached.data['is_cached'])

        # 2. Translate to Igbo
        res_ig = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/', {
            "target_language": "ig"
        })
        self.assertEqual(res_ig.status_code, 200)
        self.assertEqual(res_ig.data['target_language'], 'ig')
        self.assertIn("nyocha", res_ig.data['translated_content'].lower())

        # 3. Translate to Hausa
        res_ha = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/', {
            "target_language": "ha"
        })
        self.assertEqual(res_ha.status_code, 200)
        self.assertEqual(res_ha.data['target_language'], 'ha')
        self.assertIn("binciken", res_ha.data['translated_content'].lower())

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
            inspector_id="INS-999",
            name="David Okon",
            assigned_zone="Zone A"
        )
        res = self.client.post(f'/api/v1/stakeholders/inspectors/{inspector.id}/reassign-zone/', {
            "zone": "Zone C (Industrial Free Zone)"
        })
        self.assertEqual(res.status_code, 200)
        inspector.refresh_from_db()
        self.assertEqual(inspector.assigned_zone, "Zone C (Industrial Free Zone)")

    def test_validate_contractor_license(self):
        """Test live contractor license verification."""
        self.client.force_authenticate(user=self.agency_head)
        con = Contractor.objects.create(
            contractor_id="CON-777",
            name="Apex Builders",
            license_number="LIC-8812"
        )
        res = self.client.post(f'/api/v1/stakeholders/contractors/{con.id}/validate-license/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'VALID')
        self.assertTrue(res.data['is_verified'])

    def test_verify_professional_license(self):
        """Test verifying licensed professional credentials."""
        self.client.force_authenticate(user=self.agency_head)
        prof = LicensedProfessional.objects.create(
            name="Arc. Babatunde Jinadu",
            role_title="Principal Architect",
            firm_name="Studio Forma",
            license_authority="ARCON",
            is_verified=False
        )
        res = self.client.post(f'/api/v1/stakeholders/professionals/{prof.id}/verify-license/')
        self.assertEqual(res.status_code, 200)
        prof.refresh_from_db()
        self.assertTrue(prof.is_verified)

    def test_project_team_add_remove_member(self):
        """Test adding and removing members from Project Stakeholder Team Matrix."""
        self.client.force_authenticate(user=self.agency_head)
        team = ProjectStakeholderTeam.objects.create(
            project_reference="PRJ-101",
            project_name="Ocean View Tower",
            team_data={}
        )

        # Add MEP Consultant
        res_add = self.client.post(f'/api/v1/stakeholders/teams/{team.id}/add-member/', {
            "role_key": "mep_consultant",
            "member_data": {"name": "Horizon MEP", "role": "MEP Consultant", "initials": "HM"}
        }, format='json')
        self.assertEqual(res_add.status_code, 200)
        team.refresh_from_db()
        self.assertIn("mep_consultant", team.team_data)

        # Remove MEP Consultant
        res_rem = self.client.post(f'/api/v1/stakeholders/teams/{team.id}/remove-member/', {
            "role_key": "mep_consultant"
        }, format='json')
        self.assertEqual(res_rem.status_code, 200)
        team.refresh_from_db()
        self.assertNotIn("mep_consultant", team.team_data)
