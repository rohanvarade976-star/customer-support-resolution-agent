"""Login and token checking. The only place that turns credentials or a token into an identity.

Identity rules (CLAUDE.md): it comes only from a verified token, and the user row
is re-read from the database on every request, so a deleted user or a changed role
stops working immediately instead of when the token expires.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from support_agent.domain.enums import Role
from support_agent.repositories import users
from support_agent.schemas.auth import Principal, TokenResponse


def invalid_credentials() -> AppError:
    # One shared error for "unknown email" and "wrong password", so a caller
    # cannot use login to discover which emails have accounts.
    return AppError(401, "INVALID_CREDENTIALS", "Incorrect email or password.")


def unauthenticated() -> AppError:
    return AppError(
        401,
        "NOT_AUTHENTICATED",
        "Missing, invalid or expired token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass(frozen=True)
class _Account:
    """What login needs to know about whoever owns an email address."""

    role: Role
    token_subject: str  # becomes the JWT `sub`
    password_hash: str | None


@lru_cache
def _dummy_hash() -> str:
    # Checked when the email is unknown, so "no such user" takes as long as "wrong password".
    return hash_password("dummy-password-for-timing")


async def _find_account(session: AsyncSession, email: str) -> _Account | None:
    staff = await users.get_staff_by_email(session, email)
    if staff is not None:
        return _Account(Role(staff.role), str(staff.id), staff.password_hash)
    customer = await users.get_customer_by_email(session, email)
    if customer is not None:
        return _Account(Role.CUSTOMER, customer.external_auth_subject, customer.password_hash)
    return None


async def login(
    session: AsyncSession, settings: Settings, *, email: str, password: str
) -> TokenResponse:
    account = await _find_account(session, email.strip().lower())
    # Argon2 is deliberately slow; a thread keeps it from freezing the event loop.
    stored_hash = account.password_hash if account and account.password_hash else _dummy_hash()
    password_ok = await asyncio.to_thread(verify_password, password, stored_hash)
    if account is None or account.password_hash is None or not password_ok:
        raise invalid_credentials()

    lifetime = timedelta(minutes=settings.access_token_minutes)
    token = create_access_token(
        subject=account.token_subject,
        role=account.role,
        secret=settings.jwt_secret.get_secret_value(),
        lifetime=lifetime,
    )
    return TokenResponse(access_token=token, expires_in=int(lifetime.total_seconds()))


async def authenticate_token(session: AsyncSession, settings: Settings, token: str) -> Principal:
    """Verify the token, then load the matching user. Any mismatch is a plain 401."""
    try:
        claims = decode_access_token(token, secret=settings.jwt_secret.get_secret_value())
    except InvalidTokenError:
        raise unauthenticated() from None

    if claims.role == Role.CUSTOMER:
        customer = await users.get_customer_by_subject(session, claims.sub)
        if customer is None:
            raise unauthenticated()
        return Principal(
            id=customer.id, role=Role.CUSTOMER, name=customer.name, email=customer.email
        )

    try:
        staff_id = UUID(claims.sub)
    except ValueError:
        raise unauthenticated() from None
    staff = await users.get_staff_by_id(session, staff_id)
    # The role in the token must still match the database.
    if staff is None or Role(staff.role) != claims.role:
        raise unauthenticated()
    return Principal(id=staff.id, role=Role(staff.role), name=staff.name, email=staff.email)
