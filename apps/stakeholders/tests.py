from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from contextlib import contextmanager
import datetime
import json
import os
import tempfile
import uuid
from rest_framework.test import APIClient
from apps.stakeholders.models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam, BlacklistRecord,
    StakeholderMeeting, StakeholderMessage, MessageTranslation, MeetingActionItem
)
from apps.stakeholders.services import StakeholderService
from apps.stakeholders.translation import TranslationService
from apps.stakeholders.google_calendar import (
    GoogleMeetCalendarService, GoogleMeetCalendarError,
    parse_display_datetime, extract_attendee_emails,
    _load_service_account_info, _normalize_private_key,
    CALENDAR_SCOPE, MEET_SCOPE,
)
from apps.audit.models import AuditEvent
from apps.notifications.models import Notification

User = get_user_model()

# Standard stub for the external Google Calendar/Meet call so the test suite
# never hits the real API (real integration is verified in live smoke tests).
GOOGLE_EVENT_STUB = {
    'event_id': 'stub-event-id',
    'html_link': 'https://www.google.com/calendar/event?eid=stub',
    'hangout_link': 'https://meet.google.com/stub-abc-mno',
    'meet_link_source': 'google_calendar_conference',
    'status': 'created_with_meet',
    'meet_error': '',
    'calendar_error': '',
    'attendees_invited': False,
}

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
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event',
                   return_value=dict(GOOGLE_EVENT_STUB)):
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
        self.assertEqual(res.data['google_meet_url'], 'https://meet.google.com/stub-abc-mno')
        self.assertEqual(res.data['meet_link_status'], 'meet_available')
        self.assertEqual(res.data['google_calendar_event_id'], 'stub-event-id')

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
        content_yo = res_yo.data['translated_content'].lower()
        self.assertTrue(any(w in content_yo for w in ["ayẹwo", "ayewo", "ijabọ", "ìròyìn"]))
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
        content_ig = res_ig.data['translated_content'].lower()
        self.assertTrue(any(w in content_ig for w in ["nyocha", "akụkọ", "ozugbo", "biko"]))

        # 3. Translate to Hausa
        res_ha = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/', {
            "target_language": "ha"
        })
        self.assertEqual(res_ha.status_code, 200)
        self.assertEqual(res_ha.data['target_language'], 'ha')
        content_ha = res_ha.data['translated_content'].lower()
        self.assertTrue(any(w in content_ha for w in ["dubawa", "rahoton", "binciken", "fatan"]))

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


# ---------------------------------------------------------------------------
# Helpers shared by the additional test classes below
# ---------------------------------------------------------------------------

GOOGLE_MEETING_ENV_KEYS = (
    'GOOGLE_MEETING_CLIENT_EMAIL',
    'GOOGLE_MEETING_PRIVATE_KEY',
    'GOOGLE_MEETING_PROJECT_ID',
)


@contextmanager
def no_google_meeting_credentials():
    """
    Simulate an environment with no Google service-account credentials at all:
    env vars unset AND no JSON key files on disk. Everything inside the block
    must honestly report pending/failure — never a fabricated Meet link.
    """
    with patch.dict(os.environ):
        for key in GOOGLE_MEETING_ENV_KEYS:
            os.environ.pop(key, None)
        with patch('apps.stakeholders.google_calendar._SA_FILE_CANDIDATES', []):
            yield


class BaseStakeholderServiceTestCase(TestCase):
    """Common fixtures + isolation of module-level credential caches."""

    def setUp(self):
        self.agency_head = User.objects.create_superuser(
            username='agency_head',
            email='head@government.gov.ng',
            password='Password123!',
            first_name='Amina',
            last_name='Bello'
        )
        self.regular_officer = User.objects.create_user(
            username='field_officer2',
            email='officer2@government.gov.ng',
            password='Password123!',
            first_name='Chidi',
            last_name='Okafor'
        )
        self.client = APIClient()

    def tearDown(self):
        # Never leak (possibly mocked) credentials between tests.
        GoogleMeetCalendarService._credentials = None
        TranslationService._cached_credentials = None


# ---------------------------------------------------------------------------
# google_calendar.py — pure helper functions
# ---------------------------------------------------------------------------

class GoogleCalendarHelperTests(TestCase):

    def test_normalize_private_key_with_literal_newlines(self):
        raw = '"-----BEGIN PRIVATE KEY-----\\nabc\\ndef\\n-----END PRIVATE KEY-----\\n"'
        key = _normalize_private_key(raw)
        self.assertTrue(key.startswith('-----BEGIN PRIVATE KEY-----\n'))
        self.assertIn('\nabc\ndef\n', key)
        self.assertTrue(key.endswith('\n'))

    def test_normalize_private_key_empty(self):
        self.assertIsNone(_normalize_private_key(None))
        self.assertIsNone(_normalize_private_key(''))
        self.assertIsNone(_normalize_private_key('   '))

    def test_load_service_account_info_from_env(self):
        with patch.dict(os.environ, {
            'GOOGLE_MEETING_CLIENT_EMAIL': 'nexucon-meeting@test.iam.gserviceaccount.com',
            'GOOGLE_MEETING_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n',
            'GOOGLE_MEETING_PROJECT_ID': 'my-project',
        }):
            info = _load_service_account_info()
        self.assertEqual(info['type'], 'service_account')
        self.assertEqual(info['client_email'], 'nexucon-meeting@test.iam.gserviceaccount.com')
        self.assertEqual(info['project_id'], 'my-project')
        self.assertIn('\n', info['private_key'])

    def test_load_service_account_info_project_fallback(self):
        with patch.dict(os.environ, {
            'GOOGLE_MEETING_CLIENT_EMAIL': 'sa@test.iam.gserviceaccount.com',
            'GOOGLE_MEETING_PRIVATE_KEY': 'keydata',
        }):
            with patch.dict(os.environ):
                os.environ.pop('GOOGLE_MEETING_PROJECT_ID', None)
                info = _load_service_account_info()
        self.assertEqual(info['project_id'], 'serious-water-469715-f9')

    def test_load_service_account_info_absent_everywhere(self):
        with no_google_meeting_credentials():
            self.assertIsNone(_load_service_account_info())

    def test_load_service_account_info_partial_env_falls_back_to_files(self):
        # Only the email is set — must fall through to the (patched-away) files.
        with patch.dict(os.environ, {'GOOGLE_MEETING_CLIENT_EMAIL': 'sa@test.iam.gserviceaccount.com'}):
            with patch.dict(os.environ):
                # The real .env provides a private key — neutralise it so only
                # the email is present.
                os.environ.pop('GOOGLE_MEETING_PRIVATE_KEY', None)
                with patch('apps.stakeholders.google_calendar._SA_FILE_CANDIDATES', []):
                    self.assertIsNone(_load_service_account_info())

    def test_load_service_account_info_from_json_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sa.json')
            with open(path, 'w') as fh:
                json.dump({'client_email': 'file-sa@test.iam.gserviceaccount.com', 'private_key': 'filekey'}, fh)
            with patch.dict(os.environ):
                for key in GOOGLE_MEETING_ENV_KEYS:
                    os.environ.pop(key, None)
                with patch('apps.stakeholders.google_calendar._SA_FILE_CANDIDATES', [path]):
                    info = _load_service_account_info()
        self.assertEqual(info['client_email'], 'file-sa@test.iam.gserviceaccount.com')
        self.assertEqual(info['private_key'], 'filekey')

    def test_load_service_account_info_skips_broken_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, 'broken.json')
            with open(broken, 'w') as fh:
                fh.write('not json at all')
            with patch.dict(os.environ):
                for key in GOOGLE_MEETING_ENV_KEYS:
                    os.environ.pop(key, None)
                with patch('apps.stakeholders.google_calendar._SA_FILE_CANDIDATES',
                           [os.path.join(tmp, 'missing.json'), broken]):
                    self.assertIsNone(_load_service_account_info())


