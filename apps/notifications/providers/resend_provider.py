import os
import json
import urllib.request
import urllib.error
import logging
from typing import Dict, Any, Optional
from django.conf import settings
from .base import BaseEmailProvider

logger = logging.getLogger(__name__)

class ResendEmailProvider(BaseEmailProvider):
    """
    Resend REST API Email Provider.
    """
    API_URL = 'https://api.resend.com/emails'

    def __init__(self, api_key: Optional[str] = None, default_from: Optional[str] = None):
        self.api_key = api_key or getattr(settings, 'RESEND_API_KEY', os.environ.get('RESEND_API_KEY', ''))
        self.default_from = default_from or getattr(
            settings, 
            'RESEND_FROM_EMAIL', 
            os.environ.get('RESEND_FROM_EMAIL', 'Nexucon Notifications <notifications@nexucon.net>')
        )

    def send_email(
        self, 
        to_email: str, 
        subject: str, 
        html_content: str, 
        text_content: Optional[str] = None, 
        from_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if not self.api_key:
            logger.warning("Resend API key is missing. Simulating sandbox email delivery.")
            return {
                "success": True, 
                "id": f"sim_resend_{os.urandom(4).hex()}", 
                "simulated": True,
                "provider": "resend_sandbox"
            }

        payload = {
            "from": from_email or self.default_from,
            "to": [to_email] if isinstance(to_email, str) else to_email,
            "subject": subject,
            "html": html_content,
        }
        if text_content:
            payload["text"] = text_content
        if metadata:
            payload["headers"] = {f"X-Nexucon-{k}": str(v) for k, v in metadata.items() if isinstance(v, (str, int))}

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            self.API_URL,
            data=data,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'Nexucon-Backend/1.0'
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_data = json.loads(resp.read().decode('utf-8'))
                msg_id = resp_data.get('id')
                logger.info(f"Email successfully accepted by Resend. Message ID: {msg_id} -> {to_email}")
                return {
                    "success": True, 
                    "id": msg_id, 
                    "provider": "resend",
                    "raw_response": resp_data
                }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"Resend HTTP Error ({e.code}): {error_body}")
            return {
                "success": False, 
                "error": error_body, 
                "status_code": e.code, 
                "provider": "resend"
            }
        except Exception as ex:
            logger.error(f"Resend dispatch error: {str(ex)}")
            return {
                "success": False, 
                "error": str(ex), 
                "provider": "resend"
            }
