"""
Google Calendar + Google Meet integration for official stakeholder meetings.

Uses the dedicated `nexucon-meeting@serious-water-469715-f9.iam.gserviceaccount.com`
service account (configured via GOOGLE_MEETING_* environment variables, with a
fallback to the JSON key files under `apis/`).

Real behaviour, no mocked links:
- A real Google Calendar event is created for every scheduled meeting and its
  id / htmlLink are persisted on the StakeholderMeeting record.
- A real Google Meet link is attached when the credentials allow it:
  1. Calendar conferenceData (hangoutsMeet) — works when the calendar owner has
     Google Meet capability (e.g. Workspace / domain-wide delegation).
  2. Google Meet REST API spaces.create — works when the Google Meet API
     (meet.googleapis.com) is enabled on the Cloud project.
- When neither path is available the service reports the exact reason instead
  of fabricating a meet.google.com link.
"""
import os
import re
import uuid
import logging
import datetime
import urllib.parse
from email.utils import parseaddr
from typing import List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

CALENDAR_SCOPE = 'https://www.googleapis.com/auth/calendar'
MEET_SCOPE = 'https://www.googleapis.com/auth/meetings.space.created'
MEET_API_BASE = 'https://meet.googleapis.com/v2/spaces'

# Candidate service-account JSON key files (project serious-water-469715-f9)
_SA_FILE_CANDIDATES = [
    'apis/serious-water-469715-f9-b749b9fe57f5.json',   # nexucon-meeting@
    'apis/serious-water-469715-f9-3e152be16bfb.json',   # nexucon-meeting-schedule@
]


class GoogleMeetCalendarError(Exception):
    """Raised when the Google integration is not usable at all (bad/missing credentials)."""


def _normalize_private_key(raw: Optional[str]) -> Optional[str]:
    """Convert an env-provided private key (possibly with literal \\n) into PEM form."""
    if not raw:
        return None
    key = raw.strip().strip('"').strip("'").replace('\\n', '\n').replace('\r', '')
    if key and not key.endswith('\n'):
        key += '\n'
    return key or None


def _load_service_account_info() -> Optional[dict]:
    """Build service-account info dict from env vars or a JSON key file."""
    client_email = (os.getenv('GOOGLE_MEETING_CLIENT_EMAIL') or '').strip()
    private_key = _normalize_private_key(os.getenv('GOOGLE_MEETING_PRIVATE_KEY'))
    project_id = (os.getenv('GOOGLE_MEETING_PROJECT_ID') or '').strip()

    if client_email and private_key:
        return {
            'type': 'service_account',
            'project_id': project_id or 'serious-water-469715-f9',
            'client_email': client_email,
            'private_key': private_key,
            'token_uri': 'https://oauth2.googleapis.com/token',
        }

    # Fall back to the JSON key files shipped in the repository
    for rel in _SA_FILE_CANDIDATES:
        path = os.path.join(str(settings.BASE_DIR), rel)
        if os.path.exists(path):
            import json
            try:
                with open(path, 'r') as f:
                    info = json.load(f)
                if info.get('client_email') and info.get('private_key'):
                    return info
            except Exception as ex:
                logger.warning("[GoogleMeet] Could not read service account file %s: %s", path, ex)
    return None


