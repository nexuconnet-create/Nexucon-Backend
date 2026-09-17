"""
Canonical serialisation and hashing — the tamper-evidence primitive.

`sha256_hex` over a canonical JSON rendering is how every attestation in this
platform is computed: inspection submissions, digital sign-offs, evidence
records and telemetry packet chains. It lives here, in the pure-helper layer,
rather than in whichever app happened to need it first, because the whole
point of a hash is that independent producers compute the *same* digest for
the same content — two slightly different canonicalisers would silently
produce two different digests for identical data.
"""
import hashlib
import json


def canonical_json(obj) -> str:
    """Render ``obj`` so that equal content always yields an identical string.

    Sorted keys, no insignificant whitespace, and ``default=str`` so a date,
    UUID or Decimal is rendered deterministically rather than raising. This is
    the byte string that gets hashed — never hash a dict directly.
    """
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def sha256_hex(payload: str) -> str:
    """SHA-256 of a UTF-8 string, as lowercase hex."""
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def sha256_of_bytes(chunk_iter) -> str:
    """SHA-256 of a stream of byte chunks, as lowercase hex.

    Streaming means a large upload is hashed in the same pass that writes it
    to storage, so the digest attests the bytes actually stored rather than a
    buffer that was later truncated.
    """
    digest = hashlib.sha256()
    for chunk in chunk_iter:
        digest.update(chunk)
    return digest.hexdigest()


def chain_hash(previous_hash: str, sequence: int, payload) -> str:
    """One link of an append-only hash chain.

    ``sha256(previous_hash : sequence : canonical(payload))``. Folding the
    previous link in is what makes the chain tamper-evident: altering packet
    *n* changes its digest, which changes *n+1*, and so on to the head — so a
    single stored edit cannot be hidden by re-hashing one row.
    """
    return sha256_hex(f"{previous_hash or ''}:{sequence}:{canonical_json(payload)}")
