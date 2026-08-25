import logging
from typing import Dict, Any, Optional
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from .base import BaseEmailProvider

logger = logging.getLogger(__name__)

class SMTPEmailProvider(BaseEmailProvider):
    """
    Standard Django SMTP Email Provider.
    """
    def __init__(self, default_from: Optional[str] = None):
        self.default_from = default_from or getattr(settings, 'DEFAULT_FROM_EMAIL', 'notifications@nexucon.net')

    def send_email(
        self, 
        to_email: str, 
        subject: str, 
        html_content: str, 
        text_content: Optional[str] = None, 
        from_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        from_addr = from_email or self.default_from
        recipients = [to_email] if isinstance(to_email, str) else to_email
        plain_text = text_content or "Please view this message in an HTML-compatible email client."

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=plain_text,
                from_email=from_addr,
                to=recipients
            )
            msg.attach_alternative(html_content, "text/html")
            msg.send(fail_silently=False)
            return {
                "success": True,
                "id": f"smtp_{to_email.split('@')[0]}",
                "provider": "smtp"
            }
        except Exception as ex:
            logger.error(f"SMTP dispatch error: {str(ex)}")
            return {
                "success": False,
                "error": str(ex),
                "provider": "smtp"
            }