class ParseDisplayDatetimeTests(TestCase):

    def test_human_date_and_range(self):
        start, end = parse_display_datetime('Aug 30, 2026', '10:00 AM - 11:30 AM')
        self.assertEqual(start, datetime.datetime(2026, 8, 30, 10, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(end, datetime.datetime(2026, 8, 30, 11, 30, tzinfo=datetime.timezone.utc))

    def test_iso_date_and_24h_single_time(self):
        start, end = parse_display_datetime('2026-08-30', '14:00')
        self.assertEqual(start.hour, 14)
        self.assertEqual(end - start, datetime.timedelta(minutes=90))

    def test_meridiem_inherited_from_first_token(self):
        start, end = parse_display_datetime('Oct 28, 2026', '10:00 AM - 11:30')
        self.assertEqual(start.hour, 10)
        self.assertEqual(end.hour, 11)
        self.assertEqual(end.minute, 30)
        self.assertEqual((end - start).total_seconds(), 90 * 60)

    def test_us_date_format(self):
        start, _ = parse_display_datetime('08/30/2026', '09:00 AM - 10:00 AM')
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 8)
        self.assertEqual(start.day, 30)

    def test_iso_datetime_string(self):
        start, _ = parse_display_datetime('2026-08-30T15:00:00Z', '03:00 PM - 04:00 PM')
        self.assertEqual(start.day, 30)

    def test_overnight_range_rolls_to_next_day(self):
        start, end = parse_display_datetime('Oct 28, 2026', '10:00 PM - 1:00 AM')
        self.assertEqual(start.hour, 22)
        self.assertEqual(end.hour, 1)
        self.assertEqual(end.date(), start.date() + datetime.timedelta(days=1))

    def test_unparseable_slot_falls_back_on_given_date(self):
        start, end = parse_display_datetime('Oct 28, 2026', 'sometime later')
        self.assertEqual(start.date(), datetime.date(2026, 10, 28))
        self.assertEqual(end - start, datetime.timedelta(minutes=90))

    def test_no_date_no_slot_falls_back_to_next_hour(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start, end = parse_display_datetime(None, None)
        self.assertGreaterEqual(start, now)
        self.assertEqual(end - start, datetime.timedelta(minutes=90))

    def test_custom_default_duration(self):
        start, end = parse_display_datetime('Oct 28, 2026', '09:00 - 10:00', default_duration_minutes=30)
        self.assertEqual(end - start, datetime.timedelta(minutes=60))


class ExtractAttendeeEmailsTests(TestCase):

    def test_none_and_empty(self):
        self.assertEqual(extract_attendee_emails(None), [])
        self.assertEqual(extract_attendee_emails([]), [])

    def test_single_dict(self):
        self.assertEqual(
            extract_attendee_emails({'email': 'a@example.com'}),
            ['a@example.com']
        )

    def test_filters_invalid_and_missing(self):
        participants = [
            {'email': 'valid@example.com'},
            {'email': 'not-an-email'},
            {'email': ''},
            {'name': 'No Email Field'},
            'just a string',
            {'email': 'Name <formatted@example.com>'},
        ]
        self.assertEqual(
            extract_attendee_emails(participants),
            ['valid@example.com', 'formatted@example.com']
        )

    def test_deduplicates_preserving_order(self):
        participants = [
            {'email': 'first@example.com'},
            {'email': 'second@example.com'},
            {'email': 'first@example.com'},
        ]
        self.assertEqual(
            extract_attendee_emails(participants),
            ['first@example.com', 'second@example.com']
        )


# ---------------------------------------------------------------------------
# google_calendar.py — service-account credentials
# ---------------------------------------------------------------------------

class GoogleMeetCredentialTests(TestCase):

    def setUp(self):
        GoogleMeetCalendarService._credentials = None

    def tearDown(self):
        GoogleMeetCalendarService._credentials = None

    def test_get_credentials_raises_honestly_when_unconfigured(self):
        with no_google_meeting_credentials():
            with self.assertRaises(GoogleMeetCalendarError) as ctx:
                GoogleMeetCalendarService.get_credentials()
        self.assertIn('not configured', str(ctx.exception))
        self.assertNotIn('meet.google.com', str(ctx.exception))

    def test_configured_false_when_unconfigured(self):
        with no_google_meeting_credentials():
            self.assertFalse(GoogleMeetCalendarService.configured())

    def test_configured_true_when_credentials_available(self):
        with patch.object(GoogleMeetCalendarService, 'get_credentials', return_value=MagicMock()):
            self.assertTrue(GoogleMeetCalendarService.configured())

    def test_get_credentials_builds_and_caches_credentials(self):
        fake_creds = MagicMock()
        fake_creds.valid = True
        with patch('google.oauth2.service_account.Credentials.from_service_account_info',
                   return_value=fake_creds) as factory:
            with patch('google.auth.transport.requests.Request'):
                first = GoogleMeetCalendarService.get_credentials()
                second = GoogleMeetCalendarService.get_credentials()
        self.assertIs(first, fake_creds)
        self.assertIs(second, fake_creds)
        factory.assert_called_once()
        self.assertIn(CALENDAR_SCOPE, factory.call_args.kwargs.get('scopes', []))
        self.assertIn(MEET_SCOPE, factory.call_args.kwargs.get('scopes', []))

    def test_get_credentials_auth_failure_is_honest(self):
        with patch.dict(os.environ, {
            'GOOGLE_MEETING_CLIENT_EMAIL': 'sa@test.iam.gserviceaccount.com',
            'GOOGLE_MEETING_PRIVATE_KEY': 'bad-key',
        }):
            with patch('google.oauth2.service_account.Credentials.from_service_account_info',
                       side_effect=Exception('invalid private key')):
                with self.assertRaises(GoogleMeetCalendarError) as ctx:
                    GoogleMeetCalendarService.get_credentials()
        self.assertIn('Google authentication failed', str(ctx.exception))
        self.assertIsNone(GoogleMeetCalendarService._credentials)

    def test_service_account_email(self):
        with patch('apps.stakeholders.google_calendar._load_service_account_info', return_value=None):
            self.assertEqual(GoogleMeetCalendarService.service_account_email(), '')
        with patch('apps.stakeholders.google_calendar._load_service_account_info',
                   return_value={'client_email': 'sa@test.iam.gserviceaccount.com'}):
            self.assertEqual(GoogleMeetCalendarService.service_account_email(),
                             'sa@test.iam.gserviceaccount.com')


# ---------------------------------------------------------------------------
# google_calendar.py — Calendar event lifecycle with a mocked googleapiclient
# ---------------------------------------------------------------------------

class FakeCalendarServiceFactory:
    """
    Builds a stand-in for the object googleapiclient.discovery.build() returns.
    `insert_handler(body_kwargs)` receives the kwargs of events().insert() and
    returns the API response dict (or raises to simulate API errors).
    """

    def __init__(self, insert_handler, get_responses=None):
        self.insert_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.get_responses = get_responses
        self.service = MagicMock()
        events = self.service.events.return_value

        def _insert(**kwargs):
            self.insert_calls.append(kwargs)
            response = insert_handler(kwargs)
            wrapper = MagicMock()
            wrapper.execute.return_value = response
            return wrapper

        events.insert.side_effect = _insert

        if isinstance(get_responses, list):
            wrappers = []
            for resp in get_responses:
                wrapper = MagicMock()
                wrapper.execute.return_value = resp
                wrappers.append(wrapper)
            events.get.side_effect = wrappers
        elif get_responses is not None:
            events.get.return_value.execute.return_value = get_responses

        def _update(**kwargs):
            self.update_calls.append(kwargs)
            wrapper = MagicMock()
            wrapper.execute.return_value = kwargs.get('body', {})
            return wrapper

        events.update.side_effect = _update

        def _delete(**kwargs):
            self.delete_calls.append(kwargs)
            return MagicMock()

        events.delete.side_effect = _delete


START = datetime.datetime(2026, 10, 28, 10, 0, tzinfo=datetime.timezone.utc)
END = datetime.datetime(2026, 10, 28, 11, 30, tzinfo=datetime.timezone.utc)


class GoogleMeetEventTests(TestCase):

    def setUp(self):
        GoogleMeetCalendarService._credentials = None

    def tearDown(self):
        GoogleMeetCalendarService._credentials = None

    def test_create_event_success_with_attendees_and_conference(self):
        factory = FakeCalendarServiceFactory(lambda kwargs: {
            'id': 'evt-1',
            'htmlLink': 'https://calendar.google.com/event?eid=evt1',
            'hangoutLink': 'https://meet.google.com/abc-def-ghi',
        })
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            result = GoogleMeetCalendarService.create_meeting_event(
                title='Council Session', agenda='Review slab', start=START, end=END,
                attendees=['dg@government.gov.ng', 'dev@example.com'],
                meeting_reference='MTG-TEST', project_name='Nexus Tower',
            )
        self.assertEqual(result['status'], 'created_with_meet')
        self.assertEqual(result['event_id'], 'evt-1')
        self.assertEqual(result['hangout_link'], 'https://meet.google.com/abc-def-ghi')
        self.assertEqual(result['meet_link_source'], 'google_calendar_conference')
        self.assertTrue(result['attendees_invited'])
        self.assertEqual(result['meet_error'], '')

        body = factory.insert_calls[0]['body']
        self.assertEqual(body['summary'], 'Council Session')
        self.assertIn('Review slab', body['description'])
        self.assertIn('Project: Nexus Tower', body['description'])
        self.assertIn('Reference: MTG-TEST', body['description'])
        self.assertEqual(body['attendees'],
                         [{'email': 'dg@government.gov.ng'}, {'email': 'dev@example.com'}])
        self.assertIn('conferenceData', body)
        self.assertEqual(factory.insert_calls[0]['sendUpdates'], 'all')
        self.assertEqual(factory.insert_calls[0]['conferenceDataVersion'], 1)
        self.assertEqual(body['start']['dateTime'], '2026-10-28T10:00:00Z')

    def test_create_event_falls_back_without_attendees_when_dwd_missing(self):
        def handler(kwargs):
            if kwargs['body'].get('attendees'):
                raise Exception('ForbiddenForServiceAccounts: attendees need domain-wide delegation')
            return {'id': 'evt-dwd', 'htmlLink': 'https://calendar.google.com/event?eid=dwd',
                    'hangoutLink': ''}

        factory = FakeCalendarServiceFactory(
            handler,
            get_responses={'hangoutLink': 'https://meet.google.com/dwd-mit',
                           'conferenceData': {'createRequest': {'status': {'statusCode': 'success'}}}},
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch('time.sleep'):
                result = GoogleMeetCalendarService.create_meeting_event(
                    title='Council', agenda='', start=START, end=END,
                    attendees=['dg@government.gov.ng'],
                )
        self.assertEqual(len(factory.insert_calls), 2)
        self.assertNotIn('attendees', factory.insert_calls[1]['body'])
        self.assertEqual(factory.insert_calls[1]['sendUpdates'], 'none')
        self.assertEqual(result['status'], 'created_with_meet')
        self.assertEqual(result['hangout_link'], 'https://meet.google.com/dwd-mit')
        self.assertFalse(result['attendees_invited'])

    def test_create_event_polls_for_late_hangout_link(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: {'id': 'evt-poll', 'htmlLink': 'https://calendar.google.com/event?eid=poll',
                            'hangoutLink': ''},
            get_responses=[
                {'hangoutLink': '', 'conferenceData': {'createRequest': {'status': {'statusCode': 'pending'}}}},
                {'hangoutLink': 'https://meet.google.com/late-link'},
            ],
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch('time.sleep'):
                result = GoogleMeetCalendarService.create_meeting_event(
                    title='Council', agenda='', start=START, end=END,
                )
        self.assertEqual(result['status'], 'created_with_meet')
        self.assertEqual(result['hangout_link'], 'https://meet.google.com/late-link')

    def test_create_event_uses_meet_api_as_last_resort(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: {'id': 'evt-api', 'htmlLink': 'https://calendar.google.com/event?eid=api',
                            'hangoutLink': ''},
            get_responses={'hangoutLink': '',
                           'conferenceData': {'createRequest': {'status': {'statusCode': 'failed'}}}},
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch('time.sleep'):
                with patch.object(GoogleMeetCalendarService, 'create_meet_space',
                                  return_value={'meeting_uri': 'https://meet.google.com/api-space',
                                                'meet_id': 'spaces/abc', 'error': ''}):
                    result = GoogleMeetCalendarService.create_meeting_event(
                        title='Council', agenda='', start=START, end=END,
                    )
        self.assertEqual(result['status'], 'created_with_meet')
        self.assertEqual(result['hangout_link'], 'https://meet.google.com/api-space')
        self.assertEqual(result['meet_link_source'], 'google_meet_api')
        self.assertEqual(result['meet_error'], '')

    def test_create_event_reports_honestly_when_no_meet_available(self):
        def handler(kwargs):
            if 'conferenceData' in kwargs['body']:
                raise Exception('Invalid conference type.')
            return {'id': 'evt-plain', 'htmlLink': 'https://calendar.google.com/event?eid=plain',
                    'hangoutLink': ''}

        factory = FakeCalendarServiceFactory(handler)
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch.object(GoogleMeetCalendarService, 'create_meet_space',
                              return_value={'meeting_uri': '', 'meet_id': '',
                                            'error': 'HTTP 403: Google Meet API is disabled'}):
                result = GoogleMeetCalendarService.create_meeting_event(
                    title='Council', agenda='', start=START, end=END,
                )
        self.assertEqual(result['status'], 'created')
        self.assertEqual(result['event_id'], 'evt-plain')
        # Honest failure: no fabricated meet link or event id.
        self.assertEqual(result['hangout_link'], '')
        self.assertIn('Invalid conference type', result['meet_error'])

    def test_create_event_total_calendar_failure_fabricates_nothing(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: (_ for _ in ()).throw(Exception('Backend Error 500')),
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch.object(GoogleMeetCalendarService, 'create_meet_space') as meet_api:
                result = GoogleMeetCalendarService.create_meeting_event(
                    title='Council', agenda='', start=START, end=END,
                    attendees=['dg@government.gov.ng'],
                )
        self.assertEqual(result['status'], '')
        self.assertEqual(result['event_id'], '')
        self.assertEqual(result['html_link'], '')
        self.assertEqual(result['hangout_link'], '')
        self.assertIn('Backend Error 500', result['calendar_error'])
        meet_api.assert_not_called()

    def test_create_event_without_meet_conference(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: {'id': 'evt-plain2', 'htmlLink': 'https://calendar.google.com/event?eid=p2'},
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            result = GoogleMeetCalendarService.create_meeting_event(
                title='Council', agenda='', start=START, end=END, add_meet_conference=False,
            )
        self.assertEqual(result['status'], 'created')
        self.assertEqual(result['event_id'], 'evt-plain2')
        self.assertEqual(result['hangout_link'], '')
        self.assertNotIn('conferenceData', factory.insert_calls[0]['body'])

    def test_delete_event(self):
        factory = FakeCalendarServiceFactory(lambda kwargs: {})
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            self.assertFalse(GoogleMeetCalendarService.delete_event(''))
            self.assertTrue(GoogleMeetCalendarService.delete_event('evt-1'))
        self.assertEqual(factory.delete_calls[0]['eventId'], 'evt-1')

        failing = FakeCalendarServiceFactory(lambda kwargs: {})
        failing.service.events.return_value.delete.side_effect = Exception('gone')
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=failing.service):
            self.assertFalse(GoogleMeetCalendarService.delete_event('evt-1'))

    def test_update_event(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: {},
            get_responses={'summary': 'Old title', 'description': 'Old agenda',
                           'start': {}, 'end': {}, 'attendees': []},
        )
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            self.assertFalse(GoogleMeetCalendarService.update_event(''))
            self.assertTrue(GoogleMeetCalendarService.update_event(
                'evt-9', title='New title', agenda='New agenda',
                start=START, end=END, attendees=['x@example.com'],
            ))
        body = factory.update_calls[0]['body']
        self.assertEqual(body['summary'], 'New title')
        self.assertEqual(body['description'], 'New agenda')
        self.assertEqual(body['attendees'], [{'email': 'x@example.com'}])
        self.assertEqual(body['start']['dateTime'], '2026-10-28T10:00:00Z')
        self.assertEqual(body['end']['dateTime'], '2026-10-28T11:30:00Z')

        failing = FakeCalendarServiceFactory(lambda kwargs: {})
        failing.service.events.return_value.get.side_effect = Exception('not found')
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=failing.service):
            self.assertFalse(GoogleMeetCalendarService.update_event('evt-9'))

    def test_create_meet_space_success(self):
        creds = MagicMock()
        creds.token = 'tok123'
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {'meetingUri': 'https://meet.google.com/space-x',
                                      'name': 'spaces/space-x'}
        with patch.object(GoogleMeetCalendarService, 'get_credentials', return_value=creds):
            with patch('requests.post', return_value=response) as post:
                result = GoogleMeetCalendarService.create_meet_space()
        self.assertEqual(result['meeting_uri'], 'https://meet.google.com/space-x')
        self.assertEqual(result['meet_id'], 'spaces/space-x')
        self.assertEqual(result['error'], '')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer tok123')
        self.assertEqual(post.call_args.kwargs['timeout'], 15)

    def test_create_meet_space_http_error(self):
        creds = MagicMock()
        creds.token = 'tok123'
        response = MagicMock()
        response.status_code = 403
        response.text = 'Google Meet API has not been used in project'
        with patch.object(GoogleMeetCalendarService, 'get_credentials', return_value=creds):
            with patch('requests.post', return_value=response):
                result = GoogleMeetCalendarService.create_meet_space()
        self.assertEqual(result['meeting_uri'], '')
        self.assertTrue(result['error'].startswith('HTTP 403'))

    def test_create_meet_space_unconfigured(self):
        with patch.object(GoogleMeetCalendarService, 'get_credentials',
                          side_effect=GoogleMeetCalendarError('not configured')):
            result = GoogleMeetCalendarService.create_meet_space()
        self.assertEqual(result['meeting_uri'], '')
        self.assertIn('not configured', result['error'])

    def test_create_meet_space_network_error(self):
        creds = MagicMock()
        creds.token = 'tok123'
        with patch.object(GoogleMeetCalendarService, 'get_credentials', return_value=creds):
            with patch('requests.post', side_effect=OSError('connection refused')):
                result = GoogleMeetCalendarService.create_meet_space()
        self.assertEqual(result['meeting_uri'], '')
        self.assertIn('connection refused', result['error'])


# ---------------------------------------------------------------------------
# services.py — meeting scheduling, notifications and messaging
# ---------------------------------------------------------------------------

class ScheduleMeetingServiceTests(BaseStakeholderServiceTestCase):

    def test_schedule_meeting_stores_meet_link_when_available(self):
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event',
                   return_value={**GOOGLE_EVENT_STUB, 'attendees_invited': True}):
            meeting = StakeholderService.schedule_meeting({
                'title': 'Design Council',
                'agenda': 'Sign-off',
                'date': 'Oct 28, 2026',
                'time_slot': '10:00 AM - 11:30 AM',
                'meeting_type': 'Video Call',
                'participants': [{'name': 'DG', 'email': 'dg@government.gov.ng'}],
            }, user=self.agency_head)
        self.assertEqual(meeting.meet_link_status, 'meet_available')
        self.assertEqual(meeting.google_meet_url, GOOGLE_EVENT_STUB['hangout_link'])
        self.assertEqual(meeting.google_calendar_event_id, 'stub-event-id')
        self.assertEqual(meeting.initiated_by, self.agency_head)
        self.assertEqual(meeting.initiator_role, 'Agency Head / Director General')
        self.assertTrue(
            Notification.objects.filter(recipient=self.agency_head,
                                        title='Official Stakeholder Meeting Scheduled').exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(action='STAKEHOLDER_MEETING_SCHEDULED',
                                      resource_id=str(meeting.id)).exists()
        )

    def test_schedule_meeting_calendar_only_notes_dwd(self):
        stub = {**GOOGLE_EVENT_STUB, 'hangout_link': '', 'status': 'created',
                'attendees_invited': False}
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event',
                   return_value=stub):
            meeting = StakeholderService.schedule_meeting({
                'title': 'Design Council',
                'agenda': 'Sign-off',
                'meeting_type': 'Video Call',
                'participants': [{'name': 'DG', 'email': 'dg@government.gov.ng'}],
            }, user=self.agency_head)
        self.assertEqual(meeting.meet_link_status, 'calendar_only')
        self.assertEqual(meeting.google_meet_url, '')
        self.assertEqual(meeting.google_calendar_event_id, 'stub-event-id')
        audit = AuditEvent.objects.filter(action='STAKEHOLDER_MEETING_SCHEDULED',
                                          resource_id=str(meeting.id)).first()
        self.assertIn('Domain-Wide Delegation', audit.new_state['meet_note'])

    def test_schedule_meeting_without_credentials_is_honestly_unavailable(self):
        with no_google_meeting_credentials():
            GoogleMeetCalendarService._credentials = None
            meeting = StakeholderService.schedule_meeting({
                'title': 'No-Creds Council',
                'agenda': 'Sign-off',
                'meeting_type': 'Video Call',
                'participants': [{'name': 'DG', 'email': 'dg@government.gov.ng'}],
            }, user=self.agency_head)
        self.assertEqual(meeting.meet_link_status, 'unavailable')
        self.assertEqual(meeting.google_meet_url, '')
        self.assertEqual(meeting.google_calendar_event_id, '')
        self.assertEqual(meeting.google_calendar_link, '')
        audit = AuditEvent.objects.filter(action='STAKEHOLDER_MEETING_SCHEDULED',
                                          resource_id=str(meeting.id)).first()
        self.assertIn('not configured', audit.new_state['meet_note'])
        # Nothing anywhere pretends a Meet link or Calendar event exists.
        self.assertNotIn('meet.google.com', json.dumps(audit.new_state))

    def test_schedule_meeting_in_person_skips_google(self):
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event') as create:
            meeting = StakeholderService.schedule_meeting({
                'title': 'Physical Council',
                'agenda': 'Site walk',
                'meeting_type': 'In-Person Council',
            }, user=self.agency_head)
        create.assert_not_called()
        self.assertEqual(meeting.meet_link_status, '')
        self.assertEqual(meeting.google_meet_url, '')

    def test_schedule_meeting_bypass_flag_allows_officer(self):
        meeting = StakeholderService.schedule_meeting({
            'title': 'Delegated Meeting',
            'agenda': 'Follow-up',
            'meeting_type': 'In-Person Council',
            'bypass_agency_head_check': True,
            'initiator_name': 'Officer Delegate',
        }, user=self.regular_officer)
        self.assertEqual(meeting.initiated_by, self.regular_officer)
        self.assertEqual(meeting.initiator_name, 'Officer Delegate')

    def test_schedule_meeting_defaults_participants_from_initiator(self):
        meeting = StakeholderService.schedule_meeting({
            'title': 'Defaults',
            'agenda': 'A',
            'meeting_type': 'In-Person Council',
        }, user=self.agency_head)
        self.assertEqual(meeting.initiator_name, 'Amina Bello')
        self.assertEqual(meeting.participants[0]['name'], 'Amina Bello')
        self.assertEqual(meeting.participants[0]['status'], 'Confirmed')

    def test_schedule_meeting_send_invite_emails_flag(self):
        with patch.object(StakeholderService, 'send_meeting_invitations',
                          return_value={'sent': 0}) as invites:
            StakeholderService.schedule_meeting({
                'title': 'Invited',
                'agenda': 'A',
                'meeting_type': 'In-Person Council',
                'send_invite_emails': True,
            }, user=self.agency_head)
        invites.assert_called_once()

    def test_schedule_meeting_api_level_honest_unavailable(self):
        self.client.force_authenticate(user=self.agency_head)
        with no_google_meeting_credentials():
            GoogleMeetCalendarService._credentials = None
            res = self.client.post('/api/v1/stakeholders/meetings/', {
                "title": "No Creds",
                "agenda": "A",
                "meeting_type": "Video Call",
            })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['meet_link_status'], 'unavailable')
        self.assertEqual(res.data['google_meet_url'], '')
        self.assertEqual(res.data['google_calendar_event_id'], '')

    def test_schedule_meeting_api_level_officer_forbidden(self):
        self.client.force_authenticate(user=self.regular_officer)
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event') as create:
            res = self.client.post('/api/v1/stakeholders/meetings/', {
                "title": "Unauthorized",
                "agenda": "A",
                "meeting_type": "Video Call",
            })
        self.assertEqual(res.status_code, 403)
        create.assert_not_called()


class MeetingServiceTests(BaseStakeholderServiceTestCase):

    def _meeting(self, **overrides):
        defaults = dict(title='Council', agenda='A', status='Scheduled',
                        google_meet_url='https://meet.google.com/xyz',
                        google_calendar_link='https://calendar.google.com/event?eid=1')
        defaults.update(overrides)
        return StakeholderMeeting.objects.create(**defaults)

    def test_start_meeting_not_found(self):
        with self.assertRaises(ValueError):
            StakeholderService.start_meeting('does-not-exist', self.agency_head)

    def test_start_meeting_payload(self):
        meeting = self._meeting()
        payload = StakeholderService.start_meeting(meeting.id, self.agency_head)
        self.assertEqual(payload['status'], 'In Progress')
        self.assertEqual(payload['room_id'], meeting.room_id)
        self.assertEqual(payload['google_meet_url'], 'https://meet.google.com/xyz')
        self.assertTrue(payload['call_url'].endswith(
            f'/government/dashboard/stakeholders/meetings/{meeting.id}/room'))
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, 'In Progress')
        self.assertTrue(AuditEvent.objects.filter(action='CALL_ROOM_LAUNCHED',
                                                  resource_id=str(meeting.id)).exists())

    def test_send_meeting_invitations_no_valid_emails(self):
        meeting = self._meeting(participants=[{'name': 'No Email'}])
        result = StakeholderService.send_meeting_invitations(meeting, self.agency_head)
        self.assertEqual(result['sent'], 0)
        self.assertIn('No participants with valid email', result['error'])

    def test_send_meeting_invitations_success(self):
        meeting = self._meeting(participants=[
            {'name': 'DG', 'email': 'dg@government.gov.ng'},
            {'name': 'Dev', 'email': 'dev@example.com'},
        ])
        captured = []

        def fake_send(to_email, subject, html_content, text_content=None, from_email=None):
            captured.append({'to': to_email, 'subject': subject,
                             'html': html_content, 'text': text_content})
            return {'success': True, 'id': f'resend-{len(captured)}'}

        with patch('apps.notifications.email_service.EmailService.send_email',
                   side_effect=fake_send):
            result = StakeholderService.send_meeting_invitations(meeting, self.agency_head)

        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['total'], 2)
        self.assertEqual([c['to'] for c in captured],
                         ['dg@government.gov.ng', 'dev@example.com'])
        self.assertIn(meeting.title, captured[0]['subject'])
        # Both the real Meet link and the Calendar link appear in the invite.
        self.assertIn('https://meet.google.com/xyz', captured[0]['html'])
        self.assertIn('https://calendar.google.com/event?eid=1', captured[0]['html'])
        self.assertIn('https://meet.google.com/xyz', captured[0]['text'])
        self.assertTrue(AuditEvent.objects.filter(action='MEETING_INVITATIONS_SENT',
                                                  resource_id=str(meeting.id)).exists())

    def test_send_meeting_invitations_partial_failure(self):
        meeting = self._meeting(participants=[
            {'name': 'A', 'email': 'a@example.com'},
            {'name': 'B', 'email': 'b@example.com'},
        ])
        responses = [
            {'success': True, 'id': 'ok-1'},
            {'success': False, 'error': 'Resend rejected'},
        ]
        with patch('apps.notifications.email_service.EmailService.send_email',
                   side_effect=responses):
            result = StakeholderService.send_meeting_invitations(meeting, self.agency_head)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['total'], 2)
        self.assertFalse(result['results'][1]['success'])
        self.assertEqual(result['results'][1]['error'], 'Resend rejected')

    def test_get_meeting_instance_resolvers(self):
        first = self._meeting(title='Alpha Council')
        second = self._meeting(title='Beta Council')

        self.assertEqual(StakeholderService.get_meeting_instance(first.id).id, first.id)
        self.assertEqual(StakeholderService.get_meeting_instance(
            second.meeting_reference).id, second.id)
        self.assertEqual(StakeholderService.get_meeting_instance(
            second.room_id.upper()).id, second.id)  # room lookup is case-insensitive
        self.assertEqual(StakeholderService.get_meeting_instance('Beta').id, second.id)
        self.assertEqual(StakeholderService.get_meeting_instance('room').id, second.id)
        self.assertEqual(StakeholderService.get_meeting_instance('').id, second.id)

    def test_join_meeting_appends_and_updates_participants(self):
        meeting = self._meeting(participants=[
            {'name': 'DG', 'role': 'Agency Head', 'email': 'dg@government.gov.ng',
             'status': 'Confirmed'},
        ])
        StakeholderService.join_meeting(meeting.id, {
            'name': 'DG', 'role': 'Agency Head', 'email': 'dg@government.gov.ng',
        }, user=self.agency_head)
        meeting.refresh_from_db()
        self.assertEqual(len(meeting.participants), 1)
        self.assertEqual(meeting.participants[0]['status'], 'Live In Room')
        self.assertEqual(meeting.status, 'In Progress')

        StakeholderService.join_meeting(meeting.id, {'name': 'New Guest'}, user=None)
        meeting.refresh_from_db()
        self.assertEqual(len(meeting.participants), 2)
        self.assertEqual(meeting.participants[1]['name'], 'New Guest')
        self.assertEqual(meeting.participants[1]['status'], 'Live In Room')
        self.assertTrue(AuditEvent.objects.filter(action='PARTICIPANT_JOINED_MEETING',
                                                  resource_id=str(meeting.id)).exists())

    def test_update_meeting_notes(self):
        meeting = self._meeting()
        updated = StakeholderService.update_meeting_notes(meeting.id, 'Minute 1: approved',
                                                          self.agency_head)
        self.assertEqual(updated.minutes_notes, 'Minute 1: approved')
        meeting.refresh_from_db()
        self.assertEqual(meeting.minutes_notes, 'Minute 1: approved')
        self.assertTrue(AuditEvent.objects.filter(action='MEETING_MINUTES_UPDATED',
                                                  resource_id=str(meeting.id)).exists())

    def test_cast_meeting_vote(self):
        meeting = self._meeting()
        result = StakeholderService.cast_meeting_vote(meeting.id, 'Director Ade', 'Director',
                                                      'YES', 'Stage 2 signoff',
                                                      user=self.agency_head)
        self.assertEqual(result['vote'], 'YES')
        self.assertEqual(result['status'], 'Recorded')
        self.assertTrue(AuditEvent.objects.filter(
            action='MEETING_QUORUM_VOTE_CAST', resource_id=str(meeting.id),
            new_state__resolution='Stage 2 signoff').exists())

    def test_add_meeting_action_item_and_legacy_alias(self):
        meeting = self._meeting()
        item = StakeholderService.add_meeting_action_item(
            meeting.id, 'Submit samples', 'GeoTech Lab', 'Within 48 Hours',
            user=self.agency_head)
        self.assertEqual(item.meeting_id, meeting.id)
        self.assertEqual(item.title, 'Submit samples')
        self.assertEqual(item.assignee_name, 'GeoTech Lab')

        legacy = StakeholderService.add_meeting_actionItem(meeting.id, 'Legacy item')
        self.assertEqual(legacy.title, 'Legacy item')
        self.assertEqual(legacy.assignee_name, 'Project Lead')
        self.assertTrue(AuditEvent.objects.filter(action='MEETING_ACTION_ITEM_CREATED',
                                                  resource_id=str(item.id)).exists())

    def test_action_item_not_found(self):
        with self.assertRaises(ValueError):
            StakeholderService.add_meeting_action_item('no-such-meeting', 'Title')


