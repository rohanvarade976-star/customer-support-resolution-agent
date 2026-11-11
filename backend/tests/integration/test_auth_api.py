"""Login, /me and role checks through the real app against the test database."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.api.deps import ReviewerDep
from support_agent.core.config import get_settings
from support_agent.core.security import create_access_token
from support_agent.db.models import Customer, StaffUser
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import Role, StaffRole

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

settings = get_settings()
SECRET = settings.jwt_secret.get_secret_value()


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession, demo_password_hash: str) -> None:
    await seed(session, load_scenarios(), NOW, demo_password_hash)


@pytest.fixture(autouse=True)
def reviewer_only_route(app: FastAPI) -> None:
    """A route that exists only in tests, to exercise require_reviewer."""

    @app.get("/test/reviewer-only")
    async def reviewer_only(principal: ReviewerDep) -> dict[str, str]:
        return {"reviewer": principal.name}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def forged_token(*, subject: str, role: Role, secret: str = SECRET) -> str:
    return create_access_token(
        subject=subject, role=role, secret=secret, lifetime=timedelta(minutes=5)
    )


def assert_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert response.status_code == status
    assert body["success"] is False
    assert body["error_code"] == code
    assert isinstance(body["message"], str)
    return body


# ---------------------------------------------------------------- login


async def test_customer_login_returns_a_working_token(
    client: httpx.AsyncClient, login: Callable[..., Awaitable[httpx.Response]]
) -> None:
    response = await login("priya@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105  (a label, not a password)
    assert body["expires_in"] == settings.access_token_minutes * 60
    me = await client.get("/me", headers=bearer(body["access_token"]))
    assert me.json()["name"] == "Priya Sharma"
    assert me.json()["role"] == "customer"


async def test_reviewer_login_returns_a_reviewer_token(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("anita@example.com")

    me = await client.get("/me", headers=headers)

    assert me.json()["name"] == "Anita Desai"
    assert me.json()["role"] == "reviewer"


async def test_login_ignores_email_case_and_surrounding_spaces(
    client: httpx.AsyncClient, login: Callable[..., Awaitable[httpx.Response]]
) -> None:
    response = await login("  Priya@Example.COM ")

    assert response.status_code == 200


async def test_wrong_password_and_unknown_email_give_the_identical_401(
    client: httpx.AsyncClient, login: Callable[..., Awaitable[httpx.Response]]
) -> None:
    wrong_password = await login("priya@example.com", "not-the-password")
    unknown_email = await login("nobody@example.com")

    assert_error(wrong_password, 401, "INVALID_CREDENTIALS")
    assert wrong_password.status_code == unknown_email.status_code
    assert wrong_password.json() == unknown_email.json()


async def test_account_without_a_password_cannot_log_in(
    client: httpx.AsyncClient,
    session: AsyncSession,
    login: Callable[..., Awaitable[httpx.Response]],
) -> None:
    customer = await session.scalar(select(Customer).where(Customer.email == "priya@example.com"))
    assert customer is not None
    customer.password_hash = None
    await session.flush()

    response = await login("priya@example.com")

    assert_error(response, 401, "INVALID_CREDENTIALS")


async def test_login_with_a_malformed_body_uses_the_error_format(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/auth/login", json={"email": "priya@example.com"})

    assert_error(response, 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- /me


async def test_me_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/me")

    assert_error(response, 401, "NOT_AUTHENTICATED")
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_with_garbage_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/me", headers=bearer("not-a-jwt"))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_me_with_expired_token_is_401(client: httpx.AsyncClient) -> None:
    expired = create_access_token(
        subject="seed|priya",
        role=Role.CUSTOMER,
        secret=SECRET,
        lifetime=timedelta(minutes=5),
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    response = await client.get("/me", headers=bearer(expired))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_me_with_token_signed_by_the_wrong_secret_is_401(client: httpx.AsyncClient) -> None:
    token = forged_token(subject="seed|priya", role=Role.CUSTOMER, secret="w" * 40)

    response = await client.get("/me", headers=bearer(token))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_token_for_a_user_that_no_longer_exists_is_401(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    headers = await auth_headers("anita@example.com")
    await session.execute(delete(StaffUser).where(StaffUser.email == "anita@example.com"))

    response = await client.get("/me", headers=headers)

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_token_whose_role_no_longer_matches_the_database_is_401(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    anita = await session.scalar(select(StaffUser).where(StaffUser.email == "anita@example.com"))
    assert anita is not None
    admin_claim = forged_token(subject=str(anita.id), role=Role.ADMIN)  # DB says reviewer

    response = await client.get("/me", headers=bearer(admin_claim))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_staff_token_with_a_non_uuid_subject_is_401(client: httpx.AsyncClient) -> None:
    token = forged_token(subject="seed|priya", role=Role.REVIEWER)

    response = await client.get("/me", headers=bearer(token))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_unknown_staff_id_in_a_valid_token_is_401(client: httpx.AsyncClient) -> None:
    token = forged_token(subject=str(uuid4()), role=Role.REVIEWER)

    response = await client.get("/me", headers=bearer(token))

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_identity_comes_from_the_token_not_the_request(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("priya@example.com")

    response = await client.get(
        "/me?email=arjun@example.com", headers={**headers, "X-Customer-Id": "seed|arjun"}
    )

    assert response.json()["email"] == "priya@example.com"


# ---------------------------------------------------------------- role checks


async def test_reviewer_only_route_returns_403_for_a_customer(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("priya@example.com")

    response = await client.get("/test/reviewer-only", headers=headers)

    assert_error(response, 403, "FORBIDDEN")


async def test_reviewer_only_route_allows_a_reviewer(
    client: httpx.AsyncClient, auth_headers: Callable[[str], Awaitable[dict[str, str]]]
) -> None:
    headers = await auth_headers("karan@example.com")

    response = await client.get("/test/reviewer-only", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"reviewer": "Karan Malhotra"}


async def test_reviewer_only_route_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/reviewer-only")

    assert_error(response, 401, "NOT_AUTHENTICATED")


async def test_admin_passes_the_reviewer_check(
    client: httpx.AsyncClient,
    session: AsyncSession,
    auth_headers: Callable[[str], Awaitable[dict[str, str]]],
    demo_password_hash: str,
) -> None:
    session.add(
        StaffUser(
            name="Root",
            email="root@example.com",
            role=StaffRole.ADMIN,
            password_hash=demo_password_hash,
        )
    )
    await session.flush()
    headers = await auth_headers("root@example.com")

    response = await client.get("/test/reviewer-only", headers=headers)

    assert response.status_code == 200


# ---------------------------------------------------------------- errors


async def test_unknown_route_uses_the_error_format(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-such-route")

    assert_error(response, 404, "NOT_FOUND")
