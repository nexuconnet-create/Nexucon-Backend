"""
Device-token authentication for the machine legs of PUNDIT ingestion.

A PUNDIT unit (or the gateway it docks to) is not a person and cannot run the
interactive JWT login. It pushes with a credential issued against its own
``FieldDevice`` row, and this module turns that credential into a request.

Two decisions worth stating, because both are load-bearing:

**The token authenticates *as its issuer*.** It returns the officer who
provisioned the device, not an anonymous principal, exactly as
``apps.accounts.authentication.ApiKeyAuthentication`` returns ``api_key.user``.
That is not a convenience: the promotion path records
``created_by=request.user`` and ``operator=request.user`` on every row it
writes into the statutory registry. An anonymous principal there would either
fail the FK or file a measurement under "nobody", and a reading with no
operator is precisely the record this platform cannot produce.

What keeps that from being a widening of authority is the second decision:

**The credential may only touch its own device.** Deactivating the issuer
stops it (checked here, every request), and the telemetry views additionally
pin every session lookup to ``request.device`` — so a token for one instrument
cannot read or append to another instrument's session even when the same
officer owns both. The scheme keyword is ``Device``, deliberately distinct from
``Bearer`` and ``ApiKey`` so a device secret can never be presented where a
person's session token is expected.

This authenticator is declared per-view on the telemetry routes only, never in
``DEFAULT_AUTHENTICATION_CLASSES``. A device credential should not be usable
anywhere a device is not the subject.
"""
import logging

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import DeviceToken

logger = logging.getLogger(__name__)


class DeviceTokenAuthentication(BaseAuthentication):
    """`Authorization: Device <token>` → the issuer, with the device attached.

    Returns ``None`` (no opinion) when the header is absent or uses another
    scheme, so DRF falls through to the JWT authenticator as usual. Raises only
    when a ``Device`` credential was actually presented and could not be
    honoured — a wrong token must fail loudly, not silently degrade into an
    anonymous request that some later permission happens to allow.
    """

    keyword = 'Device'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None

        if len(parts) == 1:
            raise AuthenticationFailed('Device credential header carries no token.')
        if len(parts) > 2:
            raise AuthenticationFailed(
                'Device credential header carries more than one token.')

        try:
            raw = parts[1].decode()
        except UnicodeError:
            raise AuthenticationFailed('Device credential is not valid text.')

        token = DeviceToken.resolve(raw)
        if token is None:
            # One message for unknown, revoked and expired alike — the caller
            # learns that it was refused, not which kind of secret it guessed.
            raise AuthenticationFailed('Invalid, revoked or expired device credential.')

        issuer = token.issued_by
        if issuer is None or not issuer.is_active:
            raise AuthenticationFailed(
                'This device credential has no active issuing officer. '
                'Issue a new credential from an active account.')

        token.note_use()
        # Attached so the views can pin every lookup to this one instrument.
        request.device = token.device
        request.device_token = token
        return (issuer, token)

    def authenticate_header(self, request):
        return self.keyword
