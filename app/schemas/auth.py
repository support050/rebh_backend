from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from typing import Optional

# Hashing is intentionally expensive (argon2id 64 MiB / t=3), so an unbounded
# password is a cheap CPU denial-of-service on every hit to /login — including
# the dummy-hash timing burn. OWASP allows passphrases far longer than this, so
# a cap here costs no real user.
MAX_PASSWORD_LENGTH = 128


def normalize_email(value):
    """Canonical identity form: trimmed + lowercase.

    Postgres compares `users.email` case-sensitively (unique index too), so
    without this 'A@Gmail.com' and 'a@gmail.com' would be two distinct
    accounts and the other spelling could never log in. Applied mode="before"
    so EmailStr still validates the result.
    """
    if isinstance(value, str):
        return value.strip().lower()
    return value


class NormalizedEmailModel(BaseModel):
    """Base for request bodies carrying a user-supplied email.

    check_fields=False: the field is declared by the subclasses, not here.
    """

    @field_validator("email", mode="before", check_fields=False)
    @classmethod
    def _normalize_email(cls, value):
        return normalize_email(value)


class UserLogin(NormalizedEmailModel):
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

class UserRegister(NormalizedEmailModel):
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    full_name: Optional[str] = None

class UserUpdate(NormalizedEmailModel):
    full_name: Optional[str] = None
    password: Optional[str] = Field(default=None, max_length=MAX_PASSWORD_LENGTH)
    email: Optional[EmailStr] = None
    current_password: Optional[str] = Field(default=None, max_length=MAX_PASSWORD_LENGTH)

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: Optional[str] = None
    is_verified: bool
    is_approved: bool = False
    is_admin: bool = False

class ForgetPasswordRequest(NormalizedEmailModel):
    email: EmailStr


class AdminRoleUpdate(BaseModel):
    """Admin-only privilege switch for another account (see /api/admin/users/{id}/role)."""
    is_admin: bool

class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

class OAuthConfirmLinkRequest(BaseModel):
    """Prove control of an existing password account before linking OAuth."""
    link_token: str
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class DeleteAccountRequest(BaseModel):
    """Irreversible self-service deletion — the password is the re-auth.

    A borrowed machine or a hijacked 15-minute cookie used to be enough to wipe
    an entire account and every row attached to it. Nothing about the request
    carries the password, and it is never logged or echoed back.
    """
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
