"""
Firebase Cloud Messaging (FCM) push delivery service (implementation plan
§5 Week 6: mobile push notifications).

Uses the FCM HTTP v1 REST API with a Google service-account access token
obtained through the self-signed JWT grant (RFC 7523) — no firebase-admin
dependency required. The service account must have the Cloud Messaging API
enabled and the "Service Account Token Creator" role.

When the FCM credentials are not configured, `send` raises
FCMCredentialsMissing so callers can report the missing credential — no
notification is ever fabricated as "sent".
"""
import logging
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_TOKEN_URL = 'https://oauth2.googleapis.com/token'
_FCM_ENDPOINT = 'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'
SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'

# Access-token cache (process-local, per credentials signature).
_cached_token = {'token': None, 'expires_at': 0.0, 'fingerprint': None}


class FCMError(Exception):
    """FCM delivery failure."""


class FCMCredentialsMissing(FCMError):
    """FCM push credentials are not configured."""


def _credentials_fingerprint() -> str:
    return '|'.join([
        getattr(settings, 'GOOGLE_MEETING_PROJECT_ID', '')
        or getattr(settings, 'GOOGLE_CLOUD_PROJECT_ID', ''),
        getattr(settings, 'GOOGLE_MEETING_CLIENT_EMAIL', '') or '',
        (getattr(settings, 'GOOGLE_MEETING_PRIVATE_KEY', '') or '')[:32],
    ])


def fcm_configured() -> bool:
    return bool(
        (getattr(settings, 'GOOGLE_MEETING_PROJECT_ID', '')
         or getattr(settings, 'GOOGLE_CLOUD_PROJECT_ID', ''))
        and getattr(settings, 'GOOGLE_MEETING_CLIENT_EMAIL', '')
        and getattr(settings, 'GOOGLE_MEETING_PRIVATE_KEY', '')
    )


def _get_access_token() -> str:
    """Obtain (and cache) an OAuth2 access token via the JWT bearer grant."""
    if not fcm_configured():
        raise FCMCredentialsMissing(
            'FCM push is not configured: GOOGLE_MEETING_CLIENT_EMAIL / '
            'GOOGLE_MEETING_PRIVATE_KEY / GOOGLE_MEETING_PROJECT_ID (or '
            'GOOGLE_CLOUD_PROJECT_ID) must be set to a service account with '
            'Cloud Messaging enabled.')
    fingerprint = _credentials_fingerprint()
    now = time.time()
    if (_cached_token['token'] and _cached_token['fingerprint'] == fingerprint
            and now < _cached_token['expires_at'] - 60):
        return _cached_token['token']

    import base64
    import hashlib
    import json

    client_email = settings.GOOGLE_MEETING_CLIENT_EMAIL
    private_key = settings.GOOGLE_MEETING_PRIVATE_KEY
    project_id = (getattr(settings, 'GOOGLE_MEETING_PROJECT_ID', '')
                  or settings.GOOGLE_CLOUD_PROJECT_ID)

    header = base64.urlsafe_b64encode(
        json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    claims = base64.urlsafe_b64encode(json.dumps({
        'iss': client_email,
        'scope': SCOPE,
        'aud': _TOKEN_URL,
        'iat': int(now),
        'exp': int(now) + 3600,
    }).encode()).decode().rstrip('=')

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    signing_input = f'{header}.{claims}'.encode()
    signature = base64.urlsafe_b64encode(key.sign(
        signing_input, asym_padding.PKCS1v15(), hashes.SHA256(),
    )).decode().rstrip('=')
    assertion = f'{header}.{claims}.{signature}'

    response = requests.post(_TOKEN_URL, data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }, timeout=20)
    if response.status_code != 200:
        raise FCMError(f'Google OAuth token request failed: HTTP '
                       f'{response.status_code} {response.text[:300]}')
    payload = response.json()
    _cached_token.update({
        'token': payload['access_token'],
        'expires_at': now + int(payload.get('expires_in', 3600)),
        'fingerprint': fingerprint,
    })
    logger.debug('Obtained FCM access token for project %s', project_id)
    return payload['access_token']


def send_push(token, title, body, data=None):
    """
    Send an FCM HTTP v1 message to a single device token.
    Returns the FCM response name on success; raises FCMError on failure.
    """
    if not fcm_configured():
        raise FCMCredentialsMissing(
            'FCM push is not configured — set the Google service-account '
            'credentials (GOOGLE_MEETING_CLIENT_EMAIL, GOOGLE_MEETING_PRIVATE_KEY, '
            'GOOGLE_MEETING_PROJECT_ID) and enable the Cloud Messaging API.')

    project_id = (getattr(settings, 'GOOGLE_MEETING_PROJECT_ID', '')
                  or settings.GOOGLE_CLOUD_PROJECT_ID)
    access_token = _get_access_token()
    message = {
        'message': {
            'token': token,
            'notification': {'title': str(title), 'body': str(body)},
            'android': {'priority': 'high'},
        },
    }
    if data:
        message['message']['data'] = {str(k): str(v) for k, v in data.items()}

    response = requests.post(
        _FCM_ENDPOINT.format(project_id=project_id),
        json=message,
        headers={'Authorization': f'Bearer {access_token}',
                 'Content-Type': 'application/json'},
        timeout=20,
    )
    if response.status_code not in (200, 201):
        raise FCMError(f'FCM send failed: HTTP {response.status_code} '
                       f'{response.text[:300]}')
    return response.json().get('name', '')


class PushNotificationService:
    """
    Push delivery to a user's registered devices. Tokens that FCM reports as
    invalid/unregistered are deactivated automatically.
    """

    @classmethod
    def notify_user(cls, user, title, body, data=None):
        """Send to every active device of `user`. Returns a per-token result."""
        from .models import PushDeviceToken
        results = []
        for device in PushDeviceToken.objects.filter(user=user, is_active=True):
            try:
                send_push(device.token, title, body, data)
                device.last_used_at = timezone.now()
                device.save(update_fields=['last_used_at', 'updated_at'])
                results.append({'token_id': str(device.id), 'delivered': True})
            except FCMCredentialsMissing:
                raise  # surface the missing credential to the caller
            except FCMError as exc:
                message = str(exc)
                if ('UNREGISTERED' in message.upper()
                        or 'INVALID_REGISTRATION' in message.upper()):
                    device.is_active = False
                    device.save(update_fields=['is_active', 'updated_at'])
                logger.warning('Push to device %s failed: %s', device.id, message)
                results.append({'token_id': str(device.id), 'delivered': False,
                                'error': message})
        return results