class UploadToR2Tests(BaseStakeholderServiceTestCase):

    def test_passthrough_for_http_urls(self):
        url = 'https://example.com/files/report.pdf'
        with patch('apps.documents.services.DocumentStorageService.get_s3_client') as client:
            self.assertEqual(StakeholderService.upload_to_cloudflare_r2(url, 'report.pdf'), url)
            client.assert_not_called()

    def test_none_payload(self):
        self.assertIsNone(StakeholderService.upload_to_cloudflare_r2(None, 'x.pdf'))

    def test_data_uri_uploaded_to_r2(self):
        from apps.documents.services import R2_ENDPOINT_URL, R2_BUCKET_NAME
        client = MagicMock()
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   return_value=client):
            url = StakeholderService.upload_to_cloudflare_r2(
                'data:application/pdf;base64,SGVsbG8gV29ybGQ=', 'site report.pdf',
                folder_prefix='messages/attachments')
        self.assertTrue(url.startswith(f'{R2_ENDPOINT_URL}/{R2_BUCKET_NAME}/messages/attachments/'))
        self.assertIn('site_report.pdf', url)
        put_kwargs = client.put_object.call_args.kwargs
        self.assertEqual(put_kwargs['Bucket'], R2_BUCKET_NAME)
        self.assertEqual(put_kwargs['Body'], b'Hello World')
        self.assertEqual(put_kwargs['ContentType'], 'application/pdf')

    def test_bytes_payload_uploaded_to_r2(self):
        client = MagicMock()
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   return_value=client):
            url = StakeholderService.upload_to_cloudflare_r2(b'\x00\x01binary', 'scan.bin')
        self.assertIn('scan.bin', url)
        client.put_object.assert_called_once()

    def test_upload_failure_degrades_without_raising(self):
        client = MagicMock()
        client.put_object.side_effect = Exception('R2 unavailable')
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   return_value=client):
            url = StakeholderService.upload_to_cloudflare_r2(
                'data:image/png;base64,aGVsbG8=', 'img.png')
        # Degraded but honest: the R2 URL is still returned (upload logged a notice).
        self.assertIn('img.png', url)

    def test_unrecognised_string_payload_returned_unchanged(self):
        self.assertEqual(StakeholderService.upload_to_cloudflare_r2('raw-bytes', 'x.bin'),
                         'raw-bytes')


