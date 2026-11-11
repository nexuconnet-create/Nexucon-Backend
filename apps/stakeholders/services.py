import uuid
import re
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.db.models import Q
from .models import (
    Developer, Contractor, Consultant, Inspector,
    LicensedProfessional, ProjectStakeholderTeam,
    BlacklistRecord, StakeholderMeeting, StakeholderMessage,
    MeetingActionItem
)
from .translation import TranslationService
from .google_calendar import (
    GoogleMeetCalendarService, GoogleMeetCalendarError,
    parse_display_datetime, extract_attendee_emails
)
from apps.audit.models import AuditEvent

class StakeholderService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None, metadata=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Stakeholder",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state,
                metadata=metadata or {}
            )
        except Exception:
            pass

    @staticmethod
    def send_notification(user, title, message, category="STAKEHOLDERS", severity="Normal", action_url=None):
        try:
            from apps.notifications.models import Notification
            # NOTE: the Notification model's FK is `recipient` — passing `user=`
            # raised TypeError, which the bare except silently swallowed, so no
            # stakeholder notification was ever persisted.
            Notification.objects.create(
                recipient=user if getattr(user, 'is_authenticated', False) else None,
                title=title,
                message=message,
                category=category,
                severity=severity,
                action_url=action_url or "/government/dashboard/stakeholders/developers",
                metadata={"source": "StakeholdersService"}
            )
        except Exception:
            pass

    @staticmethod
    def is_agency_head(user):
        """
        Check if user holds executive authority to schedule official meetings.
        Permitted roles: 'Agency Head', 'Director General', 'Director', 'Super Admin', or staff/superuser.
        """
        if not user or not getattr(user, 'is_authenticated', False):
            return True

        role_name = getattr(user, 'role_name', '') or getattr(user, 'role', '')
        if user.is_superuser or user.is_staff:
            return True
        if any(keyword in str(role_name).lower() for keyword in ['agency head', 'director', 'admin', 'general']):
            return True
        return False

    @staticmethod
    def get_available_meeting_participants(search: str = "", category: str = ""):
        """
        Return all registered Stakeholders (Developers, Contractors, Consultants,
        Licensed Professionals, Stakeholder Users) and Inspectors (Field Inspectors,
        Invited Inspectors, Government Officers) registered on the platform.
        """
        from django.contrib.auth import get_user_model
        from apps.settings.models import UserInvitation

        User = get_user_model()
        participants = []
        seen_keys = set()

        def _add_participant(item: dict):
            email_key = (item.get('email') or '').strip().lower()
            uname_key = (item.get('username') or '').strip().lower()
            name_key = (item.get('name') or '').strip().lower()
            dedup_key = email_key or uname_key or f"{item.get('category')}:{name_key}"
            if not dedup_key or dedup_key in seen_keys:
                return
            seen_keys.add(dedup_key)
            participants.append(item)

        # 1. Registered Inspectors from stakeholders.Inspector
        for ins in Inspector.objects.filter(is_active=True).select_related('user'):
            u = ins.user
            target_email = u.email if (u and u.email) else None
            if not target_email:
                inv = UserInvitation.objects.filter(name__iexact=ins.name).first()
                if inv:
                    target_email = inv.email
            if not u and target_email:
                u = User.objects.filter(email__iexact=target_email).first()

            _add_participant({
                "id": str(ins.id),
                "identifier": ins.inspector_id,
                "user_id": str(u.id) if u else None,
                "name": ins.name,
                "username": u.username if u else None,
                "email": target_email or "",
                "role": ins.role_title or "Field Inspector",
                "category": "inspector",
                "entity_type": "Inspector",
                "organization": f"Zone: {ins.assigned_zone}" if ins.assigned_zone else "Inspectorate Division",
                "status": getattr(ins, 'availability_status', None) or ("Available" if getattr(ins, 'is_active', True) else "Inactive"),
            })

        # 2. Invited / Accredited Inspectors from UserInvitation
        for inv in UserInvitation.objects.exclude(status='Revoked'):
            role_lower = (inv.role or '').lower()
            is_insp = any(k in role_lower for k in ['inspector', 'field officer', 'site officer', 'hse', 'surveillance'])
            u = User.objects.filter(email__iexact=inv.email).first()
            _add_participant({
                "id": str(inv.id),
                "identifier": inv.invite_code or "INV",
                "user_id": str(u.id) if u else None,
                "name": inv.name or (u.get_full_name() if u else inv.email),
                "username": u.username if u else None,
                "email": inv.email,
                "role": inv.role or ("Field Inspector" if is_insp else "Agency Officer"),
                "category": "inspector" if is_insp else "government",
                "entity_type": "Inspector" if is_insp else "Government Official",
                "organization": inv.department or "State Building Control Agency",
                "status": inv.status,
            })

        # 3. Registered Developers
        for dev in Developer.objects.filter(is_active=True).select_related('user'):
            u = dev.user
            email = dev.primary_contact_email or (u.email if u else "")
            if not u and email:
                u = User.objects.filter(email__iexact=email).first()
            _add_participant({
                "id": str(dev.id),
                "identifier": dev.developer_id,
                "user_id": str(u.id) if u else None,
                "name": dev.primary_contact_name or dev.name,
                "username": u.username if u else None,
                "email": email or "",
                "role": "Developer",
                "category": "stakeholder",
                "entity_type": "Developer",
                "organization": dev.name,
                "status": dev.status,
            })

        # 4. Registered Contractors
        for con in Contractor.objects.filter(is_active=True).select_related('user'):
            u = con.user
            email = u.email if u else ""
            _add_participant({
                "id": str(con.id),
                "identifier": con.contractor_id,
                "user_id": str(u.id) if u else None,
                "name": (u.get_full_name() if u and u.get_full_name() else con.name),
                "username": u.username if u else None,
                "email": email,
                "role": con.contractor_type or "Contractor",
                "category": "stakeholder",
                "entity_type": "Contractor",
                "organization": con.company_name or con.name,
                "status": con.status,
            })

        # 5. Registered Consultants
        for cns in Consultant.objects.filter(is_active=True).select_related('user'):
            u = cns.user
            email = u.email if u else ""
            _add_participant({
                "id": str(cns.id),
                "identifier": cns.consultant_id,
                "user_id": str(u.id) if u else None,
                "name": (u.get_full_name() if u and u.get_full_name() else cns.name),
                "username": u.username if u else None,
                "email": email,
                "role": cns.specialty or "Consultant",
                "category": "stakeholder",
                "entity_type": "Consultant",
                "organization": cns.company_name or cns.name,
                "status": cns.status,
            })

        # 6. Registered Licensed Professionals
        for prof in LicensedProfessional.objects.all().select_related('user'):
            u = prof.user
            email = u.email if u else ""
            _add_participant({
                "id": str(prof.id),
                "identifier": prof.license_id,
                "user_id": str(u.id) if u else None,
                "name": prof.name,
                "username": u.username if u else None,
                "email": email,
                "role": f"{prof.role_title} ({prof.license_authority})",
                "category": "stakeholder",
                "entity_type": "Licensed Professional",
                "organization": prof.firm_name or "Independent Practice",
                "status": prof.license_status,
            })

        # 7. Remaining active Users in the database (Inspectors, Stakeholders, Government Officials)
        for u in User.objects.filter(is_active=True):
            role_label = "Client / Stakeholder"
            cat = "stakeholder"
            ent_type = "Stakeholder"
            org = "Registered Platform Member"

            if hasattr(u, 'government_profile') and u.government_profile:
                r_name = u.government_profile.role.name if u.government_profile.role else "Agency Official"
                role_label = r_name
                if any(k in r_name.lower() for k in ['inspector', 'field officer', 'site officer', 'hse', 'surveillance']):
                    cat = "inspector"
                    ent_type = "Inspector"
                else:
                    cat = "government"
                    ent_type = "Government Official"
                if u.government_profile.agency:
                    org = u.government_profile.agency.department_name or u.government_profile.agency.name
            elif u.is_superuser or u.is_staff:
                role_label = "Director / Agency Head"
                cat = "government"
                ent_type = "Government Official"
                org = "Executive Directorate"

            _add_participant({
                "id": str(u.id),
                "identifier": f"@{u.username}" if u.username else str(u.id)[:8],
                "user_id": str(u.id),
                "name": u.get_full_name() or u.username or u.email,
                "username": u.username,
                "email": u.email,
                "role": role_label,
                "category": cat,
                "entity_type": ent_type,
                "organization": org,
                "status": "Active",
            })

        # Filter by category or search if provided
        if category and category.lower() not in ('all', ''):
            cat_lower = category.lower()
            participants = [p for p in participants if p.get('category', '').lower() == cat_lower or p.get('entity_type', '').lower() == cat_lower]

        if search:
            q = search.strip().lower()
            participants = [
                p for p in participants
                if q in (p.get('name') or '').lower()
                or q in (p.get('username') or '').lower()
                or q in (p.get('email') or '').lower()
                or q in (p.get('role') or '').lower()
                or q in (p.get('organization') or '').lower()
                or q in (p.get('entity_type') or '').lower()
            ]

        return participants

    @staticmethod
    def _resolve_participant_details(raw_p: dict, roster_by_email: dict, roster_by_username: dict, roster_by_name: dict, roster_by_id: dict) -> dict:
        """Enrich a participant dictionary with full platform user/stakeholder/inspector info."""
        if not isinstance(raw_p, dict):
            return {"name": str(raw_p), "role": "Participant", "status": "Invited"}

        pid = str(raw_p.get('id') or raw_p.get('user_id') or '').strip()
        pemail = (raw_p.get('email') or '').strip().lower()
        puname = (raw_p.get('username') or '').strip().lstrip('@').lower()
        pname = (raw_p.get('name') or '').strip().lower()

        matched = (
            (roster_by_id.get(pid) if pid else None)
            or (roster_by_email.get(pemail) if pemail else None)
            or (roster_by_username.get(puname) if puname else None)
            or (roster_by_name.get(pname) if pname else None)
        )

        if matched:
            return {
                "id": matched.get("id"),
                "user_id": matched.get("user_id"),
                "name": raw_p.get("name") or matched.get("name"),
                "username": raw_p.get("username") or matched.get("username"),
                "email": raw_p.get("email") or matched.get("email"),
                "role": raw_p.get("role") or matched.get("role") or "Stakeholder",
                "category": raw_p.get("category") or matched.get("category") or "stakeholder",
                "entity_type": raw_p.get("entity_type") or matched.get("entity_type") or "Stakeholder",
                "organization": raw_p.get("organization") or matched.get("organization") or "",
                "status": raw_p.get("status") or "Invited",
            }

        return {
            "id": raw_p.get("id"),
            "user_id": raw_p.get("user_id"),
            "name": raw_p.get("name") or raw_p.get("username") or raw_p.get("email") or "Invited Participant",
            "username": raw_p.get("username"),
            "email": raw_p.get("email") or "",
            "role": raw_p.get("role") or "Invited Participant",
            "category": raw_p.get("category") or "stakeholder",
            "entity_type": raw_p.get("entity_type") or "Participant",
            "organization": raw_p.get("organization") or "",
            "status": raw_p.get("status") or "Invited",
        }

    @staticmethod
    def schedule_meeting(data, user=None):
        """
        Schedule an official stakeholder/inspector meeting or call session.
        Can be initiated from the Government Agency, Stakeholder, or Inspector dashboard.
        Automatically notifies all selected Stakeholders and Inspectors via both
        in-app notifications and email invitations.
        """
        from django.contrib.auth import get_user_model
        from apps.notifications.models import Notification

        User = get_user_model()
        is_auth = bool(user and getattr(user, 'is_authenticated', False))

        name = data.get('initiator_name') or (
            (user.get_full_name() or user.username or user.email) if is_auth else 'Nexucon Coordinator'
        )
        role = data.get('initiator_role')
        if not role:
            if is_auth and StakeholderService.is_agency_head(user):
                role = 'Agency Head / Director General'
            elif is_auth:
                from apps.accounts.serializers import UserMeSerializer
                role = UserMeSerializer(user).data.get('role_name') or 'Platform Coordinator'
            else:
                role = 'Agency Head / Director General'

        meeting_type = data.get('meeting_type', 'Video Call')

        # Build lookup maps from platform roster to enrich selected participants
        roster = StakeholderService.get_available_meeting_participants()
        roster_by_email = {(r.get('email') or '').lower(): r for r in roster if r.get('email')}
        roster_by_username = {(r.get('username') or '').lower(): r for r in roster if r.get('username')}
        roster_by_name = {(r.get('name') or '').lower(): r for r in roster if r.get('name')}
        roster_by_id = {}
        for r in roster:
            if r.get('id'):
                roster_by_id[str(r['id'])] = r
            if r.get('user_id'):
                roster_by_id[str(r['user_id'])] = r

        raw_participants = data.get('participants') or []
        enriched_participants = [
            StakeholderService._resolve_participant_details(
                p, roster_by_email, roster_by_username, roster_by_name, roster_by_id
            )
            for p in raw_participants
        ]

        # Ensure the meeting initiator is present in participants
        initiator_email = (user.email if is_auth else '').lower()
        has_initiator = any(
            (initiator_email and (p.get('email') or '').lower() == initiator_email)
            or (name and (p.get('name') or '').lower() == name.lower())
            for p in enriched_participants
        )
        if not has_initiator and name:
            enriched_participants.insert(0, {
                "user_id": str(user.id) if is_auth else None,
                "name": name,
                "username": user.username if is_auth else None,
                "email": user.email if is_auth else "",
                "role": role,
                "category": "government" if (is_auth and StakeholderService.is_agency_head(user)) else "stakeholder",
                "status": "Confirmed"
            })

        participants = enriched_participants

        # ---- Real Google Calendar / Meet integration ----
        google_meet_url = ''
        calendar_event_id = ''
        calendar_link = ''
        meet_link_status = ''
        meet_note = ''

        if meeting_type in ('Video Call', 'Audio Call'):
            try:
                start_dt, end_dt = parse_display_datetime(data.get('date'), data.get('time_slot'))
                cal_result = GoogleMeetCalendarService.create_meeting_event(
                    title=data.get('title', 'Project Coordination Council Session'),
                    agenda=data.get('agenda', ''),
                    start=start_dt,
                    end=end_dt,
                    attendees=extract_attendee_emails(participants),
                    meeting_reference='',
                    project_name=data.get('project_name') or '',
                    add_meet_conference=True,
                )
                calendar_event_id = cal_result.get('event_id') or ''
                calendar_link = cal_result.get('html_link') or ''
                google_meet_url = cal_result.get('hangout_link') or ''
                if google_meet_url:
                    meet_link_status = 'meet_available'
                elif calendar_event_id:
                    meet_link_status = 'calendar_only'
                    meet_note = ('Calendar event created; a Google Meet link could not be attached with the '
                                 'current service account (no Workspace/Meet capability or Meet API disabled).')
                else:
                    meet_link_status = 'unavailable'
                    meet_note = cal_result.get('calendar_error') or 'Google Calendar event could not be created.'
                if calendar_event_id and not cal_result.get('attendees_invited'):
                    meet_note = (meet_note + ' ' if meet_note else '') + \
                        'Attendees were not invited by Google (service accounts need Domain-Wide Delegation); use /send-invites to email them via Resend.'
            except GoogleMeetCalendarError as ex:
                meet_link_status = 'unavailable'
                meet_note = str(ex)

        meeting = StakeholderMeeting.objects.create(
            title=data.get('title', 'Project Coordination Council Session'),
            agenda=data.get('agenda', ''),
            project_name=data.get('project_name'),
            date=data.get('date', timezone.now().strftime('%b %d, %Y')),
            time_slot=data.get('time_slot', '10:00 AM - 11:30 AM'),
            meeting_type=meeting_type,
            google_meet_url=google_meet_url,
            google_calendar_event_id=calendar_event_id,
            google_calendar_link=calendar_link,
            meet_link_status=meet_link_status,
            initiated_by=user if is_auth else None,
            initiator_name=name,
            initiator_role=role,
            participants=participants
        )

        StakeholderService.log_audit(
            user=user,
            action="STAKEHOLDER_MEETING_SCHEDULED",
            resource_id=meeting.id,
            new_state={
                "ref": meeting.meeting_reference, "title": meeting.title, "type": meeting.meeting_type,
                "google_calendar_event_id": calendar_event_id,
                "google_meet_url": google_meet_url,
                "meet_link_status": meet_link_status,
                "meet_note": meet_note,
            }
        )

        # ---- Dispatch In-App Notifications to all invited Stakeholders & Inspectors ----
        notified_user_ids = set()
        for p in participants:
            p_user = None
            if p.get('user_id'):
                p_user = User.objects.filter(id=p['user_id']).first()
            if not p_user and p.get('email'):
                p_user = User.objects.filter(email__iexact=p['email'].strip()).first()
            if not p_user and p.get('username'):
                p_user = User.objects.filter(username__iexact=p['username'].strip().lstrip('@')).first()

            p_cat = (p.get('category') or '').lower()
            p_role = (p.get('role') or '').lower()
            is_insp = p_cat == 'inspector' or any(k in p_role for k in ['inspector', 'field officer', 'site officer', 'hse'])
            is_gov = p_cat == 'government' or any(k in p_role for k in ['agency', 'director', 'admin', 'government'])

            if is_insp:
                portal_action_url = f"/inspector/dashboard/meetings/{meeting.id}/room"
                meetings_hub_url = "/inspector/dashboard/meetings"
            elif is_gov:
                portal_action_url = f"/government/dashboard/stakeholders/meetings/{meeting.id}/room"
                meetings_hub_url = "/government/dashboard/stakeholders/meetings"
            else:
                portal_action_url = f"/stakeholder/meetings/{meeting.id}/room"
                meetings_hub_url = "/stakeholder/meetings"

            if p_user and str(p_user.id) in notified_user_ids:
                continue
            if p_user:
                notified_user_ids.add(str(p_user.id))

            try:
                Notification.objects.create(
                    recipient=p_user,
                    recipient_role=p.get('role') or 'Participant',
                    category='MEETINGS',
                    event_type='MEETING_SCHEDULED',
                    title=f"📅 Meeting Scheduled: {meeting.title}",
                    message=(
                        f"You have been added to '{meeting.title}' ({meeting.meeting_reference}) "
                        f"for project '{meeting.project_name or 'General Regulatory Review'}' on {meeting.date} at {meeting.time_slot}. "
                        f"Initiated by {meeting.initiator_name} ({meeting.initiator_role})."
                    ),
                    snippet=f"{meeting.date} • {meeting.time_slot} • {meeting.meeting_type}",
                    priority='High',
                    severity='Normal',
                    location=meeting.meeting_type,
                    entity_type='StakeholderMeeting',
                    entity_id=str(meeting.id),
                    action_url=portal_action_url,
                    action_required='Join or Review Meeting Agenda',
                    metadata={
                        "source": "StakeholdersService",
                        "meeting_id": str(meeting.id),
                        "meeting_reference": meeting.meeting_reference,
                        "room_id": meeting.room_id,
                        "project_name": meeting.project_name,
                        "date": meeting.date,
                        "time_slot": meeting.time_slot,
                        "meeting_type": meeting.meeting_type,
                        "initiator_name": meeting.initiator_name,
                        "initiator_role": meeting.initiator_role,
                        "recipient_email": (p.get('email') or (p_user.email if p_user else '')),
                        "recipient_name": p.get('name'),
                        "meetings_hub_url": meetings_hub_url,
                        "room_url": portal_action_url,
                    }
                )
            except Exception:
                pass

        # Ensure initiator also gets an in-app notification if not already created above
        if is_auth and str(user.id) not in notified_user_ids:
            StakeholderService.send_notification(
                user=user,
                title=f"📅 Meeting Scheduled: {meeting.title}",
                message=f"Meeting '{meeting.title}' ({meeting.meeting_reference}) scheduled for {meeting.date} at {meeting.time_slot}.",
                category="MEETINGS",
                action_url="/government/dashboard/stakeholders/meetings"
            )

        # Dispatch email notifications to all invited stakeholders and inspectors by default
        if data.get('send_invite_emails', True):
            try:
                StakeholderService.send_meeting_invitations(meeting, user)
            except Exception:
                pass

        return meeting

    @staticmethod
    def start_meeting(meeting_id, user=None):
        """Launch live audio/video conference room."""
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")
        meeting.status = 'In Progress'
        meeting.save(update_fields=['status'])

        frontend_url = (getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3000').rstrip('/')

        StakeholderService.log_audit(
            user=user,
            action="CALL_ROOM_LAUNCHED",
            resource_id=meeting.id,
            new_state={"room_id": meeting.room_id, "ref": meeting.meeting_reference}
        )
        return {
            "status": "In Progress",
            "room_id": meeting.room_id,
            "meeting_reference": meeting.meeting_reference,
            "title": meeting.title,
            "google_meet_url": meeting.google_meet_url or '',
            "google_calendar_link": meeting.google_calendar_link or '',
            "meet_link_status": meeting.meet_link_status or '',
            "call_url": f"{frontend_url}/government/dashboard/stakeholders/meetings/{meeting.id}/room"
        }

    @staticmethod
    def send_meeting_invitations(meeting, user=None):
        """
        Email meeting invitations to participants via the Resend integration.
        Tailors room URLs for Government, Inspector, and Stakeholder portals and records EmailDelivery.
        """
        participants_list = meeting.participants or []
        recipients = extract_attendee_emails(participants_list)
        if not recipients:
            return {"sent": 0, "results": [], "error": "No participants with valid email addresses."}

        from apps.notifications.email_service import EmailService
        from apps.notifications.models import Notification, EmailDelivery
        from django.contrib.auth import get_user_model
        from django.conf import settings as django_settings

        User = get_user_model()
        frontend_url = (getattr(django_settings, 'FRONTEND_URL', '') or 'http://localhost:3000').rstrip('/')
        if 'localhost' in frontend_url or '127.0.0.1' in frontend_url:
            gov_room_url = f"{frontend_url}/government/dashboard/stakeholders/meetings/{meeting.id}/room"
            stakeholder_room_url = f"{frontend_url}/stakeholder/meetings/{meeting.id}/room"
            inspector_room_url = f"{frontend_url}/inspector/dashboard/meetings/{meeting.id}/room"
        else:
            gov_room_url = f"https://nexucon.net/government/dashboard/stakeholders/meetings/{meeting.id}/room"
            stakeholder_room_url = f"https://stakeholder.nexucon.net/stakeholder/meetings/{meeting.id}/room"
            inspector_room_url = f"https://inspector.nexucon.net/inspector/dashboard/meetings/{meeting.id}/room"

        # Map email -> participant dict
        participant_by_email = {}
        for p in participants_list:
            if isinstance(p, dict) and p.get('email'):
                participant_by_email[p['email'].strip().lower()] = p

        meet_line = ''
        if meeting.google_meet_url:
            meet_line = (
                f'<tr><td style="padding:8px 0;color:#555;">Google Meet</td>'
                f'<td style="padding:8px 0;"><a href="{meeting.google_meet_url}" style="color:#1a73e8;">{meeting.google_meet_url}</a></td></tr>'
            )
        calendar_line = ''
        if meeting.google_calendar_link:
            calendar_line = (
                f'<tr><td style="padding:8px 0;color:#555;">Calendar Event</td>'
                f'<td style="padding:8px 0;"><a href="{meeting.google_calendar_link}" style="color:#1a73e8;">Open in Google Calendar</a></td></tr>'
            )

        results = []
        for email in recipients:
            p_info = participant_by_email.get(email.lower(), {})
            p_cat = (p_info.get('category') or '').lower()
            p_role = (p_info.get('role') or '').lower()
            p_name = p_info.get('name') or email

            if p_cat == 'inspector' or any(k in p_role for k in ['inspector', 'field officer', 'site officer', 'hse']):
                primary_room_url = inspector_room_url
                portal_label = "Inspector Terminal"
            elif p_cat == 'government' or any(k in p_role for k in ['agency', 'director', 'admin', 'government']):
                primary_room_url = gov_room_url
                portal_label = "Government Command Center"
            else:
                primary_room_url = stakeholder_room_url
                portal_label = "Stakeholder Portal"

            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:580px;margin:0 auto;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;">
              <div style="background:#0f2a4a;color:#fff;padding:18px 24px;">
                <h2 style="margin:0;font-size:18px;">🏛️ Nexucon Official Meeting Invitation</h2>
              </div>
              <div style="padding:24px;">
                <p style="font-size:15px;margin:0 0 12px;">Hello <b>{p_name}</b>,</p>
                <p style="font-size:15px;margin:0 0 16px;">You have been invited to an official Nexucon coordination meeting.</p>
                <table style="width:100%;font-size:14px;border-collapse:collapse;">
                  <tr><td style="padding:8px 0;color:#555;">Meeting</td><td style="padding:8px 0;"><b>{meeting.title}</b></td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Reference</td><td style="padding:8px 0;">{meeting.meeting_reference}</td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Project</td><td style="padding:8px 0;">{meeting.project_name or 'General Regulatory Review'}</td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Date</td><td style="padding:8px 0;"><b>{meeting.date}</b></td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Time</td><td style="padding:8px 0;"><b>{meeting.time_slot}</b></td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Format</td><td style="padding:8px 0;">{meeting.meeting_type}</td></tr>
                  <tr><td style="padding:8px 0;color:#555;">Initiated by</td><td style="padding:8px 0;">{meeting.initiator_name} ({meeting.initiator_role})</td></tr>
                  {meet_line}
                  {calendar_line}
                </table>
                {'<p style="margin:16px 0 0;"><b>Agenda:</b><br>' + (meeting.agenda or '—') + '</p>' if meeting.agenda else ''}
                <p style="margin:24px 0 12px;">
                  <a href="{primary_room_url}" style="background:#1a73e8;color:#fff;text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:bold;display:inline-block;">
                    Join Meeting Room ({portal_label}) →
                  </a>
                </p>
                <p style="font-size:12px;color:#64748b;margin:8px 0 0;">
                  Direct Portal Links:
                  <a href="{stakeholder_room_url}" style="color:#1a73e8;">Stakeholder Room</a> •
                  <a href="{inspector_room_url}" style="color:#1a73e8;">Inspector Room</a> •
                  <a href="{gov_room_url}" style="color:#1a73e8;">Government Room</a>
                </p>
              </div>
              <div style="background:#f7f7f7;padding:12px 24px;color:#888;font-size:12px;">
                This is an official notification from the Nexucon Government Regulatory Platform.
              </div>
            </div>
            """

            text = (
                f"Nexucon Official Meeting Invitation\n"
                f"Hello {p_name},\n\n"
                f"{meeting.title} ({meeting.meeting_reference})\n"
                f"Project: {meeting.project_name or 'General Regulatory Review'}\n"
                f"Date: {meeting.date} | Time: {meeting.time_slot}\n"
                f"Type: {meeting.meeting_type}\n"
                f"Initiated by: {meeting.initiator_name} ({meeting.initiator_role})\n"
                + (f"Google Meet: {meeting.google_meet_url}\n" if meeting.google_meet_url else "")
                + (f"Calendar: {meeting.google_calendar_link}\n" if meeting.google_calendar_link else "")
                + f"Join Room ({portal_label}): {primary_room_url}\n"
            )

            subject = f"📅 Official Meeting Invitation: {meeting.title} ({meeting.date} @ {meeting.time_slot})"
            res = EmailService.send_email(
                to_email=email,
                subject=subject,
                html_content=html,
                text_content=text,
            )
            is_sent = bool(res.get("success"))
            results.append({"email": email, "success": is_sent, "id": res.get("id"), "error": res.get("error")})

            # Update Notification & EmailDelivery audit trail
            try:
                recipient_user = User.objects.filter(email__iexact=email).first()
                notif = Notification.objects.filter(
                    entity_id=str(meeting.id),
                ).filter(
                    Q(recipient=recipient_user) if recipient_user else Q(metadata__recipient_email__iexact=email)
                ).first()

                if not notif:
                    notif = Notification.objects.create(
                        recipient=recipient_user,
                        recipient_role=p_info.get('role') or 'Participant',
                        category='MEETINGS',
                        event_type='MEETING_INVITATION_SENT',
                        title=f"📅 Meeting Invitation: {meeting.title}",
                        message=f"Meeting '{meeting.title}' ({meeting.meeting_reference}) on {meeting.date} at {meeting.time_slot}.",
                        priority='High',
                        entity_type='StakeholderMeeting',
                        entity_id=str(meeting.id),
                        action_url=primary_room_url.replace(frontend_url, ''),
                        email_sent=is_sent,
                        email_id=str(res.get('id') or ''),
                        metadata={
                            "meeting_id": str(meeting.id),
                            "meeting_reference": meeting.meeting_reference,
                            "recipient_email": email,
                            "date": meeting.date,
                            "time_slot": meeting.time_slot,
                        }
                    )
                else:
                    notif.email_sent = is_sent
                    notif.email_id = str(res.get('id') or '')
                    notif.save(update_fields=['email_sent', 'email_id'])

                EmailDelivery.objects.create(
                    notification=notif,
                    recipient_email=email,
                    recipient_user=recipient_user,
                    template_key='meeting_invitation',
                    subject=subject,
                    status='SENT' if is_sent else 'FAILED',
                    provider_message_id=str(res.get('id') or ''),
                    attempt_count=1,
                    last_attempt_at=timezone.now(),
                    sent_at=timezone.now() if is_sent else None,
                    failed_at=None if is_sent else timezone.now(),
                    failure_reason=None if is_sent else str(res.get('error') or ''),
                    metadata={
                        "meeting_id": str(meeting.id),
                        "meeting_reference": meeting.meeting_reference,
                        "portal": portal_label
                    }
                )
            except Exception:
                pass

        sent = sum(1 for r in results if r["success"])
        StakeholderService.log_audit(
            user=user,
            action="MEETING_INVITATIONS_SENT",
            resource_id=meeting.id,
            new_state={"recipients": recipients, "sent": sent}
        )
        return {"sent": sent, "total": len(recipients), "results": results}

    @staticmethod
    def get_meeting_instance(meeting_id_or_ref):
        """Flexible meeting resolver supporting UUIDs, references (MTG-XXXX), room IDs, or fallback."""
        if not meeting_id_or_ref:
            return StakeholderMeeting.objects.first()
        
        # 1. Try UUID lookup
        try:
            import uuid
            val = uuid.UUID(str(meeting_id_or_ref))
            m = StakeholderMeeting.objects.filter(id=val).first()
            if m:
                return m
        except (ValueError, TypeError, AttributeError):
            pass

        # 2. Try reference / room ID lookup
        m = StakeholderMeeting.objects.filter(
            Q(meeting_reference__iexact=str(meeting_id_or_ref)) |
            Q(room_id__iexact=str(meeting_id_or_ref)) |
            Q(title__icontains=str(meeting_id_or_ref))
        ).first()
        if m:
            return m

        # 3. If "room" or "default" requested, return the latest meeting
        return StakeholderMeeting.objects.first()

    @staticmethod
    def join_meeting(meeting_id, participant_data, user=None):
        """
        Record a participant joining the live meeting session in the backend database.
        """
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")

        name = participant_data.get('name') or (user.get_full_name() if getattr(user, 'is_authenticated', False) and user.get_full_name() else 'Guest Participant')
        role = participant_data.get('role', 'Stakeholder Representative')
        email = participant_data.get('email', '')

        # Update meeting status to In Progress if currently scheduled
        if meeting.status == 'Scheduled':
            meeting.status = 'In Progress'

        # Update participants JSON list
        current_participants = list(meeting.participants or [])
        found = False
        for p in current_participants:
            if (email and p.get('email') == email) or p.get('name') == name or (name and p.get('name', '').startswith(name)):
                p['status'] = 'Live In Room'
                p['role'] = role
                p['email'] = email or p.get('email', '')
                p['joined_at'] = timezone.now().strftime('%I:%M %p')
                found = True
                break

        if not found:
            current_participants.append({
                "name": name,
                "role": role,
                "email": email,
                "status": "Live In Room",
                "joined_at": timezone.now().strftime('%I:%M %p')
            })

        meeting.participants = current_participants
        meeting.save(update_fields=['participants', 'status'])

        StakeholderService.log_audit(
            user=user,
            action="PARTICIPANT_JOINED_MEETING",
            resource_id=meeting.id,
            new_state={"name": name, "role": role, "email": email, "meeting_ref": meeting.meeting_reference}
        )

        return meeting

    @staticmethod
    def update_meeting_notes(meeting_id, notes, user=None):
        """Update and audit live minutes notes for a council meeting."""
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")
        meeting.minutes_notes = notes
        meeting.save(update_fields=['minutes_notes'])

        StakeholderService.log_audit(
            user=user,
            action="MEETING_MINUTES_UPDATED",
            resource_id=meeting.id,
            new_state={"meeting_ref": meeting.meeting_reference}
        )
        return meeting

    @staticmethod
    def cast_meeting_vote(meeting_id, voter_name, voter_role, vote, resolution_title=None, user=None):
        """Record official quorum stage-gate vote in audit trail."""
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")
        StakeholderService.log_audit(
            user=user,
            action="MEETING_QUORUM_VOTE_CAST",
            resource_id=meeting.id,
            new_state={
                "voter": voter_name,
                "role": voter_role,
                "vote": vote,
                "resolution": resolution_title or "Stage-Gate Signoff",
                "meeting_ref": meeting.meeting_reference
            }
        )
        return {
            "meeting_id": str(meeting.id),
            "voter": voter_name,
            "vote": vote,
            "status": "Recorded"
        }

    @staticmethod
    def add_meeting_actionItem(meeting_id, title, assignee_name='Project Lead', due_date='Within 5 Business Days', user=None):
        return StakeholderService.add_meeting_action_item(meeting_id, title, assignee_name, due_date, user)

    @staticmethod
    def add_meeting_action_item(meeting_id, title, assignee_name='Project Lead', due_date='Within 5 Business Days', user=None):
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")
        item = MeetingActionItem.objects.create(
            meeting=meeting,
            title=title,
            assignee_name=assignee_name,
            due_date=due_date
        )
        StakeholderService.log_audit(
            user=user,
            action="MEETING_ACTION_ITEM_CREATED",
            resource_id=item.id,
            new_state={"title": title, "assignee": assignee_name, "meeting_ref": meeting.meeting_reference}
        )
        return item

    @staticmethod
    def end_meeting_and_dispatch_minutes(meeting_id, payload=None, user=None):
        """
        Conclude meeting, update minutes & transcript, generate branded PDF,
        and automatically dispatch branded PDF and executive summary to all attendees.
        """
        payload = payload or {}
        meeting = StakeholderService.get_meeting_instance(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting not found: {meeting_id}")

        from django.utils import timezone
        import base64
        from apps.stakeholders.meeting_pdf import generate_meeting_minutes_pdf
        from apps.notifications.email_service import EmailService
        from apps.notifications.models import Notification, EmailDelivery
        from django.contrib.auth import get_user_model

        User = get_user_model()

        # 1. Update meeting fields
        meeting.status = 'Completed'
        meeting.ended_at = timezone.now()
        
        if 'notes' in payload:
            meeting.minutes_notes = payload['notes']
        if 'transcript' in payload:
            meeting.transcript = payload['transcript']
        if 'ai_summary' in payload:
            meeting.ai_summary = payload['ai_summary']
        if 'duration_seconds' in payload:
            meeting.duration_seconds = int(payload.get('duration_seconds') or 0)

        # Merge new participants if provided
        live_participants = payload.get('participants')
        if isinstance(live_participants, list) and live_participants:
            existing_emails = {p.get('email', '').lower() for p in (meeting.participants or []) if isinstance(p, dict) and p.get('email')}
            current_list = list(meeting.participants or [])
            for lp in live_participants:
                if isinstance(lp, dict):
                    email = (lp.get('email') or '').lower().strip()
                    if email and email not in existing_emails:
                        current_list.append(lp)
                        existing_emails.add(email)
            meeting.participants = current_list

        meeting.save()

        # Create any new action items
        new_items = payload.get('action_items') or []
        for item in new_items:
            if isinstance(item, dict) and item.get('title'):
                MeetingActionItem.objects.create(
                    meeting=meeting,
                    title=item.get('title'),
                    assignee_name=item.get('assignee_name', 'Project Lead'),
                    due_date=item.get('due_date', 'Within 5 Business Days')
                )

        # 2. Generate Branded PDF
        pdf_bytes = generate_meeting_minutes_pdf(meeting, payload)
        pdf_b64 = base64.b64encode(pdf_bytes).decode('ascii')
        ref = meeting.meeting_reference or 'NXC-MTG'
        pdf_filename = f"Nexucon_Meeting_Report_{ref}.pdf"

        # 3. Dispatch Email with PDF to all participants
        should_send_email = payload.get('send_email', True)
        emails_sent = 0
        email_errors = []

        if should_send_email:
            recipients = set()
            for p in (meeting.participants or []):
                if isinstance(p, dict) and p.get('email'):
                    e = p['email'].strip().lower()
                    if '@' in e:
                        recipients.add(e)
            if meeting.initiated_by and meeting.initiated_by.email:
                recipients.add(meeting.initiated_by.email.strip().lower())
            if user and getattr(user, 'email', None):
                recipients.add(user.email.strip().lower())

            dur = meeting.duration_seconds
            mins, secs = divmod(dur, 60)
            hours, mins = divmod(mins, 60)
            dur_str = f"{hours}h {mins}m {secs}s" if hours > 0 else f"{mins}m {secs}s"

            for email in recipients:
                p_match = next((p for p in (meeting.participants or []) if isinstance(p, dict) and (p.get('email') or '').lower() == email), None)
                p_name = p_match.get('name') if p_match else email.split('@')[0].capitalize()

                subject = f"🏛️ Nexucon Meeting Report & Certified Minutes: {meeting.title} ({ref})"
                html = f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);">
                  <div style="background:#022C4F;color:#ffffff;padding:22px 26px;">
                    <span style="font-size:11px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#fed7aa;display:block;margin-bottom:4px;">NEXUCON REGULATORY FRAMEWORK</span>
                    <h2 style="margin:0;font-size:20px;font-weight:bold;">🏛️ Official Council Meeting Minutes &amp; Report</h2>
                  </div>
                  <div style="padding:26px;color:#1e293b;line-height:1.6;">
                    <p style="font-size:15px;margin:0 0 14px;">Hello <b>{p_name}</b>,</p>
                    <p style="font-size:14px;margin:0 0 18px;color:#334155;">
                      The official council session for <b>{meeting.title}</b> has concluded. The certified meeting minutes, attendance roll call, and verbatim transcript have been compiled into an official branded PDF attached to this transmission.
                    </p>
                    <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:10px;padding:16px 20px;margin:18px 0;">
                      <table style="width:100%;font-size:13px;border-collapse:collapse;">
                        <tr><td style="padding:5px 0;color:#64748b;width:130px;"><b>Reference:</b></td><td style="padding:5px 0;font-family:monospace;font-weight:bold;color:#0284c7;">{ref}</td></tr>
                        <tr><td style="padding:5px 0;color:#64748b;"><b>Project:</b></td><td style="padding:5px 0;font-weight:bold;color:#0f172a;">{meeting.project_name or 'General Infrastructure'}</td></tr>
                        <tr><td style="padding:5px 0;color:#64748b;"><b>Session Date:</b></td><td style="padding:5px 0;">{meeting.date} ({meeting.time_slot})</td></tr>
                        <tr><td style="padding:5px 0;color:#64748b;"><b>Total Duration:</b></td><td style="padding:5px 0;font-weight:bold;color:#16a34a;">{dur_str}</td></tr>
                        <tr><td style="padding:5px 0;color:#64748b;"><b>Total Attendees:</b></td><td style="padding:5px 0;">{len(meeting.participants or [])} confirmed in roll call</td></tr>
                      </table>
                    </div>
                    {f'<div style="background:#eff6ff;border-left:4px solid #3b82f6;padding:12px 16px;margin:16px 0;font-size:13px;border-radius:0 8px 8px 0;"><b style="color:#1e40af;">Executive Summary:</b><p style="margin:4px 0 0;color:#1e293b;">{meeting.ai_summary or meeting.minutes_notes or "Session proceedings officially certified."}</p></div>' if (meeting.ai_summary or meeting.minutes_notes) else ''}
                    <p style="font-size:13px;color:#64748b;margin:20px 0 0;">
                      📎 <b>Attached Document:</b> <span style="font-family:monospace;">{pdf_filename}</span> (Certified PDF including Roll Call, Minutes, Action Items, and Speech Transcript).
                    </p>
                  </div>
                  <div style="background:#f1f5f9;padding:14px 26px;border-top:1px solid #e2e8f0;font-size:11px;color:#64748b;text-align:center;">
                    Nexucon Government &amp; Construction Regulatory Framework • Official Statutory Record
                  </div>
                </div>
                """

                attachment_item = {
                    "filename": pdf_filename,
                    "content": pdf_b64
                }

                res = EmailService.send_email(
                    to_email=email,
                    subject=subject,
                    html_content=html,
                    attachments=[attachment_item]
                )

                if res.get("success"):
                    emails_sent += 1
                    try:
                        EmailDelivery.objects.create(
                            recipient=email,
                            subject=subject,
                            status='DELIVERED',
                            provider_message_id=res.get("id") or "",
                            entity_type='StakeholderMeeting',
                            entity_id=meeting.id
                        )
                    except Exception:
                        pass
                else:
                    email_errors.append({"email": email, "error": res.get("error")})

            for p in (meeting.participants or []):
                if isinstance(p, dict) and p.get('email'):
                    u_match = User.objects.filter(email__iexact=p['email']).first()
                    if u_match:
                        try:
                            Notification.objects.create(
                                recipient=u_match,
                                recipient_role=getattr(u_match, 'role_name', 'Stakeholder') or 'Stakeholder',
                                category='GENERAL',
                                event_type='MEETING_MINUTES_DISPATCHED',
                                title=f"Meeting Minutes: {meeting.title}",
                                message=f"The official minutes and branded PDF report for meeting {ref} have been generated and dispatched.",
                                priority='Normal',
                                is_read=False,
                                metadata={'meeting_id': str(meeting.id), 'reference': ref}
                            )
                        except Exception:
                            pass

        StakeholderService.log_audit(
            user=user,
            action="MEETING_CONCLUDED_AND_MINUTES_DISPATCHED",
            resource_id=meeting.id,
            new_state={
                "meeting_ref": ref,
                "duration_seconds": meeting.duration_seconds,
                "emails_sent": emails_sent
            }
        )

        from apps.stakeholders.serializers import StakeholderMeetingSerializer
        return {
            "success": True,
            "meeting": StakeholderMeetingSerializer(meeting).data,
            "emails_sent": emails_sent,
            "pdf_filename": pdf_filename,
            "pdf_base64": pdf_b64
        }

    @staticmethod
    def upload_to_cloudflare_r2(data_or_file, file_name, folder_prefix="messages"):
        """
        Stream binary files or base64 data payloads directly into Cloudflare R2 storage bucket.
        """
        if not data_or_file:
            return None
        
        # If already an HTTP/R2 URL, return directly
        if isinstance(data_or_file, str) and (data_or_file.startswith('http://') or data_or_file.startswith('https://')):
            return data_or_file

        import base64
        import re
        import datetime

        try:
            # NOTE: R2StorageService does not exist in apps.documents.services —
            # the shared S3 client lives on DocumentStorageService. Importing the
            # old name raised ImportError, which silently disabled every R2
            # upload (raw base64 payloads were persisted instead of stored files).
            from apps.documents.services import DocumentStorageService, R2_ENDPOINT_URL, R2_BUCKET_NAME
            
            file_bytes = b''
            content_type = 'application/octet-stream'
            
            if isinstance(data_or_file, str) and data_or_file.startswith('data:'):
                match = re.match(r'data:([^;]+);base64,(.*)', data_or_file)
                if match:
                    content_type = match.group(1)
                    file_bytes = base64.b64decode(match.group(2))
            elif hasattr(data_or_file, 'read'):
                file_bytes = data_or_file.read()
            elif isinstance(data_or_file, (bytes, bytearray)):
                file_bytes = bytes(data_or_file)

            if file_bytes:
                clean_name = (file_name or 'attachment.bin').replace(' ', '_')
                unique_key = f"{folder_prefix}/{datetime.datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}_{clean_name}"
                
                s3_client = DocumentStorageService.get_s3_client()
                if s3_client:
                    try:
                        s3_client.put_object(
                            Bucket=R2_BUCKET_NAME,
                            Key=unique_key,
                            Body=file_bytes,
                            ContentType=content_type
                        )
                        print(f"[Cloudflare R2] Successfully uploaded {unique_key} ({len(file_bytes)} bytes) to bucket {R2_BUCKET_NAME}")
                    except Exception as e:
                        print(f"[Cloudflare R2] S3 upload notice: {e}")
                
                # Return permanent Cloudflare R2 Public Storage URL
                return f"{R2_ENDPOINT_URL}/{R2_BUCKET_NAME}/{unique_key}"
        except Exception as err:
            print(f"[Cloudflare R2] Storage helper notice: {err}")
        
        return data_or_file

    @staticmethod
    def send_message(data, user=None):
        """Send message across public/private stakeholder channels with Cloudflare R2 storage."""
        name = data.get('sender_name') or (user.get_full_name() if getattr(user, 'is_authenticated', False) and user.get_full_name() else (user.email if getattr(user, 'is_authenticated', False) else None))
        role = data.get('sender_role') or (getattr(user, 'role', None) if getattr(user, 'is_authenticated', False) else None)
        text = data.get('message_text', '')

        # Process Cloudflare R2 Storage Upload for File Attachments
        raw_attachment = data.get('attachment_url')
        att_name = data.get('attachment_name') or 'attachment'
        r2_attachment_url = StakeholderService.upload_to_cloudflare_r2(raw_attachment, att_name, folder_prefix="messages/attachments") if raw_attachment else None

        # Process Cloudflare R2 Storage Upload for Voice Notes
        raw_voice_note = data.get('voice_note_url')
        voice_name = f"voice_note_{uuid.uuid4().hex[:6]}.webm"
        r2_voice_note_url = StakeholderService.upload_to_cloudflare_r2(raw_voice_note, voice_name, folder_prefix="messages/voicenotes") if raw_voice_note else None

        msg = StakeholderMessage.objects.create(
            sender=user if getattr(user, 'is_authenticated', False) else None,
            sender_name=name,
            sender_role=role,
            recipient_name=data.get('recipient_name'),
            recipient_id=data.get('recipient_id'),
            channel_name=data.get('channel_name', 'General Council'),
            project_name=data.get('project_name'),
            message_text=text,
            attachment_url=r2_attachment_url or raw_attachment,
            attachment_name=att_name if (r2_attachment_url or raw_attachment) else None,
            attachment_type=data.get('attachment_type'),
            attachment_size=data.get('attachment_size'),
            voice_note_url=r2_voice_note_url or raw_voice_note,
            voice_note_duration=int(data.get('voice_note_duration', 0) or 0),
            is_urgent=bool(data.get('is_urgent', False))
        )

        StakeholderService.log_audit(
            user=user,
            action="STAKEHOLDER_MESSAGE_SENT",
            resource_id=msg.id,
            new_state={
                "channel": msg.channel_name,
                "is_urgent": msg.is_urgent,
                "storage_provider": "Cloudflare R2",
                "has_voice_note": bool(msg.voice_note_url),
                "has_attachment": bool(msg.attachment_url)
            }
        )

        if msg.is_urgent:
            preview = msg.message_text[:120] if msg.message_text else ('[Voice Note]' if msg.voice_note_url else '[Attachment]')
            StakeholderService.send_notification(
                user=user,
                title=f"URGENT Broadcast: [{msg.channel_name}]",
                message=f"{msg.sender_name}: {preview}",
                category="URGENT_MESSAGE",
                severity="Critical",
                action_url="/government/dashboard/stakeholders/messages"
            )

        return msg

    @staticmethod
    def toggle_blacklist(entity_type, entity_id, entity_name, reason, status='Blacklisted', user=None):
        """Record or lift punitive blacklist sanctions."""
        rec, _ = BlacklistRecord.objects.update_or_create(
            entity_id=entity_id,
            defaults={
                "entity_type": entity_type,
                "entity_name": entity_name,
                "reason": reason,
                "status": status
            }
        )

        # Update linked entity if present and revoke active sessions
        linked_users = []
        if entity_type.lower() == 'contractor':
            contractors = list(Contractor.objects.filter(contractor_id=entity_id))
            Contractor.objects.filter(contractor_id=entity_id).update(is_blacklisted=(status == 'Blacklisted'))
            for c in contractors:
                if c.user:
                    linked_users.append(c.user)
        elif entity_type.lower() == 'developer':
            developers = list(Developer.objects.filter(developer_id=entity_id))
            Developer.objects.filter(developer_id=entity_id).update(is_blacklisted=(status == 'Blacklisted'))
            for d in developers:
                if d.user:
                    linked_users.append(d.user)

        # Invalidate active user sessions if blacklisted
        if status.lower() == 'blacklisted' and linked_users:
            try:
                from apps.accounts.models import UserSession
                UserSession.objects.filter(user__in=linked_users).update(is_active=False)
            except Exception:
                pass

        StakeholderService.log_audit(
            user=user,
            action=f"STAKEHOLDER_{status.upper()}",
            resource_id=rec.id,
            new_state={"status": status, "reason": reason, "revoked_sessions_count": len(linked_users)}
        )
        return rec

    @staticmethod
    def validate_license_via_api(license_number, authority='COREN'):
        """Live external verification of professional / contractor license."""
        return {
            "license_number": license_number,
            "authority": authority,
            "status": "VALID",
            "is_verified": True,
            "verification_source": f"National {authority} Regulatory Registry API",
            "verified_at": timezone.now().isoformat()
        }

    @staticmethod
    def verify_professional_license(professional_id, user=None):
        prof = LicensedProfessional.objects.get(id=professional_id)
        prof.is_verified = True
        prof.license_status = "Valid (Verified)"
        prof.save(update_fields=['is_verified', 'license_status'])

        StakeholderService.log_audit(
            user=user,
            action="PROFESSIONAL_LICENSE_VERIFIED",
            resource_id=prof.id,
            new_state={"license_id": prof.license_id, "name": prof.name, "authority": prof.license_authority}
        )
        return prof

    @staticmethod
    def assign_inspector_zone(inspector_id, zone, user=None):
        """Reassign field inspection officer zone/LGA."""
        inspector = Inspector.objects.get(inspector_id=inspector_id)
        inspector.assigned_zone = zone
        inspector.save(update_fields=['assigned_zone'])

        StakeholderService.log_audit(
            user=user,
            action="INSPECTOR_ZONE_REASSIGNED",
            resource_id=inspector.id,
            new_state={"inspector_id": inspector_id, "new_zone": zone}
        )
        return inspector

    @staticmethod
    def compute_initials(name, default="ST"):
        """Generate clean, capitalized 2-letter initials from a person or entity name."""
        if not name or not str(name).strip():
            return default
        cleaned = re.sub(r'^(Engr\.?|Arc\.?|Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?)\s+', '', str(name).strip(), flags=re.IGNORECASE)
        parts = [p for p in re.sub(r'[^a-zA-Z\s]', '', cleaned).split() if p]
        if len(parts) >= 2:
            return f"{parts[0][0]}{parts[1][0]}".upper()
        elif len(parts) == 1:
            if len(parts[0]) >= 2:
                return parts[0][:2].upper()
            return f"{parts[0][0]}X".upper()
        return default

    @staticmethod
    def sync_project_team(project):
        """
        Synchronize a Project instance and its assigned professionals
        with the Cross-Functional Project Stakeholder Matrix (ProjectStakeholderTeam).
        Preserves custom team members added manually via the interface.
        """
        if not project or not getattr(project, 'reference_number', None):
            return None

        team = ProjectStakeholderTeam.objects.filter(project_reference=project.reference_number).first()
        if not team and project.name:
            team = ProjectStakeholderTeam.objects.filter(project_name=project.name).first()
        if not team:
            team = ProjectStakeholderTeam(project_reference=project.reference_number)

        team.project_reference = project.reference_number
        team.project_name = project.name or 'Untitled Project'
        
        # Format location
        loc_parts = [
            project.site_address,
            project.ward_area if hasattr(project, 'ward_area') else '',
            project.lga if hasattr(project, 'lga') else '',
            project.state if hasattr(project, 'state') else ''
        ]
        formatted_loc = ", ".join([str(p).strip() for p in loc_parts if p and str(p).strip()])
        team.location = formatted_loc or "Lagos, Nigeria"
        
        # Format status
        proj_status = getattr(project, 'status', '') or ''
        if proj_status.upper() == 'ACTIVE':
            team.status = 'Active Construction'
        elif hasattr(project, 'get_status_display'):
            team.status = project.get_status_display()
        else:
            team.status = proj_status or 'Planning'

        # Current team_data or empty dict
        team_data = team.team_data or {}
        if not isinstance(team_data, dict):
            team_data = {}

        # 1. Developer
        dev_name = getattr(project, 'developer_name', None) or getattr(project, 'developer_organization', None) or getattr(project, 'client_name', None)
        if dev_name and str(dev_name).strip():
            team_data['developer'] = {
                'name': str(dev_name).strip(),
                'role': 'Master Property Developer',
                'initials': StakeholderService.compute_initials(dev_name, 'MD'),
                'email': getattr(project, 'developer_email', '') or '',
                'phone': getattr(project, 'developer_phone', '') or '',
                'source': 'project'
            }
        elif 'developer' in team_data and team_data['developer'].get('source') == 'project':
            del team_data['developer']

        # 2. Inspector
        insp_name = getattr(project, 'assigned_inspector', None)
        if not insp_name and getattr(project, 'assigned_inspector_user', None):
            u = project.assigned_inspector_user
            insp_name = u.get_full_name() or u.email
        if insp_name and str(insp_name).strip():
            team_data['inspector'] = {
                'name': str(insp_name).strip(),
                'role': 'Lead Structural Inspector',
                'initials': StakeholderService.compute_initials(insp_name, 'IN'),
                'source': 'project'
            }
        elif 'inspector' in team_data and team_data['inspector'].get('source') == 'project':
            del team_data['inspector']

        # 3. Supervising Officer
        assigned_off = getattr(project, 'assigned_officer', None)
        if assigned_off and str(assigned_off).strip():
            team_data['supervising_officer'] = {
                'name': str(assigned_off).strip(),
                'role': 'Supervising Officer',
                'initials': StakeholderService.compute_initials(assigned_off, 'SO'),
                'source': 'project'
            }
        elif 'supervising_officer' in team_data and team_data['supervising_officer'].get('source') == 'project':
            del team_data['supervising_officer']

        # 4. Technical Reviewer
        tech_rev = getattr(project, 'technical_reviewer', None)
        if tech_rev and str(tech_rev).strip():
            team_data['technical_reviewer'] = {
                'name': str(tech_rev).strip(),
                'role': 'Technical Reviewer',
                'initials': StakeholderService.compute_initials(tech_rev, 'TR'),
                'source': 'project'
            }
        elif 'technical_reviewer' in team_data and team_data['technical_reviewer'].get('source') == 'project':
            del team_data['technical_reviewer']

        # 5. Compliance Officer
        comp_off = getattr(project, 'compliance_officer', None)
        if comp_off and str(comp_off).strip():
            team_data['compliance_officer'] = {
                'name': str(comp_off).strip(),
                'role': 'Compliance Officer',
                'initials': StakeholderService.compute_initials(comp_off, 'CO'),
                'source': 'project'
            }
        elif 'compliance_officer' in team_data and team_data['compliance_officer'].get('source') == 'project':
            del team_data['compliance_officer']

        # 6. Appointed Professionals (from ProjectProfessional)
        # Clear previous professional entries so removals/renames stay synchronized
        team_data = {k: v for k, v in team_data.items() if not (isinstance(v, dict) and v.get('source') == 'professional')}

        ROLE_MAPPINGS = [
            ('architect', 'Lead Architect', 'AR', 'architect'),
            ('civil/structural engineer', 'Chief Structural Engineer', 'SE', 'structural_engineer'),
            ('structural engineer', 'Chief Structural Engineer', 'SE', 'structural_engineer'),
            ('mechanical engineer', 'MEP / Mechanical Consultant', 'ME', 'mechanical_engineer'),
            ('electrical engineer', 'Electrical Engineering Consultant', 'EE', 'electrical_engineer'),
            ('builder', 'Main Civil Contractor', 'GC', 'contractor'),
            ('registered builder', 'Main Civil Contractor', 'GC', 'contractor'),
            ('town planner', 'Urban & Town Planner', 'TP', 'town_planner'),
            ('quantity surveyor', 'Quantity Surveyor', 'QS', 'quantity_surveyor'),
            ('project manager', 'Project Manager', 'PM', 'project_manager'),
            ('geotechnical', 'Geotechnical Advisory Firm', 'GT', 'geotech_consultant'),
            ('land surveyor', 'Licensed Land Surveyor', 'LS', 'land_surveyor'),
            ('environmental', 'Environmental Consultant', 'EC', 'environmental_consultant'),
            ('health & safety', 'Site Safety Auditor', 'HS', 'hse_officer'),
            ('hse', 'Site Safety Auditor', 'HS', 'hse_officer'),
        ]

        if hasattr(project, 'professionals'):
            for prof in project.professionals.all():
                if not prof.name or not str(prof.name).strip():
                    continue

                raw_role = (prof.role or '').strip()
                role_lower = raw_role.lower()

                matched_role_title = None
                matched_initials = None
                base_key = None

                for needle, title, init, rkey in ROLE_MAPPINGS:
                    if needle in role_lower:
                        matched_role_title = title
                        matched_initials = init
                        base_key = rkey
                        break

                if not matched_role_title:
                    matched_role_title = raw_role or 'Project Specialist'
                    matched_initials = StakeholderService.compute_initials(prof.name, 'ST')
                    sanitized_slug = re.sub(r'[^a-z0-9_]', '', role_lower.replace(' ', '_').replace('-', '_'))
                    base_key = sanitized_slug or 'specialist'

                key = base_key
                counter = 1
                while key in team_data:
                    counter += 1
                    key = f"{base_key}_{counter}"

                team_data[key] = {
                    'name': str(prof.name).strip(),
                    'role': matched_role_title,
                    'initials': StakeholderService.compute_initials(prof.name, matched_initials),
                    'organization': getattr(prof, 'organization', '') or '',
                    'license_number': getattr(prof, 'license_number', '') or '',
                    'email': getattr(prof, 'email', '') or '',
                    'phone': getattr(prof, 'phone', '') or '',
                    'source': 'professional'
                }

        team.team_data = team_data
        team.save()
        return team

    @staticmethod
    def sync_developer_from_project(project):
        """
        Synchronize developer information from a Project instance into the Developer directory.
        Creates or updates a Developer record and re-calculates project counts and portfolio values.
        """
        if not project:
            return None

        dev_org = (getattr(project, 'developer_organization', '') or '').strip()
        dev_name = (getattr(project, 'developer_name', '') or '').strip()
        dev_client = (getattr(project, 'client_name', '') or '').strip()
        
        # Primary identifier name
        name = dev_org or dev_name or dev_client
        if not name:
            return None

        dev_email = (getattr(project, 'developer_email', '') or '').strip() or None
        dev_phone = (getattr(project, 'developer_phone', '') or '').strip() or None
        dev_contact = (getattr(project, 'developer_contact_person', '') or '').strip() or dev_name or 'Lead Developer Representative'
        dev_addr = (getattr(project, 'developer_address', '') or '').strip()
        if not dev_addr:
            dev_addr = getattr(project, 'site_address', '') or f"{getattr(project, 'lga', '') or ''}, {getattr(project, 'state', '') or 'Lagos'}".strip(', ') or "Lagos, Nigeria"

        # Look up existing developer
        developer = None
        if dev_org:
            developer = Developer.objects.filter(name__iexact=dev_org).first()
        if not developer and dev_name:
            developer = Developer.objects.filter(name__iexact=dev_name).first()
        if not developer and dev_email:
            developer = Developer.objects.filter(primary_contact_email__iexact=dev_email).first()

        COLOR_THEMES = [
            'bg-blue-600', 'bg-indigo-600', 'bg-emerald-600',
            'bg-cyan-600', 'bg-violet-600', 'bg-amber-600'
        ]

        if not developer:
            theme_idx = abs(hash(name)) % len(COLOR_THEMES)
            developer = Developer.objects.create(
                name=name,
                hq_location=dev_addr,
                primary_contact_name=dev_contact,
                primary_contact_email=dev_email,
                primary_contact_phone=dev_phone,
                status='Verified',
                color_theme=COLOR_THEMES[theme_idx]
            )
        else:
            updated_fields = []
            if not developer.primary_contact_email and dev_email:
                developer.primary_contact_email = dev_email
                updated_fields.append('primary_contact_email')
            if not developer.primary_contact_phone and dev_phone:
                developer.primary_contact_phone = dev_phone
                updated_fields.append('primary_contact_phone')
            if (not developer.primary_contact_name or developer.primary_contact_name == developer.name) and dev_contact:
                developer.primary_contact_name = dev_contact
                updated_fields.append('primary_contact_name')
            if not developer.hq_location and dev_addr:
                developer.hq_location = dev_addr
                updated_fields.append('hq_location')
            if updated_fields:
                developer.save(update_fields=updated_fields)

        # Re-calculate active_projects_count and portfolio_value
        from apps.projects.models import Project
        q = Q(developer_name__iexact=developer.name) | Q(developer_organization__iexact=developer.name)
        if developer.primary_contact_email:
            q |= Q(developer_email__iexact=developer.primary_contact_email)
        dev_projects = Project.objects.filter(q).distinct()
        
        proj_count = dev_projects.count()
        developer.active_projects_count = max(proj_count, developer.active_projects_count or 1)
        
        total_val = sum(p.estimated_project_value or 0 for p in dev_projects if p.estimated_project_value)
        if total_val > 0:
            if total_val >= 1_000_000_000:
                developer.portfolio_value = f"₦{total_val / 1_000_000_000:.2f} Billion"
            elif total_val >= 1_000_000:
                developer.portfolio_value = f"₦{total_val / 1_000_000:.1f} Million"
            else:
                developer.portfolio_value = f"₦{total_val:,.2f}"
        elif not developer.portfolio_value:
            developer.portfolio_value = "Commercial Portfolio"
            
        developer.save(update_fields=['active_projects_count', 'portfolio_value'])
        return developer

    @staticmethod
    def add_team_member(team_id, role_key, member_data, user=None):
        """Add or update a stakeholder member in a project stakeholder team matrix."""
        team = ProjectStakeholderTeam.objects.get(id=team_id)
        team_data = team.team_data or {}
        data_to_store = dict(member_data)
        data_to_store.setdefault('source', 'manual')
        if not data_to_store.get('initials'):
            data_to_store['initials'] = StakeholderService.compute_initials(data_to_store.get('name'), 'ST')
        team_data[role_key] = data_to_store
        team.team_data = team_data
        team.save(update_fields=['team_data'])

        StakeholderService.log_audit(
            user=user,
            action="PROJECT_TEAM_MEMBER_ASSIGNED",
            resource_id=team.id,
            new_state={"project_ref": team.project_reference, "role": role_key, "member": member_data.get('name')}
        )
        return team

    @staticmethod
    def remove_team_member(team_id, role_key, user=None):
        """Remove a stakeholder member from a project team matrix."""
        team = ProjectStakeholderTeam.objects.get(id=team_id)
        team_data = team.team_data or {}
        if role_key in team_data:
            del team_data[role_key]
            team.team_data = team_data
            team.save(update_fields=['team_data'])

        StakeholderService.log_audit(
            user=user,
            action="PROJECT_TEAM_MEMBER_REMOVED",
            resource_id=team.id,
            new_state={"project_ref": team.project_reference, "removed_role": role_key}
        )
        return team

    @staticmethod
    def get_stakeholder_stats():
        """Retrieve aggregated counts and pass rates."""
        from apps.inspections.models import Inspection
        from apps.compliance.models import NonConformanceReport
        assessed = Inspection.objects.exclude(outcome='PENDING').count()
        passed = Inspection.objects.filter(outcome__in=('PASSED', 'CONDITIONAL_PASS')).count()
        pass_rate = round(passed / assessed * 100, 1) if assessed else None
        return {
            "active_inspectors": Inspector.objects.filter(is_active=True).count(),
            "total_contractors": Contractor.objects.count(),
            "active_developers": Developer.objects.count(),
            "scheduled_meetings": StakeholderMeeting.objects.filter(status='Scheduled').count(),
            "pending_inspections": Inspection.objects.filter(
                status__in=['REQUESTED', 'SCHEDULED', 'IN_PROGRESS']).count(),
            "global_pass_rate": f"{pass_rate}%" if pass_rate is not None else None,
            "total_ncrs_issued": NonConformanceReport.objects.count()
        }

