"""
Time-based One-Time Password (TOTP) two-factor authentication for mobile
apps (implementation plan §5 Week 6: "Mobile JWT authentication with 2FA").

Implements RFC 6238 (TOTP, HMAC-SHA1, 6 digits, 30-second step) directly on
the standard library so authenticator apps (Google Authenticator, Authy,
Microsoft Authenticator) work without an extra dependency. Also provides
base32 secret generation and otpauth:// provisioning URIs.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
DRIFT_WINDOWS = 1  # accept codes from the previous/next step (clock skew)


def generate_secret() -> str:
    """Generate a new 160-bit base32 TOTP secret."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _decode_secret(secret: str) -> bytes:
    padding = '=' * (-len(secret) % 8)
    return base64.b32decode(secret.upper() + padding, casefold=True)


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP for the given counter."""
    key = _decode_secret(secret)
    msg = struct.pack('>Q', counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_at(secret: str, timestamp: int = None) -> str:
    """Current TOTP code for the secret."""
    if timestamp is None:
        timestamp = int(time.time())
    return hotp(secret, counter=timestamp // STEP_SECONDS)


def verify_code(secret: str, code: str, at_timestamp: int = None,
                last_used_counter: int = None) -> bool:
    """
    Verify a submitted code with a +/-1 step window for clock skew. Codes are
    single-use: pass last_used_counter to reject replayed codes.
    """
    if not code or not code.strip().isdigit() or len(code.strip()) != DIGITS:
        return False
    timestamp = int(time.time()) if at_timestamp is None else at_timestamp
    counter = timestamp // STEP_SECONDS
    for candidate in range(counter - DRIFT_WINDOWS, counter + DRIFT_WINDOWS + 1):
        if last_used_counter is not None and candidate <= last_used_counter:
            continue  # replay of an already-used step
        if hmac.compare_digest(hotp(secret, candidate), code.strip()):
            return True
    return False


def provisioning_uri(secret: str, account_name: str, issuer: str = 'Nexucon') -> str:
    """otpauth:// URI for authenticator apps."""
    return (
        f'otpauth://totp/{quote(issuer)}:{quote(account_name)}'
        f'?secret={secret}&issuer={quote(issuer)}'
        f'&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}'
    )
