import os
import json
import urllib.request
import urllib.error
import urllib.parse
import logging
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


class EmailService:
    """
    Centralized Resend Email Service for Nexucon Government & Construction System.
    Dispatches HTML role invitations, registration verification OTPs, 2FA codes, and statutory alerts.
    """

    @classmethod
    def get_api_key(cls) -> str:
        return getattr(settings, 'RESEND_API_KEY', '') or os.environ.get('RESEND_API_KEY', '')

    @classmethod
    def get_from_email(cls) -> str:
        return getattr(
            settings,
            'RESEND_FROM_EMAIL',
            ''
        ) or os.environ.get(
            'RESEND_FROM_EMAIL',
            'Nexucon Notifications <notifications@nexucon.net>'
        )

    @classmethod
    def get_frontend_url(cls) -> str:
        return (
            getattr(settings, 'FRONTEND_URL', '')
            or os.environ.get('FRONTEND_URL', 'https://nexucon.net')
        ).rstrip('/')

    @classmethod
    def send_email(cls, to_email: str, subject: str, html_content: str, text_content: str = None, from_email: str = None) -> dict:
        """
        Send an email via the Resend REST API.
        """
        api_key = cls.get_api_key()
        if not api_key:
            logger.warning("Resend API key is missing. Email dispatch aborted.")
            return {"success": False, "error": "RESEND_API_KEY not configured"}

        sender = from_email or cls.get_from_email()
        payload = {
            "from": sender,
            "to": [to_email] if isinstance(to_email, str) else to_email,
            "subject": subject,
            "html": html_content,
        }
        if text_content:
            payload["text"] = text_content

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            RESEND_API_URL,
            data=data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'Nexucon-Backend/1.0'
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode('utf-8'))
                logger.info(f"Email sent successfully to {to_email} via Resend. ID: {resp_data.get('id')}")
                return {"success": True, "id": resp_data.get('id'), "data": resp_data}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"Resend HTTP Error ({e.code}) sending to {to_email}: {error_body}")
            return {"success": False, "error": error_body, "status_code": e.code}
        except Exception as ex:
            logger.error(f"Failed to dispatch email to {to_email}: {str(ex)}")
            return {"success": False, "error": str(ex)}

    @classmethod
    def send_verification_otp_email(cls, email: str, name: str, otp_code: str, expires_minutes: int = 15) -> dict:
        """
        Dispatch a branded email verification OTP code to activate newly registered accounts.
        """
        subject = f"✉️ {otp_code} is your Nexucon verification code"
        context = {
            'email': email,
            'name': name or email.split('@')[0].capitalize(),
            'otp_code': otp_code,
            'expires_minutes': expires_minutes,
            'current_year': timezone.now().year,
        }

        try:
            html_content = render_to_string('emails/email_verification.html', context)
        except Exception as e:
            logger.warning(f"Could not render email_verification.html, falling back to basic template: {e}")
            html_content = render_to_string('emails/two_factor_auth.html', context)

        return cls.send_email(
            to_email=email,
            subject=subject,
            html_content=html_content
        )

    @classmethod
    def send_invitation_email(cls, email: str, name: str, role: str, department: str = "Urban Planning", invite_token: str = None, invited_by=None, base_url: str = None, temp_password: str = None) -> dict:
        """
        Dispatch a tailored, role-specific HTML invitation email.
        """
        base = (base_url or cls.get_frontend_url()).rstrip('/')
        token = invite_token or "invite-token-sample"
        encoded_email = urllib.parse.quote(email)
        encoded_role = urllib.parse.quote(role or '')
        invite_url = f"{base}/auth/accept-invite?token={token}&email={encoded_email}&role={encoded_role}"
        if temp_password:
            invite_url += f"&temp={urllib.parse.quote(temp_password)}"

        # Choose template based on designated authority role
        role_lower = (role or '').lower()
        if any(keyword in role_lower for keyword in ['director', 'commissioner', 'permanent secretary', 'executive', 'head']):
            template_name = 'emails/invite_director.html'
            subject = f"🏛️ Directorate Appointment & Onboarding: {role} - Nexucon"
        elif any(keyword in role_lower for keyword in ['inspector', 'site officer', 'hse', 'surveillance']):
            template_name = 'emails/invite_inspector.html'
            subject = f"🔍 Field Inspector Terminal Onboarding: {role} - Nexucon"
        elif any(keyword in role_lower for keyword in ['reviewer', 'examiner', 'architect', 'structural', 'bim']):
            template_name = 'emails/invite_reviewer.html'
            subject = f"📐 Technical Plan Examination Board Invitation - Nexucon"
        elif any(keyword in role_lower for keyword in ['contractor', 'developer', 'builder']):
            template_name = 'emails/invite_contractor.html'
            subject = f"🏗️ Contractor & Developer Portal Access - Nexucon"
        else:
            template_name = 'emails/invite_general_role.html'
            subject = f"📋 Official Invitation to Join Nexucon: {role}"

        context = {
            'email': email,
            'name': name or email.split('@')[0].capitalize(),
            'role': role,
            'department': department,
            'temp_password': temp_password,
            'invite_url': invite_url,
            'invited_by': invited_by.get_full_name() if hasattr(invited_by, 'get_full_name') and invited_by.get_full_name() else str(invited_by) if invited_by else 'System Administrator',
            'current_year': timezone.now().year
        }

        try:
            html_content = render_to_string(template_name, context)
        except Exception as e:
            logger.warning(f"Could not render {template_name}, falling back to general: {e}")
            html_content = render_to_string('emails/invite_general_role.html', context)

        return cls.send_email(
            to_email=email,
            subject=subject,
            html_content=html_content
        )

    @classmethod
    def send_2fa_otp_email(cls, email: str, name: str, otp_code: str, expires_minutes: int = 10) -> dict:
        """
        Dispatch a high-security Two-Factor Authentication (2FA) OTP code email.
        """
        subject = f"🔐 {otp_code} is your Nexucon 2FA Security Passcode"
        context = {
            'email': email,
            'name': name or 'Valued User',
            'otp_code': otp_code,
            'expires_minutes': expires_minutes,
            'current_year': timezone.now().year
        }

        html_content = render_to_string('emails/two_factor_auth.html', context)

        return cls.send_email(
            to_email=email,
            subject=subject,
            html_content=html_content
        )
