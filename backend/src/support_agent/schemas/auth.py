"""Request and response models for login and "who am I"."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.domain.enums import Role


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Length limits keep a huge password from making the (slow) hash do huge work.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (a label, not a password)
    expires_in: int  # seconds until the token expires


class Principal(BaseModel):
    """The verified identity behind a request. Built only from a valid token + database row."""

    model_config = ConfigDict(frozen=True)

    id: UUID  # customers.id or staff_users.id
    role: Role
    name: str
    email: str


class MeResponse(BaseModel):
    id: UUID
    role: Role
    name: str
    email: str
