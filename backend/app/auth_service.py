from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from .auth_models import LoginRequest, RegisterRequest, UserAccount
from .storage import (clear_login_failures, create_session_record, create_user_account,
                      get_user_by_email, get_user_for_session, record_login_failure)


SESSION_DAYS = 7
MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 5
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize_email(value: str) -> str:
    return value.strip().casefold()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return "pbkdf2_sha256$600000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     base64.urlsafe_b64decode(salt), int(iterations), dklen=32)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def _public_user(row: sqlite3.Row | dict[str, str]) -> UserAccount:
    return UserAccount(id=row["id"], email=row["email"], display_name=row["display_name"],
                       created_at=row["created_at"])


def _validate_password(password: str) -> None:
    if not 8 <= len(password) <= 128:
        raise ValueError("密码长度需要在 8 到 128 个字符之间")
    categories = sum((any(char.isalpha() for char in password), any(char.isdigit() for char in password),
                      any(not char.isalnum() for char in password)))
    if categories < 2:
        raise ValueError("密码至少需要包含字母、数字或符号中的两类")


def register(payload: RegisterRequest) -> tuple[UserAccount, str]:
    email = _normalize_email(payload.email)
    password = payload.password.get_secret_value()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("请输入有效的邮箱地址")
    _validate_password(password)
    try:
        row = create_user_account(email, payload.display_name.strip(), _hash_password(password))
    except sqlite3.IntegrityError as exc:
        raise ValueError("该邮箱已经注册") from exc
    token = secrets.token_urlsafe(32)
    create_session_record(hashlib.sha256(token.encode()).hexdigest(), row["id"], SESSION_DAYS)
    return _public_user(row), token


def login(payload: LoginRequest) -> tuple[UserAccount, str]:
    email = _normalize_email(payload.email)
    password = payload.password.get_secret_value()
    row = get_user_by_email(email)
    now = datetime.now(timezone.utc)
    if row and row["locked_until"]:
        locked_until = datetime.fromisoformat(row["locked_until"])
        if locked_until > now:
            raise PermissionError("登录尝试次数过多，请稍后再试")
    if not row or not _verify_password(password, row["password_hash"]):
        record_login_failure(email, MAX_LOGIN_FAILURES, LOCKOUT_MINUTES)
        raise PermissionError("账号或密码不正确")
    clear_login_failures(row["id"])
    token = secrets.token_urlsafe(32)
    create_session_record(hashlib.sha256(token.encode()).hexdigest(), row["id"], SESSION_DAYS)
    return _public_user(row), token


def user_for_token(token: str) -> UserAccount | None:
    if not token:
        return None
    row = get_user_for_session(hashlib.sha256(token.encode()).hexdigest())
    return _public_user(row) if row else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
