import os
import logging
from django.utils import timezone
from django.template.loader import render_to_string
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from .models import Notification, EmailDelivery, NotificationPreference
from .providers.factory import EmailProviderFactory
from apps.audit.models import AuditEvent

logger = logging.getLogger(__name__)
User = get_user_model()

DEFAULT_FRONTEND_URL = getattr(settings, 'FRONTEND_URL', os.environ.get('FRONTEND_URL', 'https://nexucon-frontend-8x3a.vercel.app')).rstrip('/')

class NotificationService:
    @staticmethod
    def resolve_recipients(recipient=None, recipient_role='All'):
        """
        Resolve target recipients based on explicit user, role, or broadcast.
        """
        if recipient:
            return [recipient] if not isinstance(recipient, (list, tuple)) else recipient

        if recipient_role and recipient_role != 'All':
            # Check Government Profiles
            users = User.objects.filter(government_profile__role__name__icontains=recipient_role, is_active=True)
            if users.exists():
                return list(users)

        # Fallback to staff / directors
        return list(User.objects.filter(is_active=True, is_staff=True)[:5])

    @staticmethod
    def get_or_create_preferences(user):
        pref, _ = NotificationPreference.objects.get_or_create(user=user)
        return pref

    @staticmethod
    def dispatch_event(
        event_type: str,
        title: str,
        message: str,
        category: str = 'GENERAL',
        priority: str = 'Medium',
        recipient = None,
        recipient_role: str = 'All',
        entity_type: str = None,
        entity_id: str = None,
        action_url: str = None,
        location: str = None,
        metadata: dict = None,
        template_key: str = None
    ):
        """
        Core event dispatcher: In-App notification, Preference check, Email delivery, and Audit logging.
        """
        recipients = NotificationService.resolve_recipients(recipient, recipient_role)
        created_notifications = []

        formatted_action_url = action_url
        if formatted_action_url and formatted_action_url.startswith('/'):
            formatted_action_url = f"{DEFAULT_FRONTEND_URL}{formatted_action_url}"

        for r in recipients:
            # 1. Create In-App Notification
            notif = Notification.objects.create(
                recipient=r,
                recipient_role=recipient_role,
                category=category.upper(),
                event_type=event_type,
                title=title,
                message=message,
                snippet=message[:120] if message else '',
                priority=priority,
                location=location,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id else None,
                action_url=formatted_action_url,
                metadata=metadata or {}
            )
            created_notifications.append(notif)

            # 2. Check Notification & Email Preferences
            pref = NotificationService.get_or_create_preferences(r) if r else None
            should_send_email = True
            if pref:
                should_send_email = pref.is_email_allowed_for_category(category)

            # Emergency is unconditionally mandatory
            if category.upper() in ['EMERGENCY', 'CRITICAL']:
                should_send_email = True

            # 3. Route to Email Delivery Channel
            if should_send_email and r and getattr(r, 'email', None):
                idempotency_key = f"{event_type}:{entity_id or notif.id}:{r.id}"
                NotificationService.deliver_email(
                    notification=notif,
                    recipient_user=r,
                    recipient_email=r.email,
                    template_key=template_key or event_type.lower(),
                    subject=f"[{priority.upper()}] {title} - Nexucon Regulatory Authority",
                    context={
                        'notification_title': title,
                        'notification_message': message,
                        'category': category,
                        'priority': priority,
                        'project_name': (metadata or {}).get('project_name', 'Government Monitored Project'),
                        'reference': (metadata or {}).get('reference', notif.notification_reference),
                        'due_date': (metadata or {}).get('due_date'),
                        'assigned_to': (metadata or {}).get('assigned_to', r.get_full_name() or r.username),
                        'location': location,
                        'action_url': formatted_action_url,
                        'action_label': 'Open Record on Government Dashboard →'
                    },
                    idempotency_key=idempotency_key
                )

            # 4. Audit Trail
            try:
                AuditEvent.objects.create(
                    user=r,
                    action="NOTIFICATION_DISPATCHED",
                    resource_type="Notification",
                    resource_id=str(notif.id),
                    new_state={"event": event_type, "category": category, "ref": notif.notification_reference}
                )
            except Exception:
                pass

        return created_notifications[0] if len(created_notifications) == 1 else created_notifications

    @staticmethod
    def deliver_email(
        notification: Notification,
        recipient_user,
        recipient_email: str,
        template_key: str,
        subject: str,
        context: dict,
        idempotency_key: str = None
    ) -> EmailDelivery:
        """
        Renders templates, checks idempotency, and delivers via active EmailProvider.
        """
        # Deduplication check
        if idempotency_key:
            existing = EmailDelivery.objects.filter(idempotency_key=idempotency_key).first()
            if existing and existing.status in ['SENT', 'DELIVERED']:
                logger.info(f"Duplicate email prevented for idempotency key: {idempotency_key}")
                return existing

        # Render HTML & Plain-text templates
        template_candidates = [
            f"emails/{template_key}.html",
            "emails/base_notification.html"
        ]
        html_content = ""
        for t in template_candidates:
            try:
                html_content = render_to_string(t, context)
                break
            except Exception:
                continue

        text_content = ""
        try:
            text_content = render_to_string("emails/base_notification.txt", context)
        except Exception:
            text_content = f"{context.get('notification_title')}\n{context.get('notification_message')}\nAction: {context.get('action_url')}"

        delivery = EmailDelivery.objects.create(
            notification=notification,
            recipient_email=recipient_email,
            recipient_user=recipient_user,
            template_key=template_key,
            subject=subject,
            status='PROCESSING',
            attempt_count=1,
            last_attempt_at=timezone.now(),
            idempotency_key=idempotency_key,
            metadata=context
        )

        provider = EmailProviderFactory.get_provider()
        res = provider.send_email(
            to_email=recipient_email,
            subject=subject,
            html_content=html_content,
            text_content=text_content
        )

        if res.get('success'):
            delivery.status = 'SENT'
            delivery.provider_message_id = res.get('id')
            delivery.sent_at = timezone.now()
            delivery.save()
            notification.email_sent = True
            notification.email_id = res.get('id')
            notification.save()
        else:
            delivery.status = 'FAILED'
            delivery.failure_reason = str(res.get('error'))
            delivery.failed_at = timezone.now()
            delivery.save()

        return delivery

    # --- Domain Helper Methods ---

    @staticmethod
    def notify_new_application(application, applicant_name="Applicant"):
        return NotificationService.dispatch_event(
            event_type="APPLICATION_SUBMITTED",
            title=f"New Permit Application: {application.reference_number}",
            message=f"{applicant_name} submitted a new statutory building permit application for review.",
            category="APPLICATIONS",
            priority="High",
            recipient_role="Director",
            entity_type="Application",
            entity_id=str(application.id),
            action_url="/government/dashboard/applications",
            metadata={"project_name": getattr(application, 'project_name', 'Commercial Tower'), "reference": application.reference_number}
        )

    @staticmethod
    def notify_inspection_requested(inspection):
        return NotificationService.dispatch_event(
            event_type="INSPECTION_REQUESTED",
            title=f"Inspection Requested: {inspection.inspection_reference}",
            message=f"Site inspection requested for {inspection.project.name if inspection.project else 'Monitored Site'}.",
            category="INSPECTIONS",
            priority="Medium",
            recipient_role="Inspector",
            entity_type="Inspection",
            entity_id=str(inspection.id),
            action_url="/government/dashboard/inspections/requests",
            metadata={"project_name": inspection.project.name if inspection.project else 'Site', "reference": inspection.inspection_reference}
        )

    @staticmethod
    def notify_approval_required(approval_request):
        return NotificationService.dispatch_event(
            event_type="APPROVAL_REQUIRED",
            title=f"Technical Approval Required: {approval_request.approval_reference}",
            message=f"{approval_request.title} requires formal regulatory sign-off.",
            category="APPROVALS",
            priority="High",
            recipient=approval_request.assigned_to,
            recipient_role="Director",
            entity_type="ApprovalRequest",
            entity_id=str(approval_request.id),
            action_url="/government/dashboard/approvals/pending",
            metadata={"reference": approval_request.approval_reference, "due_date": str(approval_request.due_date) if approval_request.due_date else None}
        )

    @staticmethod
    def notify_compliance_ncr(ncr):
        return NotificationService.dispatch_event(
            event_type="NCR_CREATED",
            title=f"Non-Conformance Flagged: {ncr.ncr_reference}",
            message=f"Severity {ncr.severity} infraction logged for {ncr.project.name}: {ncr.title}",
            category="COMPLIANCE",
            priority="Critical" if ncr.severity == 'Critical' else "High",
            recipient_role="Inspector",
            entity_type="NonConformanceReport",
            entity_id=str(ncr.id),
            action_url="/government/dashboard/compliance/non-conformances",
            metadata={"project_name": ncr.project.name, "reference": ncr.ncr_reference}
        )

    @staticmethod
    def notify_emergency_dispatch(title: str, message: str, location: str = "Lagos Central"):
        return NotificationService.dispatch_event(
            event_type="EMERGENCY_DISPATCH",
            title=f"🚨 EMERGENCY ALERT: {title}",
            message=message,
            category="EMERGENCY",
            priority="Critical",
            recipient_role="All",
            location=location,
            action_url="/government/dashboard/notifications/emergency",
            metadata={"location": location, "emergency": True}
        )

    @staticmethod
    def notify_overdue_action(title: str, message: str, action_url: str = None, due_date: str = None):
        return NotificationService.dispatch_event(
            event_type="ACTION_OVERDUE",
            title=f"Overdue Action Warning: {title}",
            message=message,
            category="OVERDUE",
            priority="High",
            recipient_role="All",
            action_url=action_url or "/government/dashboard/notifications/overdue",
            metadata={"due_date": due_date}
        )

    @staticmethod
    def notify_critical_issue(title: str, message: str, entity_type: str = None, entity_id: str = None, action_url: str = None):
        return NotificationService.dispatch_event(
            event_type="CRITICAL_ISSUE",
            title=f"Critical Structural Issue: {title}",
            message=message,
            category="CRITICAL",
            priority="Critical",
            recipient_role="Director",
            entity_type=entity_type,
            entity_id=entity_id,
            action_url=action_url or "/government/dashboard/notifications/critical"
        )
