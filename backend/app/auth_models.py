from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, SecretStr


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr
    display_name: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr


class UserAccount(BaseModel):
    id: str
    email: str
    display_name: str = ""
    created_at: datetime


class AuthSession(BaseModel):
    user: UserAccount
