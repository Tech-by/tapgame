# auth.py
"""
Authentication helpers for the Telegram Mini App backend.

Responsibilities:
  1. Verify Telegram WebApp initData (HMAC-SHA256 signature check)
  2. Issue JWT access tokens for authenticated users
  3. Decode & validate JWT access tokens on protected routes

Keys are read from the .env file -- never hardcode them here.
"""

import os
import json
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import HTTPException, status
from jose import JWTError, jwt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7        # 7 days
INIT_DATA_MAX_AGE_SECONDS = 60 * 60 * 24         # reject initData older than 24h
CLOCK_SKEW_SECONDS = 60                           # tolerance for slow clocks

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is missing. Add it to your .env file.")


# ---------------------------------------------------------------------------
# 1. Telegram initData validation
# ---------------------------------------------------------------------------

def _build_secret_key() -> bytes:
    """
    Telegram's WebApp secret key.
    secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)
    """
    return hmac.new(
        key=b"WebAppData",
        msg=BOT_TOKEN.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()


def validate_init_data(init_data: str) -> Dict[str, Any]:
    """
    Verify the `initData` string sent by the Telegram WebApp.

    Returns a dict of the parsed fields (with `user` decoded into a dict).
    Raises HTTPException(401) if the signature or freshness check fails.

    Docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not isinstance(init_data, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData is missing or malformed",
        )

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not parse initData: {exc}",
        )

    # `hash` is the signature we must verify.
    # `signature` is a newer field used for third-party validation and is
    # NOT part of the data-check-string, so it must be excluded too.
    received_hash = parsed.pop("hash", None)
    parsed.pop("signature", None)

    if not received_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData is missing the hash field",
        )

    # Data-check-string: all fields sorted alphabetically, joined by "\n"
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )

    calculated_hash = hmac.new(
        key=_build_secret_key(),
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to avoid timing attacks
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData signature is invalid",
        )

    # --- Freshness check (prevents replaying an old captured initData) ---
    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData is missing auth_date",
        )

    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData auth_date is invalid",
        )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if now_ts - auth_date > INIT_DATA_MAX_AGE_SECONDS + CLOCK_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="initData has expired, please reopen the app",
        )

    # --- Decode the `user` field (it is a JSON string) ---
    user_raw = parsed.get("user")
    if user_raw:
        try:
            parsed["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="initData user field is not valid JSON",
            )

    return parsed


def extract_telegram_user(parsed_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull the user object out of validated initData.
    Raises HTTPException(400) if no user is present.
    """
    user = parsed_data.get("user")
    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User data not found in initData",
        )
    return user


# ---------------------------------------------------------------------------
# 2. JWT creation
# ---------------------------------------------------------------------------

def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT.

    `data` must contain the "sub" claim (we use the Telegram user ID as a string).
    """
    to_encode = data.copy()

    # JWT spec requires "sub" to be a string
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])

    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update(
        {
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
    )

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ---------------------------------------------------------------------------
# 3. JWT decoding
# ---------------------------------------------------------------------------

def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT. Raises HTTPException(401) if invalid or expired.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing the subject claim",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def get_telegram_id_from_token(token: str) -> int:
    """
    Convenience helper: decode a token and return the Telegram user ID as int.
    """
    payload = decode_access_token(token)
    try:
        return int(payload["sub"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is invalid",
        )


def extract_bearer_token(authorization: Optional[str]) -> str:
    """
    Pull the raw token out of an `Authorization: Bearer <token>` header.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()