def parse_display_datetime(date_str: Optional[str], time_slot: Optional[str],
                           default_duration_minutes: int = 90) -> Tuple[datetime.datetime, datetime.datetime]:
    """
    Convert the human-readable date/time the frontend sends into timezone-aware
    UTC datetimes. Examples handled:
      date:      "Aug 30, 2026" | "2026-08-30" | "08/30/2026" | ISO datetime
      time_slot: "10:00 AM - 11:30 AM" | "10:00 - 11:30" | "14:00"
    Falls back to the next full hour from now.
    """
    now = datetime.datetime.now(datetime.timezone.utc)

    start_date = None
    date_str = (date_str or '').strip()
    if date_str:
        for fmt in ('%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y'):
            try:
                start_date = datetime.datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if start_date is None:
            try:  # ISO datetime from the API
                start_date = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')).date()
            except ValueError:
                pass

    def _parse_clock(token: str) -> Optional[datetime.time]:
        token = token.strip().upper().replace('.', '')
        for fmt in ('%I:%M %p', '%I %p', '%H:%M:%S', '%H:%M', '%I:%M%p'):
            try:
                return datetime.datetime.strptime(token, fmt).time()
            except ValueError:
                continue
        return None

    start_time = None
    end_time = None
    slot = (time_slot or '').strip()
    if slot:
        # "10:00 AM - 11:30 AM" / "10:00 - 11:30" / single time
        parts = [p for p in re.split(r'\s*(?:[-–—]|to)\s*', slot, flags=re.IGNORECASE) if p.strip()]
        meridiem = None
        if len(parts) == 2 and 'AM' not in parts[1].upper() and 'PM' not in parts[1].upper():
            # "10:00 AM - 11:30" — inherit meridiem from the first token
            meridiem = 'AM' if 'AM' in parts[0].upper() else ('PM' if 'PM' in parts[0].upper() else None)
        start_time = _parse_clock(parts[0])
        if start_time is None:
            start_time = None
        if len(parts) >= 2:
            second_token = parts[1]
            if meridiem and second_token and 'AM' not in second_token.upper() and 'PM' not in second_token.upper():
                second_token = f"{second_token} {meridiem}"
            end_time = _parse_clock(second_token)
    if start_date is None:
        start_date = now.date()
        if start_time is not None:
            candidate = datetime.datetime.combine(start_date, start_time, tzinfo=datetime.timezone.utc)
            if candidate < now:
                start_date = start_date + datetime.timedelta(days=1)
    if start_time is None:
        # next full hour
        nxt = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        start = datetime.datetime.combine(start_date, datetime.time(nxt.hour, nxt.minute), tzinfo=datetime.timezone.utc)
    else:
        start = datetime.datetime.combine(start_date, start_time, tzinfo=datetime.timezone.utc)
    if end_time is not None:
        end = datetime.datetime.combine(start_date, end_time, tzinfo=datetime.timezone.utc)
        if end <= start:
            end += datetime.timedelta(days=1)
    else:
        end = start + datetime.timedelta(minutes=default_duration_minutes)
    return start, end


def extract_attendee_emails(participants) -> List[str]:
    """Pull valid email addresses out of a participants list of dicts."""
    emails = []
    if not participants:
        return emails
    if isinstance(participants, dict):
        participants = [participants]
    for p in participants:
        if not isinstance(p, dict):
            continue
        raw = (p.get('email') or '').strip()
        if not raw:
            continue
        addr = parseaddr(raw)[1]
        if addr and re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', addr):
            emails.append(addr)
    # de-duplicate, preserve order
    seen = set()
    return [e for e in emails if not (e in seen or seen.add(e))]