class SendMessageServiceTests(BaseStakeholderServiceTestCase):

    def test_send_message_creates_row_and_audit(self):
        msg = StakeholderService.send_message({
            'channel_name': 'General Council',
            'project_name': 'Nexus Tower',
            'message_text': 'Please submit revised drawings.',
        }, user=self.agency_head)
        self.assertEqual(msg.sender, self.agency_head)
        self.assertEqual(msg.sender_name, 'Amina Bello')
        self.assertEqual(msg.channel_name, 'General Council')
        self.assertFalse(msg.is_urgent)
        self.assertTrue(AuditEvent.objects.filter(action='STAKEHOLDER_MESSAGE_SENT',
                                                  resource_id=str(msg.id)).exists())
        # Non-urgent messages must not create notifications.
        self.assertFalse(Notification.objects.filter(
            recipient=self.agency_head, category='URGENT_MESSAGE').exists())

    def test_send_message_urgent_creates_critical_notification(self):
        msg = StakeholderService.send_message({
            'channel_name': 'Site Safety & Inspections',
            'message_text': 'URGENT: crane instability on level 12',
            'is_urgent': True,
        }, user=self.agency_head)
        self.assertTrue(msg.is_urgent)
        notification = Notification.objects.get(recipient=self.agency_head,
                                                category='URGENT_MESSAGE')
        self.assertEqual(notification.severity, 'Critical')
        self.assertIn('crane instability', notification.message)

    def test_send_message_urgent_voice_note_preview(self):
        msg = StakeholderService.send_message({
            'channel_name': 'General Council',
            'message_text': '',
            'voice_note_url': 'https://r2.example.com/voice.webm',
            'is_urgent': True,
        }, user=self.agency_head)
        notification = Notification.objects.get(recipient=self.agency_head,
                                                category='URGENT_MESSAGE')
        self.assertIn('[Voice Note]', notification.message)

    def test_send_message_http_attachment_passthrough(self):
        msg = StakeholderService.send_message({
            'channel_name': 'General Council',
            'message_text': 'See attached.',
            'attachment_url': 'https://example.com/files/soil-tests.pdf',
            'attachment_name': 'soil-tests.pdf',
        }, user=self.agency_head)
        self.assertEqual(msg.attachment_url, 'https://example.com/files/soil-tests.pdf')
        self.assertEqual(msg.attachment_name, 'soil-tests.pdf')

    def test_send_message_upload_attachment_to_r2(self):
        client = MagicMock()
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   return_value=client):
            msg = StakeholderService.send_message({
                'channel_name': 'General Council',
                'message_text': 'Drawing attached',
                'attachment_url': 'data:application/pdf;base64,aGVsbG8=',
                'attachment_name': 'rev 3 drawings.pdf',
                'voice_note_url': 'data:audio/webm;base64,aGVsbG8=',
                'voice_note_duration': '42',
            }, user=self.agency_head)
        self.assertTrue(msg.attachment_url.endswith('rev_3_drawings.pdf'))
        self.assertTrue(msg.voice_note_url.endswith('.webm'))
        self.assertEqual(msg.voice_note_duration, 42)
        self.assertEqual(client.put_object.call_count, 2)

    def test_send_message_anonymous_sender_fallback(self):
        msg = StakeholderService.send_message({
            'sender_name': 'Lead Consultant',
            'sender_role': 'MEP Consultant',
            'channel_name': 'General Council',
            'message_text': 'Update from the consultant.',
        }, user=None)
        self.assertIsNone(msg.sender)
        self.assertEqual(msg.sender_name, 'Lead Consultant')
        self.assertEqual(msg.sender_role, 'MEP Consultant')


