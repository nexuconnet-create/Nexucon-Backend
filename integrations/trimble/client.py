"""
Trimble Connect Platform API client (implementation plan §5 Weeks 1–2).

Implements the OAuth 2.0 Authorization-Code flow with PKCE (S256), token
refresh, connection health checks and BIM project / model discovery.

All endpoints are configurable through environment variables because the
Trimble Connect API base URL differs between regional deployments. Tokens are
persisted on the caller-provided connection object (apps.digital_eye.
TrimbleConnection) so the refresh lifecycle survives restarts.

No credential is ever hardcoded: when TRIMBLE_CLIENT_ID / SECRET are absent
the client raises TrimbleCredentialsMissing — callers surface that to the
operator instead of fabricating a response.
"""
import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_AUTHORIZE_URL = 'https://app.connect.trimble.com/connect/oauth/authorize'
DEFAULT_TOKEN_URL = 'https://app.connect.trimble.com/connect/oauth/token'
DEFAULT_API_BASE = 'https://app.connect.trimble.com/connect/api'

REQUEST_TIMEOUT = 20  # seconds


class TrimbleError(Exception):
    """Trimble Connect integration failure."""


class TrimbleCredentialsMissing(TrimbleError):
    """The Trimble OAuth client credentials are not configured."""


class TrimbleAuthError(TrimbleError):
    """Authorization / token exchange failure."""