class GoogleMeetCalendarService:
    """Service-account backed Google Calendar/Meet operations."""

    _credentials = None

    # ------------------------------------------------------------------ creds
    @classmethod
    def get_credentials(cls):
        """Return (and cache) a valid service-account credential or raise GoogleMeetCalendarError."""
        if cls._credentials is not None and cls._credentials.valid:
            return cls._credentials
        info = _load_service_account_info()
        if not info:
            raise GoogleMeetCalendarError(
                'Google Meet/Calendar service account is not configured. '
                'Set GOOGLE_MEETING_CLIENT_EMAIL and GOOGLE_MEETING_PRIVATE_KEY '
                '(or provide a service account JSON file).'
            )
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[CALENDAR_SCOPE, MEET_SCOPE]
            )
            creds.refresh(Request())
            cls._credentials = creds
            logger.info('[GoogleMeet] Authenticated as %s', info['client_email'])
            return creds
        except GoogleMeetCalendarError:
            raise
        except Exception as ex:
            cls._credentials = None
            raise GoogleMeetCalendarError(f'Google authentication failed: {ex}')

    @classmethod
    def configured(cls) -> bool:
        try:
            cls.get_credentials()
            return True
        except GoogleMeetCalendarError:
            return False

    @classmethod
    def service_account_email(cls) -> str:
        info = _load_service_account_info()
        return (info or {}).get('client_email', '')

    # -------------------------------------------------------------- calendar
    @classmethod
    def _calendar_service(cls):
        from googleapiclient.discovery import build
        creds = cls.get_credentials()
        return build('calendar', 'v3', credentials=creds, cache_discovery=False)

    @classmethod
    def create_meeting_event(cls, *, title: str, agenda: str = '', start: datetime.datetime,
                             end: datetime.datetime, attendees: Optional[List[str]] = None,
                             meeting_reference: str = '', project_name: str = '',
                             add_meet_conference: bool = True) -> dict:
        """
        Create a real Calendar event (optionally with a Meet conference).

        Returns a dict:
          event_id, html_link, hangout_link (may be ''), meet_link_source,
          status ('created_with_meet' | 'created' | 'created_without_conference'),
          meet_error, calendar_error
        """
        result = {
            'event_id': '', 'html_link': '', 'hangout_link': '', 'meet_link_source': '',
            'status': '', 'meet_error': '', 'calendar_error': '', 'attendees_invited': False,
        }
        attendees = [a for a in (attendees or []) if a]
        description_lines = []
        if agenda:
            description_lines.append(agenda)
        if project_name:
            description_lines.append(f'Project: {project_name}')
        if meeting_reference:
            description_lines.append(f'Reference: {meeting_reference}')

        event_body = {
            'summary': title or 'Nexucon Stakeholder Meeting',
            'description': '\n'.join(description_lines),
            'start': {'dateTime': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'timeZone': 'UTC'},
            'end': {'dateTime': end.strftime('%Y-%m-%dT%H:%M:%SZ'), 'timeZone': 'UTC'},
            'reminders': {'useDefault': True},
            'guestsCanSeeOtherGuests': True,
            'guestsCanInviteOthers': False,
        }
        if attendees:
            event_body['attendees'] = [{'email': a} for a in attendees]

        service = cls._calendar_service()
        created = None

        # Progressive fallbacks. A plain service account (without Domain-Wide
        # Delegation) cannot invite attendees and has no Google Meet conference
        # capability, so we degrade gracefully and report what happened:
        #   1. attendees + Meet conference + email updates (DWD / Workspace)
        #   2. Meet conference only, no attendees
        #   3. plain calendar event, no attendees, no conference
        def _with_conference(body):
            body = dict(body)
            body['conferenceData'] = {
                'createRequest': {
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'},
                    'requestId': uuid.uuid4().hex,
                }
            }
            return body

        attempts = []
        if attendees:
            attempts.append(('attendees+meet', _with_conference(event_body), 'all'))
        if add_meet_conference:
            no_attendees = {k: v for k, v in event_body.items() if k != 'attendees'}
            attempts.append(('meet-only', _with_conference(no_attendees), 'none'))
        attempts.append(('plain', {k: v for k, v in event_body.items() if k != 'attendees'}, 'none'))

        used_attendees = False
        for label, body, send_updates in attempts:
            kwargs = {
                'calendarId': 'primary',
                'body': body,
                'sendUpdates': send_updates if body.get('attendees') else 'none',
            }
            if 'conferenceData' in body:
                kwargs['conferenceDataVersion'] = 1
            try:
                created = service.events().insert(**kwargs).execute()
                used_attendees = bool(body.get('attendees'))
                break
            except Exception as ex:
                detail = str(ex)
                if 'forbiddenforserviceaccounts' in detail.lower() or 'domain-wide' in detail.lower():
                    # Attendees cannot be invited without DWD; continue without them
                    logger.warning('[GoogleMeet] Attendee invitations require Domain-Wide Delegation; '
                                   'creating event without attendees (invitations sent via Resend instead).')
                elif 'invalid conference type' in detail.lower() or 'conference' in detail.lower():
                    result['meet_error'] = detail
                    logger.warning('[GoogleMeet] Conference creation unavailable (%s).', detail[:200])
                else:
                    result['calendar_error'] = detail
                    logger.error('[GoogleMeet] Calendar event creation failed: %s', detail[:300])
                    created = None
                    continue

        if created is None:
            return result

        result['status'] = 'created'
        result['attendees_invited'] = used_attendees
        result['event_id'] = created.get('id') or ''
        result['html_link'] = created.get('htmlLink') or ''

        # The Meet link is populated asynchronously — poll briefly for it
        hangout = created.get('hangoutLink') or ''
        if not hangout and add_meet_conference and result['event_id'] and not result['meet_error']:
            for _ in range(5):
                import time
                time.sleep(1)
                try:
                    fetched = service.events().get(calendarId='primary', eventId=result['event_id']).execute()
                except Exception:
                    break
                hangout = fetched.get('hangoutLink') or ''
                conf = (fetched.get('conferenceData') or {})
                req = conf.get('createRequest') or {}
                if hangout or req.get('status', {}).get('statusCode') not in (None, 'pending'):
                    break

        if hangout:
            result['hangout_link'] = hangout
            result['meet_link_source'] = 'google_calendar_conference'
            result['status'] = 'created_with_meet'
        elif add_meet_conference and not result['meet_error']:
            # Conference accepted by the API but never materialised
            result['meet_error'] = 'Conference request returned no hangoutLink'

        # Last resort for a real Meet link: the Google Meet REST API
        # (requires the Google Meet API to be enabled on the Cloud project).
        if not result['hangout_link'] and add_meet_conference:
            space = cls.create_meet_space()
            if space.get('meeting_uri'):
                result['hangout_link'] = space['meeting_uri']
                result['meet_link_source'] = 'google_meet_api'
                result['status'] = 'created_with_meet'
                result['meet_error'] = ''
            elif space.get('error'):
                result['meet_error'] = result['meet_error'] or space['error']

        return result

    @classmethod
    def delete_event(cls, event_id: str) -> bool:
        if not event_id:
            return False
        try:
            service = cls._calendar_service()
            service.events().delete(calendarId='primary', eventId=event_id).execute()
            return True
        except Exception as ex:
            logger.warning('[GoogleMeet] Could not delete calendar event %s: %s', event_id, ex)
            return False

    @classmethod
    def update_event(cls, event_id: str, *, title=None, agenda=None, start=None, end=None,
                     attendees=None) -> bool:
        if not event_id:
            return False
        try:
            service = cls._calendar_service()
            event = service.events().get(calendarId='primary', eventId=event_id).execute()
            if title is not None:
                event['summary'] = title
            if agenda is not None:
                event['description'] = agenda
            if start is not None:
                event['start'] = {'dateTime': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'timeZone': 'UTC'}
            if end is not None:
                event['end'] = {'dateTime': end.strftime('%Y-%m-%dT%H:%M:%SZ'), 'timeZone': 'UTC'}
            if attendees is not None:
                event['attendees'] = [{'email': a} for a in attendees]
            service.events().update(calendarId='primary', eventId=event_id, body=event).execute()
            return True
        except Exception as ex:
            logger.warning('[GoogleMeet] Could not update calendar event %s: %s', event_id, ex)
            return False

    # ------------------------------------------------------------- meet api
    @classmethod
    def create_meet_space(cls) -> dict:
        """
        Create a standalone Google Meet space via the Google Meet REST API.
        Requires the Meet API (meet.googleapis.com) to be enabled on the project.
        """
        result = {'meeting_uri': '', 'meet_id': '', 'error': ''}
        try:
            creds = cls.get_credentials()
            import requests
            resp = requests.post(
                MEET_API_BASE,
                headers={'Authorization': f'Bearer {creds.token}',
                         'Content-Type': 'application/json'},
                json={}, timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                result['meeting_uri'] = data.get('meetingUri') or data.get('meeting_code', '')
                result['meet_id'] = data.get('name', '')
            else:
                result['error'] = f'HTTP {resp.status_code}: {resp.text[:300]}'
        except GoogleMeetCalendarError as ex:
            result['error'] = str(ex)
        except Exception as ex:
            result['error'] = str(ex)
        return result
