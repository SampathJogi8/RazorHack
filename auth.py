"""
auth.py — Authentication & User Management for RecoverAI
---------------------------------------------------------
Handles user registration, authentication, password hashing with PBKDF2-HMAC,
session management, and pre-seeded demo accounts for quick evaluator access.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, EmailStr

USERS_FILE = Path("data/users.json")
SESSIONS_FILE = Path("data/sessions.json")


class UserPublic(BaseModel):
    id: str
    name: str
    email: str
    company: str
    role: str
    avatar_initials: str
    created_at: str


class UserRecord(BaseModel):
    id: str
    name: str
    email: str
    company: str
    role: str
    password_hash: str
    salt: str
    created_at: str

    def to_public(self) -> UserPublic:
        names = self.name.strip().split()
        initials = (names[0][0] + (names[-1][0] if len(names) > 1 else "")).upper()
        return UserPublic(
            id=self.id,
            name=self.name,
            email=self.email,
            company=self.company,
            role=self.role,
            avatar_initials=initials or "RA",
            created_at=self.created_at,
        )


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return key.hex(), salt


def _verify_password(password: str, salt: str, password_hash: str) -> bool:
    expected_hash, _ = _hash_password(password, salt)
    return secrets.compare_digest(expected_hash, password_hash)


# Pre-seeded demo accounts for evaluator / judge convenience
PRESEEDED_USERS = [
    {
        "id": "usr_ops_01",
        "name": "Priya Sharma",
        "email": "ops@recoverai.io",
        "company": "Razorpay Merchant Ops",
        "role": "Revenue Recovery Lead",
        "password": "recovery123",
    },
    {
        "id": "usr_cfo_02",
        "name": "Amit Joshi",
        "email": "cfo@razorpay.com",
        "company": "FinTech Enterprise",
        "role": "Chief Financial Officer",
        "password": "admin123",
    },
    {
        "id": "usr_risk_03",
        "name": "Neha Gupta",
        "email": "risk@finance.com",
        "company": "Razorpay Risk & Compliance",
        "role": "Risk & Compliance Manager",
        "password": "risk123",
    },
]


class AuthManager:
    def __init__(self):
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, str] = {}  # token -> user_id
        self._load_data()

    def _load_data(self):
        if USERS_FILE.exists():
            try:
                with open(USERS_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                    for u in raw:
                        self.users[u["email"].lower()] = UserRecord(**u)
            except Exception:
                self._seed_default_users()
        else:
            self._seed_default_users()

        if SESSIONS_FILE.exists():
            try:
                with open(SESSIONS_FILE, encoding="utf-8") as f:
                    self.sessions = json.load(f)
            except Exception:
                self.sessions = {}

    def _seed_default_users(self):
        self.users = {}
        now = datetime.now(timezone.utc).isoformat()
        for seed in PRESEEDED_USERS:
            pwd_hash, salt = _hash_password(seed["password"])
            rec = UserRecord(
                id=seed["id"],
                name=seed["name"],
                email=seed["email"].lower(),
                company=seed["company"],
                role=seed["role"],
                password_hash=pwd_hash,
                salt=salt,
                created_at=now,
            )
            self.users[rec.email] = rec
        self._save_users()

    def _save_users(self):
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump([u.model_dump() for u in self.users.values()], f, indent=2)
        except Exception as e:
            print(f"Warning: could not save users: {e}")

    def _save_sessions(self):
        try:
            with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.sessions, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save sessions: {e}")

    def authenticate(self, email: str, password: str) -> tuple[Optional[UserPublic], Optional[str], Optional[str]]:
        email_clean = email.strip().lower()
        user = self.users.get(email_clean)
        if not user:
            return None, None, "Invalid email or password"

        if not _verify_password(password, user.salt, user.password_hash):
            return None, None, "Invalid email or password"

        token = f"recov_{secrets.token_urlsafe(32)}"
        self.sessions[token] = user.id
        self._save_sessions()
        return user.to_public(), token, None

    def register(
        self,
        name: str,
        email: str,
        company: str,
        role: str,
        password: str,
    ) -> tuple[Optional[UserPublic], Optional[str], Optional[str]]:
        email_clean = email.strip().lower()
        if not name or len(name.strip()) < 2:
            return None, None, "Full name must be at least 2 characters"
        if not email_clean or "@" not in email_clean:
            return None, None, "Valid email address is required"
        if len(password) < 6:
            return None, None, "Password must be at least 6 characters"

        if email_clean in self.users:
            return None, None, "An account with this email already exists"

        now = datetime.now(timezone.utc).isoformat()
        user_id = f"usr_{secrets.token_hex(6)}"
        pwd_hash, salt = _hash_password(password)

        rec = UserRecord(
            id=user_id,
            name=name.strip(),
            email=email_clean,
            company=company.strip() or "Razorpay Merchant",
            role=role.strip() or "Revenue Recovery Specialist",
            password_hash=pwd_hash,
            salt=salt,
            created_at=now,
        )
        self.users[email_clean] = rec
        self._save_users()

        token = f"recov_{secrets.token_urlsafe(32)}"
        self.sessions[token] = rec.id
        self._save_sessions()

        return rec.to_public(), token, None

    def get_user_by_token(self, token: str) -> Optional[UserPublic]:
        if not token:
            return None
        user_id = self.sessions.get(token)
        if not user_id:
            return None
        for u in self.users.values():
            if u.id == user_id:
                return u.to_public()
        return None

    def logout(self, token: str) -> bool:
        if token in self.sessions:
            del self.sessions[token]
            self._save_sessions()
            return True
        return False

    def get_demo_accounts(self) -> list[dict]:
        demos = []
        for p in PRESEEDED_USERS:
            demos.append({
                "name": p["name"],
                "email": p["email"],
                "role": p["role"],
                "company": p["company"],
                "password": p["password"],
            })
        return demos


auth_manager = AuthManager()
