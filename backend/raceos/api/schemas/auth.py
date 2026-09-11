"""Auth request and response shapes."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from raceos.domain.enums import AccountState, AthleteLevel, Currency, UnitSystem, UserTier


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=512)
    name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=512)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    new_password: str = Field(min_length=10, max_length=512)


class UserOut(BaseModel):
    """The signed-in athlete. Carries no secrets and no other user's data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str | None
    units: UnitSystem
    level: AthleteLevel
    tier: UserTier
    currency: Currency
    is_coach: bool
    account_state: AccountState
    avatar_url: str | None
    email_verified_at: datetime | None
    country: str | None
    #: Equipment and personal facts the settings screen edits. Present on the
    #: athlete's own record only — no endpoint returns another user's.
    date_of_birth: date | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    created_at: datetime | None = None


class ProfileUpdate(BaseModel):
    """What an athlete may change about themselves.

    Deliberately excludes ``email`` and ``tier``. Changing an email is an
    identity change and needs re-verification to be one — a field on a settings
    form that silently moved someone's sign-in would be an account-takeover
    primitive. Tier is what the billing system decided, so letting a client
    send it would make the paywall a suggestion.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    date_of_birth: date | None = None
    country: str | None = Field(default=None, max_length=2)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_phone: str | None = Field(default=None, max_length=40)
    units: UnitSystem | None = None
    level: AthleteLevel | None = None
    currency: Currency | None = None


class AuthResponse(BaseModel):
    """The refresh token is set as an httpOnly cookie, never in this body."""

    user: UserOut
    access_token: str
    token_type: str = "bearer"
    expires_in: int
