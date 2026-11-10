"""Password hashing and login tokens. Pure functions: no database, no config.

Callers pass the secret in, so these are easy to unit-test.

- Passwords use Argon2 (a slow, memory-hard hash) through pwdlib.
- Tokens are JWTs signed with HS256. Claims: `sub` (who), `role`, `iat` (issued at)
  and `exp` (expires at). Only a holder of the secret can create a valid token.
"""

from datetime import UTC, datetime, timedelta
from typing import Final

import jwt
from pwdlib import PasswordHash
from pydantic import BaseModel, ValidationError

from support_agent.domain.enums import Role

ALGORITHM: Final = "HS256"

_hasher = PasswordHash.recommended()  # Argon2id with safe defaults


class InvalidTokenError(Exception):
    """The token is malformed, tampered with, expired, or missing a required claim."""


class TokenClaims(BaseModel):
    sub: str
    role: Role
    iat: int
    exp: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _hasher.verify(password, password_hash)


def create_access_token(
    *, subject: str, role: Role, secret: str, lifetime: timedelta, now: datetime | None = None
) -> str:
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": subject,
        "role": role.value,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + lifetime).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, *, secret: str) -> TokenClaims:
    """Return the verified claims, or raise InvalidTokenError for any problem."""
    try:
        # Pinning `algorithms` blocks the "alg: none" and algorithm-swap attacks.
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "role", "iat", "exp"]},
        )
        return TokenClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as error:
        raise InvalidTokenError("invalid token") from error
