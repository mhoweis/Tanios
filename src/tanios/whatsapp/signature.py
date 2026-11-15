"""Proof that a webhook really came from Meta.

Meta signs the exact bytes of every webhook body with the app secret and sends the
result in the X-Hub-Signature-256 header as "sha256=<hex digest>".
"""

import hashlib
import hmac

PREFIX = "sha256="


def is_valid_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith(PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(PREFIX) :])


def sign(body: bytes, app_secret: str) -> str:
    """The header value Meta would send for this body. Used by tests and local tools."""
    return PREFIX + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
