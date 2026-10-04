"""Password hashing (PBKDF2-SHA256) and push-token hashing (SHA-256)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_source(token: str, push_tokens: dict[str, str]) -> str | None:
    """Return the source name whose stored hash matches `token`, else None."""
    h = hash_token(token)
    for source, stored in push_tokens.items():
        if hmac.compare_digest(h, stored):
            return source
    return None
