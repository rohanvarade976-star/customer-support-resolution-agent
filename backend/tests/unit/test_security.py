"""Password hashing and JWT behaviour. No database needed."""

import base64
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from support_agent.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from support_agent.domain.enums import Role

SECRET = "unit-test-secret-" + "x" * 20
OTHER_SECRET = "a-completely-different-secret-" + "y" * 10


def make_token(
    *, secret: str = SECRET, lifetime: timedelta = timedelta(minutes=5), now: datetime | None = None
) -> str:
    return create_access_token(
        subject="seed|priya", role=Role.CUSTOMER, secret=secret, lifetime=lifetime, now=now
    )


# ---------------------------------------------------------------- passwords


def test_correct_password_verifies() -> None:
    assert verify_password("correct horse", hash_password("correct horse"))


def test_wrong_password_does_not_verify() -> None:
    assert not verify_password("wrong", hash_password("correct horse"))


def test_hash_is_argon2_and_never_the_plain_password() -> None:
    stored = hash_password("correct horse")

    assert stored.startswith("$argon2")
    assert "correct horse" not in stored


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password("same") != hash_password("same")  # random salt


# ---------------------------------------------------------------- tokens


def test_token_round_trip_carries_subject_role_and_times() -> None:
    claims = decode_access_token(make_token(lifetime=timedelta(minutes=5)), secret=SECRET)

    assert claims.sub == "seed|priya"
    assert claims.role == Role.CUSTOMER
    assert claims.exp - claims.iat == 300


def test_expired_token_is_rejected() -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=2)

    with pytest.raises(InvalidTokenError):
        decode_access_token(make_token(now=long_ago, lifetime=timedelta(minutes=5)), secret=SECRET)


def test_token_signed_with_another_secret_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(make_token(secret=OTHER_SECRET), secret=SECRET)


def test_tampered_payload_is_rejected() -> None:
    header, _payload, signature = make_token().split(".")
    forged_payload = b'{"sub":"seed|priya","role":"admin","iat":1,"exp":9999999999}'
    forged = base64.urlsafe_b64encode(forged_payload).rstrip(b"=").decode()

    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{forged}.{signature}", secret=SECRET)


def test_unsigned_alg_none_token_is_rejected() -> None:
    unsigned = jwt.encode(
        {"sub": "seed|priya", "role": "admin", "iat": 1, "exp": 9999999999},
        key="",  # PyJWT's "none" algorithm takes an empty key (typed as str)
        algorithm="none",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned, secret=SECRET)


def test_token_missing_a_required_claim_is_rejected() -> None:
    no_role = jwt.encode(
        {"sub": "seed|priya", "iat": 1, "exp": 9999999999}, SECRET, algorithm="HS256"
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(no_role, secret=SECRET)


def test_token_with_unknown_role_is_rejected() -> None:
    odd_role = jwt.encode(
        {"sub": "x", "role": "superuser", "iat": 1, "exp": 9999999999}, SECRET, algorithm="HS256"
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(odd_role, secret=SECRET)


def test_garbage_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-jwt", secret=SECRET)