class MiscellaneousServiceTests(BaseStakeholderServiceTestCase):

    def test_is_agency_head_matrix(self):
        self.assertTrue(StakeholderService.is_agency_head(None))  # anonymous walk-in flow
        self.assertTrue(StakeholderService.is_agency_head(
            SimpleNamespace(is_authenticated=False)))
        self.assertFalse(StakeholderService.is_agency_head(self.regular_officer))
        self.assertTrue(StakeholderService.is_agency_head(self.agency_head))
        officer = User.objects.create_user(username='staffer', email='staff@government.gov.ng',
                                           password='Password123!')
        officer.is_staff = True
        self.assertTrue(StakeholderService.is_agency_head(officer))
        self.assertTrue(StakeholderService.is_agency_head(
            SimpleNamespace(is_authenticated=True, is_superuser=False, is_staff=False,
                            role_name='Agency Head')))
        self.assertTrue(StakeholderService.is_agency_head(
            SimpleNamespace(is_authenticated=True, is_superuser=False, is_staff=False,
                            role_name='Director General')))
        self.assertFalse(StakeholderService.is_agency_head(
            SimpleNamespace(is_authenticated=True, is_superuser=False, is_staff=False,
                            role_name='Field Officer')))

    def test_toggle_blacklist_syncs_linked_contractor(self):
        contractor = Contractor.objects.create(
            contractor_id='CON-BL1', name='Shaky Foundations Ltd')
        rec = StakeholderService.toggle_blacklist(
            'Contractor', 'CON-BL1', 'Shaky Foundations Ltd',
            'Repeated trench collapse incidents', user=self.agency_head)
        self.assertEqual(rec.status, 'Blacklisted')
        contractor.refresh_from_db()
        self.assertTrue(contractor.is_blacklisted)
        self.assertTrue(AuditEvent.objects.filter(action='STAKEHOLDER_BLACKLISTED',
                                                  resource_id=str(rec.id)).exists())

        # Lifting the sanction clears the flag and updates the same record.
        lifted = StakeholderService.toggle_blacklist(
            'Contractor', 'CON-BL1', 'Shaky Foundations Ltd',
            'Remediation complete', status='Cleared', user=self.agency_head)
        self.assertEqual(lifted.id, rec.id)
        contractor.refresh_from_db()
        self.assertFalse(contractor.is_blacklisted)

    def test_toggle_blacklist_syncs_linked_developer(self):
        developer = Developer.objects.create(name='Delaying Developments Ltd')
        StakeholderService.toggle_blacklist(
            'Developer', developer.developer_id, 'Delaying Developments Ltd',
            'Abandoned project site', user=self.agency_head)
        developer.refresh_from_db()
        self.assertTrue(developer.is_blacklisted)

    def test_get_stakeholder_stats_counts(self):
        Inspector.objects.create(inspector_id='INS-A', name='Active Inspector',
                                 is_active=True)
        Inspector.objects.create(inspector_id='INS-B', name='Retired Inspector',
                                 is_active=False)
        Developer.objects.create(name='Dev One')
        Contractor.objects.create(contractor_id='CON-1', name='Con One')
        StakeholderMeeting.objects.create(title='Upcoming', agenda='A', status='Scheduled')
        StakeholderMeeting.objects.create(title='Done', agenda='A', status='Completed')

        stats = StakeholderService.get_stakeholder_stats()
        self.assertEqual(stats['active_inspectors'], 1)
        self.assertEqual(stats['total_contractors'], 1)
        self.assertEqual(stats['active_developers'], 1)
        self.assertEqual(stats['scheduled_meetings'], 1)
        # No assessed inspections yet -> no fabricated pass rate.
        self.assertIsNone(stats['global_pass_rate'])

    def test_verify_professional_license(self):
        prof = LicensedProfessional.objects.create(
            name='Engr. Ngozi Eze', role_title='Structural Engineer',
            firm_name='Eze Consulting', license_authority='COREN')
        updated = StakeholderService.verify_professional_license(prof.id, self.agency_head)
        self.assertTrue(updated.is_verified)
        self.assertEqual(updated.license_status, 'Valid (Verified)')
        self.assertTrue(AuditEvent.objects.filter(
            action='PROFESSIONAL_LICENSE_VERIFIED', resource_id=str(prof.id)).exists())

    def test_add_and_remove_team_member(self):
        team = ProjectStakeholderTeam.objects.create(
            project_reference='PRJ-T1', project_name='Tower One', team_data={})
        StakeholderService.add_team_member(team.id, 'architect',
                                           {'name': 'Arc. Ada'}, user=self.agency_head)
        team.refresh_from_db()
        self.assertEqual(team.team_data['architect']['name'], 'Arc. Ada')
        StakeholderService.remove_team_member(team.id, 'architect', user=self.agency_head)
        team.refresh_from_db()
        self.assertEqual(team.team_data, {})
        # Removing a role that does not exist is a no-op, not a crash.
        StakeholderService.remove_team_member(team.id, 'missing_role', user=self.agency_head)
        team.refresh_from_db()
        self.assertEqual(team.team_data, {})

    def test_send_notification_anonymous_user(self):
        StakeholderService.send_notification(None, 'Test Title', 'Test message',
                                             category='STAKEHOLDERS')
        self.assertTrue(Notification.objects.filter(title='Test Title').exists())


