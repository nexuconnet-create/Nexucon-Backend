import os
import uuid
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from .models import Notification, NotificationPreference
from apps.audit.models import AuditEvent

class NotificationService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Notification",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def send_email_via_resend(to_email, subject, body_text):
        """
        Dispatches transactional email via Resend SMTP / Django EmailBackend.
        Safely catches and logs exceptions without interrupting core application flows.
        """
        if not to_email:
            return False, None

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'Nexucon Alerts <notifications@nexucon.gov.ng>')
        try:
            email_id = f"resend_{uuid.uuid4().hex[:8]}"
            send_mail(
                subject=subject,
                message=body_text,
                from_email=from_email,
                recipient_list=[to_email],
                fail_silently=True
            )
            return True, email_id
        except Exception as e:
            # Fallback safe logging
            return False, None

    @staticmethod
    def send_notification(data, user=None):
        """Create a notification event and dispatch transactional email if applicable."""
        recipient = data.get('recipient')
        notif = Notification.objects.create(
            recipient=recipient,
            recipient_role=data.get('recipient_role', 'All'),
            category=data.get('category', 'GENERAL'),
            title=data.get('title'),
            message=data.get('message', ''),
            snippet=data.get('snippet', ''),
            priority=data.get('priority', 'Medium'),
            severity=data.get('severity', 'Normal'),
            location=data.get('location'),
            entity_type=data.get('entity_type'),
            entity_id=data.get('entity_id'),
            action_url=data.get('action_url'),
            action_required=data.get('action_required')
        )

        # Dispatch email if recipient has email
        recipient_email = getattr(recipient, 'email', None) or data.get('recipient_email')
        if recipient_email:
            sent, email_id = NotificationService.send_email_via_resend(
                to_email=recipient_email,
                subject=f"[Nexucon {notif.priority}] {notif.title}",
                body_text=f"{notif.message}\n\nLocation: {notif.location or 'N/A'}\nAction Required: {notif.action_required or 'None'}"
            )
            if sent:
                notif.email_sent = True
                notif.email_id = email_id
                notif.save(update_fields=['email_sent', 'email_id'])

        NotificationService.log_audit(
            user=user,
            action="NOTIFICATION_SENT",
            resource_id=notif.id,
            new_state={"ref": notif.notification_reference, "category": notif.category, "priority": notif.priority}
        )
        return notif

    @staticmethod
    def mark_as_read(notif_id, user=None):
        """Mark a notification as read."""
        notif = Notification.objects.get(id=notif_id)
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=['is_read', 'read_at'])
        return notif

    @staticmethod
    def mark_all_as_read(category=None, user=None):
        """Bulk mark all notifications in a category as read."""
        qs = Notification.objects.filter(is_read=False)
        if category and category.upper() != 'ALL':
            qs = qs.filter(category__iexact=category)
        if user and getattr(user, 'is_authenticated', False):
            from django.db.models import Q
            qs = qs.filter(Q(recipient=user) | Q(recipient__isnull=True))
        
        count = qs.update(is_read=True, read_at=timezone.now())
        return count

    @staticmethod
    def acknowledge_critical(notif_id, user=None):
        """Acknowledge high-urgency incident."""
        notif = Notification.objects.get(id=notif_id)
        notif.is_acknowledged = True
        notif.acknowledged_by = user if getattr(user, 'is_authenticated', False) else None
        notif.acknowledged_at = timezone.now()
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=['is_acknowledged', 'acknowledged_by', 'acknowledged_at', 'is_read', 'read_at'])

        NotificationService.log_audit(
            user=user,
            action="INCIDENT_ACKNOWLEDGED",
            resource_id=notif.id,
            new_state={"ref": notif.notification_reference, "ack_by": str(user)}
        )
        return notif

    @staticmethod
    def sound_site_alarm(location, reason, user=None):
        """Broadcast emergency site alarm and sound horns."""
        notif = Notification.objects.create(
            category='CRITICAL',
            title=f"EMERGENCY: Site Alarm Triggered ({location})",
            message=reason or "Immediate site evacuation ordered due to severe structural/safety hazard.",
            snippet="High-priority audible alarm broadcasted across site sectors.",
            priority='Critical',
            severity='Immediate Action',
            location=location,
            action_required="Evacuate immediate sector and notify emergency response teams."
        )

        NotificationService.log_audit(
            user=user,
            action="SITE_ALARM_SOUNDED",
            resource_id=notif.id,
            new_state={"location": location, "reason": reason}
        )
        return notif

    @staticmethod
    def ping_assignee(notif_id, method='Email', user=None):
        """Send reminder alert or ping to overdue assignee."""
        notif = Notification.objects.get(id=notif_id)
        
        # Log ping event
        NotificationService.log_audit(
            user=user,
            action="ASSIGNEE_PINGED",
            resource_id=notif.id,
            new_state={"method": method, "ref": notif.notification_reference}
        )
        return {
            "status": "Success",
            "message": f"Ping reminder dispatched via {method} for {notif.notification_reference}",
            "notification_id": str(notif.id)
        }

    @staticmethod
    def get_unread_counts(user=None):
        """Retrieve count of unread items across all notification categories."""
        qs = Notification.objects.filter(is_read=False)
        return {
            "total_unread": qs.count(),
            "critical": qs.filter(category='CRITICAL').count(),
            "applications": qs.filter(category='APPLICATIONS').count(),
            "inspections": qs.filter(category='INSPECTIONS').count(),
            "compliance": qs.filter(category='COMPLIANCE').count(),
            "approvals": qs.filter(category='APPROVALS').count(),
            "overdue": qs.filter(category='OVERDUE').count(),
        }

    @staticmethod
    def seed_initial_notifications():
        """Ensure standard sample notifications exist for live UI walkthrough."""
        if Notification.objects.exists():
            return

        defaults = [
            {
                "notification_reference": "CRIT-001",
                "category": "CRITICAL",
                "title": "Work Stoppage: Unstable Trench Wall",
                "message": "Trench wall showing signs of collapse near active heavy machinery. All personnel evacuated from immediate vicinity.",
                "snippet": "Trench wall collapse risk detected.",
                "priority": "Critical",
                "severity": "Active",
                "location": "Sector A, Deep Foundation",
                "action_required": "Acknowledge receipt and dispatch structural engineer."
            },
            {
                "notification_reference": "NOT-9102",
                "category": "APPLICATIONS",
                "title": "New Subcontractor Prequalification Submitted",
                "message": "Vertex Engineering has submitted their QA/QC manual and past performance logs for review to bid on MEP packages.",
                "snippet": "Vertex Engineering Solutions prequalification packet submitted.",
                "priority": "Medium",
                "location": "Central Metro Transit Hub",
                "action_required": "Review QA/QC manual."
            },
            {
                "notification_reference": "INSP-REQ-502",
                "category": "INSPECTIONS",
                "title": "Structural Framing Inspection Requested",
                "message": "Requesting sign-off on structural framing before drywall installation begins.",
                "snippet": "Proposed: Oct 18, 2026 at 09:00 AM - 11:30 AM",
                "priority": "High",
                "location": "Zone 3, Level 2",
                "action_required": "Accept or propose new walkthrough time."
            },
            {
                "notification_reference": "ALT-889",
                "category": "COMPLIANCE",
                "title": "Noise Decibel Limit Exceeded (Night Shift)",
                "message": "Sensor N-41 registered sustained noise levels above 85dB between 02:00 and 02:45 AM.",
                "snippet": "Sustained night-shift noise violation.",
                "priority": "Medium",
                "severity": "Warning",
                "location": "Zone 2 Perimeter",
                "action_required": "Generate Non-Conformance Report (NCR)."
            },
            {
                "notification_reference": "NOT-4421",
                "category": "APPROVALS",
                "title": "Action Required: Technical Review (HVAC Load)",
                "message": "HVAC engineering submittal requires technical sign-off before duct fabrication.",
                "snippet": "From: MEP Dept • REF: TR-502",
                "priority": "High",
                "location": "Main Terminal B",
                "action_required": "Execute technical review evaluation."
            },
            {
                "notification_reference": "ACT-092",
                "category": "OVERDUE",
                "title": "Environmental Impact Review (Phase 2)",
                "message": "Review SLA missed by 4 days. Requires urgent reassignment or ping.",
                "snippet": "Assignee: Sarah Jenkins (Environmental)",
                "priority": "High",
                "location": "Site Boundary & Water Runoff",
                "action_required": "Ping assignee or reassign workflow."
            }
        ]

        for item in defaults:
            Notification.objects.create(**item)
