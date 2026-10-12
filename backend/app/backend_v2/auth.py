"""V2 PostgreSQL sessions; no default password and no client-supplied user identity."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .repository import new_id


def hash_password(value: str) -> str:
    salt = secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 600_000)
    return "pbkdf2_sha256$600000$%s$%s" % (base64.urlsafe_b64encode(salt).decode(),
                                         base64.urlsafe_b64encode(result).decode())


def verify_password(value: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, expected = encoded.split("$")
        if scheme != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, db):
        self.db = db

    def register(self, email: str, password: str, display_name: str) -> dict:
        user_id = new_id()
        password_hash = hash_password(password)
        with self.db.transaction(user_id) as cx:
            cx.execute(text("""INSERT INTO zhida.users(id,email,password_hash,display_name)
                VALUES(:id,:email,:hash,:name)"""), {"id": user_id, "email": email.strip().casefold(),
                                                   "hash": password_hash, "name": display_name})
        return {"id": user_id, "email": email.strip().casefold(), "display_name": display_name}

    def login(self, email: str, password: str) -> dict | None:
        with self.db.transaction() as cx:
            row = cx.execute(text("SELECT * FROM zhida.users WHERE email=:email"),
                             {"email": email.strip().casefold()}).mappings().first()
        if not row or not row["password_hash"] or not verify_password(password, row["password_hash"]):
            # Keep negative account lookups computationally comparable to password verification.
            if not row or not row["password_hash"]:
                hash_password(password)
            return None
        token = secrets.token_urlsafe(32)
        with self.db.transaction(row["id"]) as cx:
            cx.execute(text("""INSERT INTO zhida.auth_sessions(token_hash,user_id,expires_at)
                VALUES(:hash,:u,:expires)"""), {"hash": token_digest(token), "u": row["id"],
                  "expires": datetime.now(timezone.utc) + timedelta(days=7)})
        return {"user_id": row["id"], "token": token}

    def authenticate(self, token: str) -> str | None:
        if not token or len(token) > 512:
            return None
        with self.db.transaction() as cx:
            return cx.execute(text("""SELECT user_id FROM zhida.auth_sessions
                WHERE token_hash=:hash AND expires_at>now()"""), {"hash": token_digest(token)}).scalar_one_or_none()

    def logout(self, token: str) -> None:
        with self.db.transaction() as cx:
            cx.execute(text("DELETE FROM zhida.auth_sessions WHERE token_hash=:hash"), {"hash": token_digest(token)})