# ---------------------------------------------------------------------------
# translation.py — credential loading, caching and language handling
# ---------------------------------------------------------------------------

class TranslationServiceExtraTests(TestCase):

    def setUp(self):
        TranslationService._cached_credentials = None

    def tearDown(self):
        TranslationService._cached_credentials = None

    def test_unsupported_language_rejected(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Hello')
        with self.assertRaises(ValueError):
            TranslationService.translate_message(msg.id, 'fr')

    def test_translate_to_english_returns_original(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Original English text')
        result = TranslationService.translate_message(msg.id, 'en')
        self.assertTrue(result['is_cached'])
        self.assertEqual(result['translated_content'], 'Original English text')
        self.assertEqual(result['provider'], 'Original Source')
        # English is never cached as a translation row.
        self.assertFalse(MessageTranslation.objects.filter(message=msg).exists())

    def test_dictionary_exact_match_preserves_technical_terms(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Structural non-conformance detected on Grid 4.')
        with patch.object(TranslationService, 'get_google_credentials', return_value=None):
            result = TranslationService.translate_message(msg.id, 'yo')
        self.assertEqual(result['translated_content'],
                         'A rí àṣìṣe ìdúróṣinṣin lórí ìlà kẹrin (Grid 4)')
        self.assertIn('Neural Yorùbá Engine', result['provider'])

    def test_neural_fallback_prefix_for_unknown_text(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Brand new coordination bulletin')
        with patch.object(TranslationService, 'get_google_credentials', return_value=None):
            for lang, prefix in (('yo', 'Ìtumọ̀ Yorùbá:'), ('ig', 'Ntụgharị Igbo:'),
                                 ('ha', 'Fassarar Hausa:')):
                result = TranslationService.translate_message(msg.id, lang)
                self.assertTrue(result['translated_content'].startswith(prefix))
                self.assertFalse(result['is_cached'])

    def test_cached_translation_served_from_db(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Cache me')
        MessageTranslation.objects.create(
            message=msg, target_language='yo',
            translated_content='Ṣàtúntò mi', provider='Test Provider')
        result = TranslationService.translate_message(msg.id, 'yo')
        self.assertTrue(result['is_cached'])
        self.assertEqual(result['translated_content'], 'Ṣàtúntò mi')
        self.assertEqual(result['provider'], 'Test Provider')

    def test_translation_audit_event_created(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Audit this translation')
        with patch.object(TranslationService, 'get_google_credentials', return_value=None):
            TranslationService.translate_message(msg.id, 'ig', user=None)
        self.assertTrue(AuditEvent.objects.filter(action='MESSAGE_TRANSLATED',
                                                  resource_id=str(msg.id)).exists())

    def test_get_google_credentials_no_file_configured(self):
        with override_settings(GOOGLE_SERVICE_ACCOUNT_FILE='Z:/nonexistent/sa.json'):
            with patch.dict(os.environ):
                os.environ.pop('GOOGLE_APPLICATION_CREDENTIALS', None)
                self.assertIsNone(TranslationService.get_google_credentials())

    def test_get_google_credentials_loads_key_file(self):
        fake_creds = MagicMock()
        fake_creds.valid = True
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'translation-sa.json')
            with open(path, 'w') as fh:
                json.dump({'client_email': 'nexucon-language@test.iam.gserviceaccount.com',
                           'private_key': 'key'}, fh)
            with override_settings(GOOGLE_SERVICE_ACCOUNT_FILE=path):
                with patch('google.oauth2.service_account.Credentials.from_service_account_file',
                           return_value=fake_creds) as factory:
                    with patch('google.auth.transport.requests.Request'):
                        creds = TranslationService.get_google_credentials()
        self.assertIs(creds, fake_creds)
        self.assertIn('https://www.googleapis.com/auth/cloud-translation',
                      factory.call_args.kwargs.get('scopes', []))

    def test_get_google_credentials_bad_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad-sa.json')
            with open(path, 'w') as fh:
                fh.write('garbage')
            with override_settings(GOOGLE_SERVICE_ACCOUNT_FILE=path):
                self.assertIsNone(TranslationService.get_google_credentials())

    def test_translate_api_unsupported_language_400(self):
        self.client = APIClient()
        user = User.objects.create_superuser(username='translator', email='t@gov.ng',
                                             password='Password123!')
        self.client.force_authenticate(user=user)
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Hello')
        res = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/',
                               {"target_language": "fr"})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Unsupported language', res.data['error'])

    def test_translate_api_english_returns_original(self):
        self.client = APIClient()
        user = User.objects.create_superuser(username='translator2', email='t2@gov.ng',
                                             password='Password123!')
        self.client.force_authenticate(user=user)
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Plain English bulletin')
        res = self.client.post(f'/api/v1/stakeholders/messages/{msg.id}/translate/',
                               {"target_language": "en"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['translated_content'], 'Plain English bulletin')


# ---------------------------------------------------------------------------
# views.py — auth, CRUD, validation, 404s and action endpoints
# ---------------------------------------------------------------------------

class ViewAuthTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_unauthenticated_requests_rejected(self):
        endpoints = [
            '/api/v1/stakeholders/developers/',
            '/api/v1/stakeholders/contractors/',
            '/api/v1/stakeholders/consultants/',
            '/api/v1/stakeholders/inspectors/',
            '/api/v1/stakeholders/professionals/',
            '/api/v1/stakeholders/teams/',
            '/api/v1/stakeholders/blacklist/',
            '/api/v1/stakeholders/meetings/',
            '/api/v1/stakeholders/messages/',
            '/api/v1/stakeholders/certifications/',
            '/api/v1/stakeholders/trainings/',
            '/api/v1/stakeholders/stats/',
        ]
        for url in endpoints:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 401, f'{url} should require authentication')

    def test_unauthenticated_write_rejected(self):
        res = self.client.post('/api/v1/stakeholders/messages/', {'message_text': 'x'})
        self.assertEqual(res.status_code, 401)


class StakeholderCrudViewTests(BaseStakeholderServiceTestCase):

    def test_developer_full_crud_and_search(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/developers/', {
            'name': 'Lekki Gardens', 'hq_location': 'Lagos', 'status': 'Active'})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['developer_id'].startswith('DEV-'))
        dev_id = res.data['id']

        Developer.objects.create(name='Eko Atlantic Group', hq_location='Lagos')
        res = self.client.get('/api/v1/stakeholders/developers/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data['results'] if isinstance(res.data, dict) else res.data),
                         2)
        res = self.client.get('/api/v1/stakeholders/developers/', {'search': 'Lekki'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Lekki Gardens')

        res = self.client.patch(f'/api/v1/stakeholders/developers/{dev_id}/',
                                {'status': 'Blacklisted'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Blacklisted')

        res = self.client.delete(f'/api/v1/stakeholders/developers/{dev_id}/')
        self.assertEqual(res.status_code, 204)
        res = self.client.get(f'/api/v1/stakeholders/developers/{dev_id}/')
        self.assertEqual(res.status_code, 404)

    def test_developer_validation_error(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/developers/', {})
        self.assertEqual(res.status_code, 400)
        errors = res.data.get('errors', res.data)
        self.assertIn('name', errors)

    def test_contractor_search(self):
        self.client.force_authenticate(user=self.agency_head)
        Contractor.objects.create(contractor_id='CON-S1', name='Skyline Civils',
                                  contractor_type='Civil Works')
        Contractor.objects.create(contractor_id='CON-S2', name='Deep Foundations',
                                  contractor_type='Piling Specialist')
        res = self.client.get('/api/v1/stakeholders/contractors/',
                              {'search': 'Piling'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Deep Foundations')

    def test_consultant_specialty_filter(self):
        self.client.force_authenticate(user=self.agency_head)
        Consultant.objects.create(name='Geo Advisory', specialty='Geotechnical')
        Consultant.objects.create(name='M&E Partners', specialty='Mechanical & Electrical')
        res = self.client.get('/api/v1/stakeholders/consultants/',
                              {'specialty': 'geotechnical'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        res = self.client.get('/api/v1/stakeholders/consultants/', {'specialty': 'ALL'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 2)

    def test_inspector_zone_filter(self):
        self.client.force_authenticate(user=self.agency_head)
        Inspector.objects.create(inspector_id='INS-Z1', name='North Officer',
                                 assigned_zone='Zone C')
        Inspector.objects.create(inspector_id='INS-Z2', name='South Officer',
                                 assigned_zone='Zone A')
        res = self.client.get('/api/v1/stakeholders/inspectors/', {'zone': 'zone c'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'North Officer')

    def test_professional_authority_filter(self):
        self.client.force_authenticate(user=self.agency_head)
        LicensedProfessional.objects.create(name='Arc. Kemi', role_title='Architect',
                                            firm_name='Forma', license_authority='ARCON')
        LicensedProfessional.objects.create(name='Engr. Tunde', role_title='Engineer',
                                            firm_name='Struco', license_authority='COREN')
        res = self.client.get('/api/v1/stakeholders/professionals/',
                              {'authority': 'COREN'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Engr. Tunde')

    def test_professional_verify_license_unknown_404(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post(f'/api/v1/stakeholders/professionals/{uuid.uuid4()}/verify-license/')
        self.assertEqual(res.status_code, 404)

    def test_team_actions_validation_and_404(self):
        self.client.force_authenticate(user=self.agency_head)
        team = ProjectStakeholderTeam.objects.create(project_reference='PRJ-V',
                                                     project_name='View Tower',
                                                     team_data={})
        res = self.client.post(f'/api/v1/stakeholders/teams/{team.id}/add-member/',
                                {'role_key': 'engineer'})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(f'/api/v1/stakeholders/teams/{team.id}/remove-member/', {})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(f'/api/v1/stakeholders/teams/{uuid.uuid4()}/add-member/',
                               {'role_key': 'engineer', 'member_data': {'name': 'X'}},
                               format='json')
        self.assertEqual(res.status_code, 404)
        res = self.client.post(f'/api/v1/stakeholders/teams/{uuid.uuid4()}/remove-member/',
                               {'role_key': 'engineer'})
        self.assertEqual(res.status_code, 404)

    def test_blacklist_toggle_lifting(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/blacklist/toggle/', {
            'entity_type': 'Contractor',
            'entity_id': 'CON-V1',
            'entity_name': 'View Test Contractor',
            'reason': 'Test reason',
            'status': 'Cleared',
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Cleared')

    def test_stats_endpoint(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.get('/api/v1/stakeholders/stats/')
        self.assertEqual(res.status_code, 200)
        for key in ('active_inspectors', 'total_contractors', 'active_developers',
                    'scheduled_meetings', 'pending_inspections', 'global_pass_rate',
                    'total_ncrs_issued'):
            self.assertIn(key, res.data)


class MeetingViewTests(BaseStakeholderServiceTestCase):

    def test_meeting_validation_errors(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/meetings/', {})
        self.assertEqual(res.status_code, 400)
        errors = res.data.get('errors', res.data)
        self.assertIn('agenda', errors)
        self.assertIn('title', errors)

        res = self.client.post('/api/v1/stakeholders/meetings/', {
            'title': 'Bad type', 'agenda': 'A', 'meeting_type': 'Hologram Call'})
        self.assertEqual(res.status_code, 400)

    def test_meeting_list_retrieve_update_delete(self):
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title='Lifecycle Council', agenda='A', status='Scheduled',
            meeting_type='In-Person Council')

        res = self.client.get('/api/v1/stakeholders/meetings/')
        self.assertEqual(res.status_code, 200)

        # Detail lookup by meeting reference (custom resolver).
        res = self.client.get(f'/api/v1/stakeholders/meetings/{meeting.meeting_reference}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['title'], 'Lifecycle Council')

        # Detail lookup by room id.
        res = self.client.get(f'/api/v1/stakeholders/meetings/{meeting.room_id}/')
        self.assertEqual(res.status_code, 200)

        res = self.client.patch(f'/api/v1/stakeholders/meetings/{meeting.id}/',
                                {'status': 'Completed'})
        self.assertEqual(res.status_code, 200)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, 'Completed')

        res = self.client.delete(f'/api/v1/stakeholders/meetings/{meeting.id}/')
        self.assertEqual(res.status_code, 204)
        self.assertFalse(StakeholderMeeting.objects.filter(id=meeting.id).exists())

    def test_meeting_start_unknown_404(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/meetings/no-such-ref/start/')
        self.assertEqual(res.status_code, 404)

    def test_meeting_actions_unknown_404(self):
        self.client.force_authenticate(user=self.agency_head)
        base = '/api/v1/stakeholders/meetings/no-such-ref'
        self.assertEqual(self.client.post(f'{base}/join/', {'name': 'X'}).status_code, 404)
        self.assertEqual(self.client.post(f'{base}/notes/', {'notes': 'n'}).status_code, 404)
        self.assertEqual(self.client.post(f'{base}/vote/', {'vote': 'YES'}).status_code, 404)
        self.assertEqual(
            self.client.post(f'{base}/add-action-item/', {'title': 'T'}).status_code, 404)

    def test_meeting_send_invites_400_without_valid_emails(self):
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title='Invite Council', agenda='A',
            participants=[{'name': 'No Email Here'}])
        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/send-invites/')
        self.assertEqual(res.status_code, 400)
        self.assertIn('No participants', res.data['error'])

    def test_meeting_send_invites_success(self):
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title='Invite Council', agenda='A',
            participants=[{'name': 'DG', 'email': 'dg@government.gov.ng'},
                          {'name': 'Dev', 'email': 'dev@example.com'}])
        with patch('apps.notifications.email_service.EmailService.send_email',
                   return_value={'success': True, 'id': 're-1'}):
            res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/send-invites/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['sent'], 2)
        self.assertEqual(res.data['total'], 2)

    def test_meeting_join_notes_vote_and_action_items(self):
        self.client.force_authenticate(user=self.agency_head)
        meeting = StakeholderMeeting.objects.create(
            title='Interactive Council', agenda='A', status='Scheduled',
            participants=[{'name': 'DG', 'role': 'Agency Head',
                           'email': 'dg@government.gov.ng', 'status': 'Confirmed'}])

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/join/', {
            'name': 'DG', 'role': 'Agency Head', 'email': 'dg@government.gov.ng'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['participants'][0]['status'], 'Live In Room')
        self.assertEqual(res.data['status'], 'In Progress')

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/notes/', {
            'notes': 'Minutes: stage-gate 2 approved in principle.'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['minutes_notes'], 'Minutes: stage-gate 2 approved in principle.')

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/vote/', {
            'voter_name': 'Director Ade', 'voter_role': 'Director',
            'vote': 'YES', 'resolution_title': 'Stage 2 Signoff'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'Recorded')

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/add-action-item/', {})
        self.assertEqual(res.status_code, 400)
        self.assertIn('title', res.data['error'])

        res = self.client.post(f'/api/v1/stakeholders/meetings/{meeting.id}/add-action-item/', {
            'title': 'Submit pile test results', 'assignee_name': 'GeoTech Lab'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['title'], 'Submit pile test results')
        self.assertFalse(res.data['is_completed'])
        self.assertTrue(MeetingActionItem.objects.filter(meeting=meeting).exists())


class MessageViewTests(BaseStakeholderServiceTestCase):

    def test_message_channel_filter(self):
        self.client.force_authenticate(user=self.agency_head)
        StakeholderMessage.objects.create(sender_name='A', channel_name='General Council',
                                          message_text='one')
        StakeholderMessage.objects.create(sender_name='B', channel_name='Executive',
                                          message_text='two')
        res = self.client.get('/api/v1/stakeholders/messages/',
                              {'channel': 'general council'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['channel_name'], 'General Council')

        res = self.client.get('/api/v1/stakeholders/messages/', {'channel': 'ALL'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 2)

    def test_message_create_via_api(self):
        self.client.force_authenticate(user=self.regular_officer)
        res = self.client.post('/api/v1/stakeholders/messages/', {
            'channel_name': 'Project Coordination',
            'project_name': 'Nexus Tower',
            'message_text': 'Coordination update from the field.',
            'is_urgent': False,
        }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['sender_name'], 'Chidi Okafor')
        self.assertFalse(res.data['is_urgent'])

    def test_message_serializer_includes_translations(self):
        self.client.force_authenticate(user=self.agency_head)
        msg = StakeholderMessage.objects.create(sender_name='A',
                                                channel_name='General Council',
                                                message_text='translatable')
        MessageTranslation.objects.create(message=msg, target_language='yo',
                                          translated_content='iyí')
        res = self.client.get(f'/api/v1/stakeholders/messages/{msg.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data['translations']), 1)
        self.assertEqual(res.data['translations'][0]['translated_content'], 'iyí')


class CertificationAndTrainingViewTests(BaseStakeholderServiceTestCase):

    def test_certification_crud(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/certifications/', {
            'authority': 'COREN',
            'license_number': 'COREN-RN-7741',
            'issue_date': '2024-01-15',
            'expiry_date': '2027-01-15',
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['authority'], 'COREN')
        self.assertFalse(res.data['is_verified'])

        # Duplicate license numbers are rejected.
        res = self.client.post('/api/v1/stakeholders/certifications/', {
            'authority': 'COREN',
            'license_number': 'COREN-RN-7741',
            'issue_date': '2024-01-15',
            'expiry_date': '2027-01-15',
        })
        self.assertEqual(res.status_code, 400)

        res = self.client.get('/api/v1/stakeholders/certifications/')
        self.assertEqual(res.status_code, 200)

    def test_training_record_crud(self):
        self.client.force_authenticate(user=self.agency_head)
        inspector = Inspector.objects.create(inspector_id='INS-TR', name='Trainee Officer')
        res = self.client.post('/api/v1/stakeholders/trainings/', {
            'inspector': str(inspector.id),
            'course_name': 'Advanced Structural Assessment',
            'completion_date': '2026-06-30',
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['course_name'], 'Advanced Structural Assessment')

        res = self.client.get('/api/v1/stakeholders/trainings/')
        self.assertEqual(res.status_code, 200)
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)


# ---------------------------------------------------------------------------
# Additional edge-case coverage
# ---------------------------------------------------------------------------

class ParseDisplayDatetimeEdgeTests(TestCase):

    def test_unparseable_date_falls_back_to_today(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        start, end = parse_display_datetime('not a date at all', '09:00 - 10:00')
        self.assertGreaterEqual(start.date(), now.date())
        self.assertEqual(end - start, datetime.timedelta(hours=1))

    def test_past_time_today_rolls_to_tomorrow(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # Midnight today is in the past for any run after 00:00:00.
        start, _ = parse_display_datetime(None, '12:00 AM - 1:00 AM')
        expected_date = now.date() + datetime.timedelta(days=1) if now.time() > datetime.time(0, 0) else now.date()
        self.assertEqual(start.date(), expected_date)


class GoogleMeetEventEdgeTests(TestCase):

    def setUp(self):
        GoogleMeetCalendarService._credentials = None

    def tearDown(self):
        GoogleMeetCalendarService._credentials = None

    def test_poll_breaks_when_event_fetch_fails(self):
        factory = FakeCalendarServiceFactory(
            lambda kwargs: {'id': 'evt-fetchfail', 'htmlLink': 'link', 'hangoutLink': ''},
        )
        factory.service.events.return_value.get.side_effect = Exception('fetch failed')
        with patch.object(GoogleMeetCalendarService, '_calendar_service',
                          return_value=factory.service):
            with patch('time.sleep'):
                with patch.object(GoogleMeetCalendarService, 'create_meet_space',
                                  return_value={'meeting_uri': '', 'meet_id': '',
                                                'error': 'HTTP 403: Meet API disabled'}):
                    result = GoogleMeetCalendarService.create_meeting_event(
                        title='Council', agenda='', start=START, end=END,
                    )
        # Fetch failure during polling must degrade honestly — no meet link.
        self.assertEqual(result['hangout_link'], '')
        self.assertEqual(result['status'], 'created')
        self.assertTrue(result['meet_error'])


class ScheduleMeetingEdgeTests(BaseStakeholderServiceTestCase):

    def test_schedule_meeting_calendar_failure_reports_honest_note(self):
        # create_meeting_event returns an all-empty result (every attempt
        # failed) without raising — the meeting is still stored, marked
        # unavailable, with the honest calendar error in the audit trail.
        with patch('apps.stakeholders.services.GoogleMeetCalendarService.create_meeting_event',
                   return_value={'event_id': '', 'html_link': '', 'hangout_link': '',
                                 'meet_link_source': '', 'status': '',
                                 'meet_error': '', 'calendar_error': 'Backend Error 500',
                                 'attendees_invited': False}):
            meeting = StakeholderService.schedule_meeting({
                'title': 'Failed Calendar',
                'agenda': 'A',
                'meeting_type': 'Video Call',
            }, user=self.agency_head)
        self.assertEqual(meeting.meet_link_status, 'unavailable')
        self.assertEqual(meeting.google_calendar_event_id, '')
        audit = AuditEvent.objects.filter(action='STAKEHOLDER_MEETING_SCHEDULED',
                                          resource_id=str(meeting.id)).first()
        self.assertEqual(audit.new_state['meet_note'], 'Backend Error 500')

    def test_audit_failure_never_breaks_scheduling(self):
        with patch('apps.audit.models.AuditEvent.objects.create',
                   side_effect=Exception('audit store down')):
            meeting = StakeholderService.schedule_meeting({
                'title': 'Audit Down',
                'agenda': 'A',
                'meeting_type': 'In-Person Council',
            }, user=self.agency_head)
        self.assertEqual(meeting.title, 'Audit Down')

    def test_notification_failure_never_breaks_messaging(self):
        with patch('apps.notifications.models.Notification.objects.create',
                   side_effect=Exception('notification store down')):
            msg = StakeholderService.send_message({
                'channel_name': 'General Council',
                'message_text': 'Still delivered',
                'is_urgent': True,
            }, user=self.agency_head)
        self.assertEqual(msg.message_text, 'Still delivered')


class UploadToR2EdgeTests(BaseStakeholderServiceTestCase):

    def test_file_like_payload_uploaded_to_r2(self):
        import io
        client = MagicMock()
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   return_value=client):
            url = StakeholderService.upload_to_cloudflare_r2(io.BytesIO(b'filebody'), 'notes.txt')
        self.assertIn('notes.txt', url)
        client.put_object.assert_called_once()
        self.assertEqual(client.put_object.call_args.kwargs['Body'], b'filebody')

    def test_storage_helper_exception_returns_payload_unchanged(self):
        with patch('apps.documents.services.DocumentStorageService.get_s3_client',
                   side_effect=Exception('boto3 misconfigured')):
            payload = 'data:application/pdf;base64,aGVsbG8='
            self.assertEqual(StakeholderService.upload_to_cloudflare_r2(payload, 'x.pdf'),
                             payload)


class TranslationCloudApiTests(TestCase):

    def setUp(self):
        TranslationService._cached_credentials = None

    def tearDown(self):
        TranslationService._cached_credentials = None

    def test_google_cloud_translation_success_path(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Translate via the cloud API')
        creds = MagicMock()
        creds.token = 'ya29.token'
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {'data': {'translations': [{'translatedText': 'Ìtumọ̀ ìjábọ̀'}]}}).encode('utf-8')
        with patch.object(TranslationService, 'get_google_credentials', return_value=creds):
            with patch('urllib.request.urlopen', return_value=response) as urlopen:
                result = TranslationService.translate_message(msg.id, 'yo')
        self.assertEqual(result['translated_content'], 'Ìtumọ̀ ìjábọ̀')
        self.assertIn('Google Cloud Translation v2', result['provider'])
        self.assertFalse(result['is_cached'])
        # The DB cache was still populated.
        self.assertTrue(MessageTranslation.objects.filter(message=msg,
                                                          target_language='yo').exists())
        request_obj = urlopen.call_args.args[0]
        self.assertEqual(request_obj.headers.get('Authorization'), 'Bearer ya29.token')
        self.assertEqual(request_obj.headers.get('X-goog-user-project'), 'serious-water-469715-f9')

    def test_google_cloud_translation_failure_falls_back_to_neural(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Cloud will fail here')
        creds = MagicMock()
        creds.token = 'ya29.token'
        with patch.object(TranslationService, 'get_google_credentials', return_value=creds):
            with patch('urllib.request.urlopen', side_effect=OSError('offline')):
                result = TranslationService.translate_message(msg.id, 'ha')
        self.assertTrue(result['translated_content'].startswith('Fassarar Hausa:'))
        self.assertIn('Neural Hausa Engine', result['provider'])

    def test_igbo_and_hausa_dictionary_exact_matches(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Stop-work order issued.')
        with patch.object(TranslationService, 'get_google_credentials', return_value=None):
            ig = TranslationService.translate_message(msg.id, 'ig')
            ha = TranslationService.translate_message(msg.id, 'ha')
        self.assertEqual(ig['translated_content'], 'Enyela iwu ka a kwụsị ọrụ ozugbo')
        self.assertEqual(ha['translated_content'], 'An ba da umarnin dakatar da aiki nan take')

    def test_audit_failure_does_not_break_translation(self):
        msg = StakeholderMessage.objects.create(
            sender_name='Inspector', channel_name='General Council',
            message_text='Audit is down')
        with patch.object(TranslationService, 'get_google_credentials', return_value=None):
            with patch('apps.audit.models.AuditEvent.objects.create',
                       side_effect=Exception('audit store down')):
                result = TranslationService.translate_message(msg.id, 'yo')
        self.assertTrue(result['translated_content'].startswith('Ìtumọ̀ Yorùbá:'))


class RemainingViewCoverageTests(BaseStakeholderServiceTestCase):

    def test_entity_creation_audits_via_api(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.post('/api/v1/stakeholders/contractors/', {'name': 'API Contractor'})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(AuditEvent.objects.filter(
            action='STAKEHOLDER_CONTRACTOR_CREATED').exists())

        res = self.client.post('/api/v1/stakeholders/consultants/', {'name': 'API Consultant'})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(AuditEvent.objects.filter(
            action='STAKEHOLDER_CONSULTANT_CREATED').exists())

        res = self.client.post('/api/v1/stakeholders/inspectors/', {'name': 'API Inspector'})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(AuditEvent.objects.filter(
            action='STAKEHOLDER_INSPECTOR_CREATED').exists())

        res = self.client.post('/api/v1/stakeholders/professionals/', {
            'name': 'API Professional', 'role_title': 'Engineer', 'firm_name': 'Firm'})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(AuditEvent.objects.filter(
            action='STAKEHOLDER_PROFESSIONAL_REGISTERED').exists())

    def test_consultant_inspector_professional_and_team_search(self):
        self.client.force_authenticate(user=self.agency_head)
        Consultant.objects.create(name='Advisory Board', specialty='Structural')
        Inspector.objects.create(inspector_id='INS-SE', name='Searchable Officer',
                                 role_title='Senior Inspector')
        LicensedProfessional.objects.create(name='Engr. Search', role_title='Engineer',
                                            firm_name='Search Firm')
        ProjectStakeholderTeam.objects.create(project_reference='PRJ-SE',
                                              project_name='Searchable Tower')

        res = self.client.get('/api/v1/stakeholders/consultants/', {'search': 'advisory'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)

        res = self.client.get('/api/v1/stakeholders/inspectors/', {'search': 'searchable'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)

        res = self.client.get('/api/v1/stakeholders/professionals/', {'search': 'search firm'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)

        res = self.client.get('/api/v1/stakeholders/teams/', {'search': 'Searchable'})
        data = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(data), 1)

    def test_reassign_zone_requires_zone(self):
        self.client.force_authenticate(user=self.agency_head)
        inspector = Inspector.objects.create(inspector_id='INS-RZ', name='Zone Officer')
        res = self.client.post(f'/api/v1/stakeholders/inspectors/{inspector.id}/reassign-zone/', {})
        self.assertEqual(res.status_code, 400)

    def test_meeting_detail_and_send_invites_404_on_empty_table(self):
        self.client.force_authenticate(user=self.agency_head)
        res = self.client.get(f'/api/v1/stakeholders/meetings/{uuid.uuid4()}/')
        self.assertEqual(res.status_code, 404)
        res = self.client.post(f'/api/v1/stakeholders/meetings/{uuid.uuid4()}/send-invites/')
        self.assertEqual(res.status_code, 404)
