"""First-party login helpers for SyKit endpoints.

Passwords are hashed with scrypt (standard library). Where user records
live is up to the app; these helpers only cover hashing, verification,
and moving a verified identity into the session that @perms checks.

Usage from endpoints:

    from sykit import auth
    from sykit.utils import expose, limits

    @expose("login")
    @limits({"per-client": "10m"})
    def login(username: str, password: str):
        user = find_user(username)
        if user is None or not auth.verify_password(password, user["hash"]):
            return {"ok": False}
        auth.login({"user": username, "role": user["role"]})
        return {"ok": True}

    @expose("logout")
    def logout():
        auth.logout()
        return {"ok": True}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from sykit.utils import _INTERNAL_PREFIX, _session

SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MAX_VERIFY_KEY_BYTES = 64
MAX_PASSWORD_BYTES = 1024
_SCRYPT_MAXMEM = 128 * 1024 * 1024
_ROTATE_KEY = _INTERNAL_PREFIX + "rotate"


class AuthError(ValueError):
    """An invalid argument to the auth helpers."""


def _password_bytes(password: Any) -> bytes | None:
    if not isinstance(password, str) or not password:
        return None
    raw = password.encode("utf-8")
    return raw if len(raw) <= MAX_PASSWORD_BYTES else None


def hash_password(password: str) -> str:
    """Hash a password for storage; verify with verify_password()."""
    raw = _password_bytes(password)
    if raw is None:
        raise AuthError(
            "passwords must be non-empty strings of at most "
            f"{MAX_PASSWORD_BYTES} UTF-8 bytes."
        )
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        raw,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=KEY_BYTES,
    )
    encoded_salt = base64.b64encode(salt).decode("ascii")
    encoded_key = base64.b64encode(key).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encoded_salt}${encoded_key}"


def verify_password(password: str, stored: str) -> bool:
    """True when the password matches a hash_password() value.

    A wrong, empty, or non-string password returns False. A malformed
    stored value raises AuthError: that is corrupt data, not a failed
    login attempt.
    """
    if not isinstance(stored, str):
        raise AuthError("stored password hashes must be strings.")
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise AuthError("stored value is not a SyKit scrypt hash.")
    try:
        cost, block_size, parallelism = (int(part) for part in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except ValueError as error:
        raise AuthError(f"stored value is not a SyKit scrypt hash: {error}") from error
    if (
        cost < 2**10
        or cost > 2**22
        or cost & (cost - 1)
        or not 1 <= block_size <= 32
        or not 1 <= parallelism <= 16
        or not salt
        or not expected
        or len(expected) > MAX_VERIFY_KEY_BYTES
        or 128 * cost * block_size > _SCRYPT_MAXMEM
    ):
        raise AuthError("stored value has out-of-range scrypt parameters.")
    raw = _password_bytes(password)
    if raw is None:
        return False
    try:
        key = hashlib.scrypt(
            raw,
            salt=salt,
            n=cost,
            r=block_size,
            p=parallelism,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except ValueError as error:
        raise AuthError(
            f"stored value has invalid scrypt parameters: {error}"
        ) from error
    return hmac.compare_digest(key, expected)


def login(claims: dict[str, Any]) -> None:
    """Replace the visitor's session with the given claims.

    Call after verifying credentials. The keys become the session values
    @perms checks (for example {"role": "admin"}). Everything already in
    the session is dropped, and the session id is rotated when a
    server-side session store is configured.
    """
    if not isinstance(claims, dict) or not claims:
        raise AuthError("login() takes a non-empty dict of session claims.")
    for key, value in claims.items():
        if not isinstance(key, str) or not key:
            raise AuthError("claim keys must be non-empty strings.")
        if key.startswith(_INTERNAL_PREFIX):
            raise AuthError(f'claim keys may not start with "{_INTERNAL_PREFIX}".')
        try:
            json.dumps(value)
        except (TypeError, ValueError) as error:
            raise AuthError(
                f"claim {key!r} is not JSON-serializable: {error}"
            ) from error
    session = _session()
    rate_id_key = _INTERNAL_PREFIX + "rate_id"
    rate_id = session.get(rate_id_key)
    session.clear()
    session.update(claims)
    if isinstance(rate_id, str) and rate_id:
        session[rate_id_key] = rate_id
    session[_ROTATE_KEY] = True


def logout() -> None:
    """Clear the visitor's session.

    With a server-side session store the stored session is deleted, so
    the old cookie is revoked everywhere, not just in this browser.
    """
    _session().clear()


__all__ = [
    "AuthError",
    "hash_password",
    "login",
    "logout",
    "verify_password",
]
