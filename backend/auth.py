import hashlib
import hmac
import logging
import os
import secrets
import time


logger = logging.getLogger("safevision.auth")
SESSION_COOKIE_NAME = "safevision_session"
SESSION_MAX_AGE = 12 * 60 * 60
MINIMUM_PASSWORD_LENGTH = 16


def configured_password():
    return os.getenv("SAFEVISION_ACCESS_PASSWORD") or None


def validate_auth_configuration():
    if os.getenv("SAFEVISION_ENV", "development").lower() != "production":
        return

    password = configured_password()
    if password is None:
        raise RuntimeError(
            "SAFEVISION_ACCESS_PASSWORD is required when SAFEVISION_ENV=production."
        )
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        logger.warning(
            "SAFEVISION_ACCESS_PASSWORD is shorter than %s characters; "
            "use a stronger password before exposing the app publicly.",
            MINIMUM_PASSWORD_LENGTH,
        )


def create_session_token(password, *, now=None):
    issued_at = int(time.time() if now is None else now)
    payload = f"{issued_at}.{secrets.token_urlsafe(18)}"
    key = hashlib.sha256(password.encode("utf-8")).digest()
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def is_valid_session_token(token, password, *, now=None):
    if not token or not password:
        return False
    try:
        issued_at_text, nonce, signature = token.rsplit(".", 2)
        issued_at = int(issued_at_text)
    except (ValueError, AttributeError):
        return False

    current_time = int(time.time() if now is None else now)
    if issued_at > current_time or current_time - issued_at > SESSION_MAX_AGE:
        return False

    payload = f"{issued_at_text}.{nonce}"
    key = hashlib.sha256(password.encode("utf-8")).digest()
    expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
