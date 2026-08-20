import datetime
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import Permit
from apps.audit.models import AuditEvent

class PermitService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Permit",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def renew_permit(permit, user, extension_months=12, notes=None):
        """Extend permit expiry date and log renewal."""
        if permit.status not in ['ACTIVE', 'EXPIRED']:
            raise ValidationError(f"Cannot renew a permit with status '{permit.status}'.")

        previous_expiry = permit.expiry_date
        # If expired, renew from today, otherwise from expiry_date
        base_date = max(permit.expiry_date, datetime.date.today())
        new_expiry = base_date + datetime.timedelta(days=extension_months * 30)

        permit.expiry_date = new_expiry
        permit.status = 'ACTIVE'
        permit.renewal_count += 1
        permit.last_renewal_date = datetime.date.today()
        if notes:
            permit.conditions = f"{permit.conditions or ''}\nRenewal Note: {notes}".strip()
        permit.save()

        # Update linked application if present
        if hasattr(permit, 'application'):
            app = permit.application
            app.status = 'RENEWED'
            app.save()

        PermitService.log_audit(
            user=user,
            action="PERMIT_RENEWED",
            resource_id=permit.id,
            previous_state={"expiry_date": str(previous_expiry), "status": permit.status},
            new_state={"expiry_date": str(new_expiry), "renewal_count": permit.renewal_count}
        )
        return permit

    @staticmethod
    def send_expiry_notice(permit, user):
        """Dispatch expiry reminder notification."""
        notice_data = {
            "permit_number": permit.permit_number,
            "project": permit.project.name,
            "expiry_date": str(permit.expiry_date),
            "sent_at": timezone.now().isoformat(),
            "sent_by": user.get_full_name() or user.email
        }
        PermitService.log_audit(
            user=user,
            action="PERMIT_EXPIRY_NOTICE_SENT",
            resource_id=permit.id,
            new_state=notice_data
        )
        return notice_data

    @staticmethod
    def suspend_permit(permit, user, reason):
        """Suspend permit (e.g., in event of stop-work order or safety breach)."""
        previous_status = permit.status
        permit.status = 'SUSPENDED'
        permit.save()

        PermitService.log_audit(
            user=user,
            action="PERMIT_SUSPENDED",
            resource_id=permit.id,
            previous_state={"status": previous_status},
            new_state={"status": "SUSPENDED", "reason": reason}
        )
        return permit

    @staticmethod
    def revoke_permit(permit, user, reason):
        """Permanently revoke permit."""
        previous_status = permit.status
        permit.status = 'REVOKED'
        permit.save()

        PermitService.log_audit(
            user=user,
            action="PERMIT_REVOKED",
            resource_id=permit.id,
            previous_state={"status": previous_status},
            new_state={"status": "REVOKED", "reason": reason}
        )
        return permit