class TrimbleClient:
    """Thin HTTP client for the Trimble Connect Platform API."""

    def __init__(self, connection=None):
        self.connection = connection

    # ------------------------------------------------------------- config
    @classmethod
    def credentials_configured(cls) -> bool:
        return bool(getattr(settings, 'TRIMBLE_CLIENT_ID', '')
                    and getattr(settings, 'TRIMBLE_CLIENT_SECRET', ''))

    @property
    def client_id(self):
        if not getattr(settings, 'TRIMBLE_CLIENT_ID', ''):
            raise TrimbleCredentialsMissing(
                'TRIMBLE_CLIENT_ID is not configured — obtain OAuth client '
                'credentials from the Trimble Developer Console.')
        return settings.TRIMBLE_CLIENT_ID

    @property
    def client_secret(self):
        if not getattr(settings, 'TRIMBLE_CLIENT_SECRET', ''):
            raise TrimbleCredentialsMissing(
                'TRIMBLE_CLIENT_SECRET is not configured — obtain OAuth client '
                'credentials from the Trimble Developer Console.')
        return settings.TRIMBLE_CLIENT_SECRET

    @property
    def redirect_uri(self):
        uri = getattr(settings, 'TRIMBLE_REDIRECT_URI', '')
        if not uri:
            raise TrimbleCredentialsMissing(
                'TRIMBLE_REDIRECT_URI is not configured — set it to the '
                'registered callback URL of the OAuth client.')
        return uri

    # ------------------------------------------------------------ OAuth2
    @staticmethod
    def generate_pkce_pair():
        """Return (verifier, challenge) for PKCE S256."""
        verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(verifier.encode('ascii')).digest()
        challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
        return verifier, challenge

    def start_authorization(self, connection, state=''):
        """
        Begin the OAuth2+PKCE handshake: generates the code verifier /
        challenge, persists the verifier on the connection and returns the
        authorization URL the operator must open.

        The URL (and thus the client credentials) is resolved BEFORE anything
        is persisted — a connection that cannot start the handshake is never
        marked as pending authorization.
        """
        verifier, challenge = self.generate_pkce_pair()
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'state': state or str(connection.id),
        }
        connection.pkce_verifier = verifier
        connection.status = 'pending_authorization'
        connection.last_error = ''
        connection.save(update_fields=['pkce_verifier', 'status', 'last_error', 'updated_at'])

        authorize_url = getattr(settings, 'TRIMBLE_AUTHORIZE_URL', '') or DEFAULT_AUTHORIZE_URL
        return f'{authorize_url}?{urlencode(params)}'

    def complete_authorization(self, connection, authorization_code):
        """Exchange the authorization code for tokens and persist them."""
        token_url = getattr(settings, 'TRIMBLE_TOKEN_URL', '') or DEFAULT_TOKEN_URL
        if not connection.pkce_verifier:
            raise TrimbleAuthError(
                'No PKCE verifier on this connection — start_authorization() '
                'must be called first.')
        response = requests.post(token_url, data={
            'grant_type': 'authorization_code',
            'code': authorization_code,
            'redirect_uri': self.redirect_uri,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code_verifier': connection.pkce_verifier,
        }, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            connection.status = 'error'
            connection.last_error = f'Token exchange failed: HTTP {response.status_code} {response.text[:500]}'
            connection.save(update_fields=['status', 'last_error', 'updated_at'])
            raise TrimbleAuthError(connection.last_error)
        self._store_tokens(connection, response.json())
        connection.pkce_verifier = ''  # single-use
        connection.save(update_fields=['pkce_verifier'])
        return connection

    def refresh_tokens(self, connection):
        """Use the refresh token to obtain a fresh access token."""
        if not connection.refresh_token:
            raise TrimbleAuthError('No refresh token stored for this connection.')
        token_url = getattr(settings, 'TRIMBLE_TOKEN_URL', '') or DEFAULT_TOKEN_URL
        response = requests.post(token_url, data={
            'grant_type': 'refresh_token',
            'refresh_token': connection.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            connection.status = 'error'
            connection.last_error = f'Token refresh failed: HTTP {response.status_code} {response.text[:500]}'
            connection.save(update_fields=['status', 'last_error', 'updated_at'])
            raise TrimbleAuthError(connection.last_error)
        self._store_tokens(connection, response.json())
        return connection

    @staticmethod
    def _store_tokens(connection, payload):
        connection.access_token = payload.get('access_token', '')
        if payload.get('refresh_token'):
            connection.refresh_token = payload['refresh_token']
        expires_in = payload.get('expires_in')
        if expires_in:
            from django.utils import timezone
            from datetime import timedelta
            connection.token_expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        connection.scope = payload.get('scope', connection.scope)
        connection.status = 'connected'
        connection.last_error = ''
        connection.save(update_fields=[
            'access_token', 'refresh_token', 'token_expires_at', 'scope',
            'status', 'last_error', 'updated_at',
        ])

    # --------------------------------------------------------------- API
    def _ensure_access_token(self):
        if not self.connection or self.connection.status != 'connected':
            raise TrimbleAuthError('Connection is not authorized — complete the OAuth flow first.')
        if self.connection.token_expired:
            self.refresh_tokens(self.connection)

    def _request(self, method, path, params=None, json_body=None, **kwargs):
        self._ensure_access_token()
        base = getattr(settings, 'TRIMBLE_API_BASE', '') or DEFAULT_API_BASE
        url = f'{base.rstrip("/")}/{path.lstrip("/")}'
        response = requests.request(
            method, url,
            params=params, json=json_body,
            headers={'Authorization': f'Bearer {self.connection.access_token}',
                     'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT, **kwargs,
        )
        if response.status_code == 401:
            # Access token may have been revoked server-side — refresh once.
            self.refresh_tokens(self.connection)
            response = requests.request(
                method, url,
                params=params, json=json_body,
                headers={'Authorization': f'Bearer {self.connection.access_token}',
                         'Accept': 'application/json'},
                timeout=REQUEST_TIMEOUT, **kwargs,
            )
        if response.status_code >= 400:
            raise TrimbleError(
                f'Trimble API {method} {path} failed: HTTP {response.status_code} '
                f'{response.text[:500]}')
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def health_check(self, connection):
        """
        Verify the connection end-to-end with a lightweight authenticated
        call. Persists the result on the connection (plan §5 Week 1 health
        checks). Returns (healthy: bool, detail: str).
        """
        from django.utils import timezone
        try:
            if not self.credentials_configured():
                connection.last_health_check_at = timezone.now()
                connection.last_health_status = 'unconfigured'
                connection.last_error = 'Trimble OAuth client credentials are not configured.'
                connection.save(update_fields=[
                    'last_health_check_at', 'last_health_status', 'last_error', 'updated_at'])
                return False, connection.last_error
            if connection.status != 'connected':
                detail = f'Connection status is "{connection.get_status_display()}" — complete the OAuth flow.'
                connection.last_health_check_at = timezone.now()
                connection.last_health_status = 'disconnected'
                connection.last_error = detail
                connection.save(update_fields=[
                    'last_health_check_at', 'last_health_status', 'last_error', 'updated_at'])
                return False, detail
            self.connection = connection
            me = self._request('GET', '/users/me')
            connection.trimble_user_id = str(me.get('id') or me.get('uid') or '')
            connection.trimble_user_name = me.get('name') or me.get('displayName') or ''
            connection.last_health_check_at = timezone.now()
            connection.last_health_status = 'healthy'
            connection.last_error = ''
            connection.save(update_fields=[
                'trimble_user_id', 'trimble_user_name', 'last_health_check_at',
                'last_health_status', 'last_error', 'updated_at'])
            return True, f'Connected as {connection.trimble_user_name or connection.trimble_user_id}'
        except TrimbleError as exc:
            connection.last_health_check_at = timezone.now()
            connection.last_health_status = 'error'
            connection.last_error = str(exc)
            connection.save(update_fields=[
                'last_health_check_at', 'last_health_status', 'last_error', 'updated_at'])
            return False, str(exc)

    # ------------------------------------------------------- discovery
    def discover_projects(self, connection):
        """
        List Trimble Connect projects visible to the connected account and
        upsert them as TrimbleProject rows. Returns the queryset.
        """
        self.connection = connection
        data = self._request('GET', '/projects')
        # The API may return {"projects": [...]} or a bare list.
        items = data.get('projects') if isinstance(data, dict) else data
        if items is None:
            items = []
        for item in items:
            external_id = str(item.get('id') or item.get('projectId') or '')
            if not external_id:
                continue
            TrimbleProject.objects.update_or_create(
                connection=connection, external_id=external_id,
                defaults={
                    'name': item.get('name') or '',
                    'raw_metadata': item,
                },
            )
        return connection.trimble_projects.all()

    def discover_models(self, trimble_project):
        """
        List the BIM models of a Trimble project. Returns the raw model list
        (each entry contains model id, name, and file metadata).
        """
        self.connection = trimble_project.connection
        data = self._request('GET', f'/projects/{trimble_project.external_id}/models')
        models = data.get('models') if isinstance(data, dict) else data
        return models or []
