# main.py
"""
Telegram Mini App backend -- FastAPI application.

Endpoints:
  GET  /                  -> redirects to /app/index.html
  GET  /health            -> simple health check
  POST /auth              -> verify Telegram initData, return JWT
  GET  /user/me           -> current user profile
  POST /tap               -> handle a tap (legacy, kept for compatibility)
  POST /reward            -> handle rewarded-ad completion (resets farm)
  POST /referral          -> record a referral
  GET  /admin/stats       -> admin-only summary (protected by ADMIN_USER_ID)
  GET  /app/index.html    -> serves the Mini App frontend
"""

import os
from datetime import datetime, timezone, date
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from dotenv import load_dotenv

from database import create_db_and_tables, get_session
from models import User, Impression
from auth import (
    validate_init_data,
    extract_telegram_user,
    create_access_token,
    extract_bearer_token,
    get_telegram_id_from_token,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]


def _parse_admin_id(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        print(
            f"[config] WARNING: ADMIN_USER_ID='{raw}' is not a valid number. "
            "Admin routes disabled."
        )
        return None


ADMIN_USER_ID = _parse_admin_id(os.getenv("ADMIN_USER_ID"))

# Game tuning
DEFAULT_FARM_CAP = 500
DEFAULT_FARM_RATE = 1.0            # points per second
DAILY_AD_LIMIT = 15
REFERRAL_BONUS = 500

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Auto-Farm API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()
    # Start the admin bot in a background thread
    try:
        from admin_bot import start_admin_bot_background
        start_admin_bot_background()
    except Exception as e:
        print(f"[startup] admin bot failed to start: {e}")


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class AuthRequest(BaseModel):
    init_data: str = Field(..., description="window.Telegram.WebApp.initData")


class ReferralRequest(BaseModel):
    referral_code: str = Field(..., description="Referrer's Telegram ID as string")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> User:
    """Resolve the authenticated user from the Bearer token."""
    token = extract_bearer_token(authorization)
    telegram_id = get_telegram_id_from_token(token)

    user = session.exec(
        select(User).where(User.telegram_id == telegram_id)
    ).first()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Only allow the configured admin to access admin routes."""
    if ADMIN_USER_ID is None or current_user.telegram_id != ADMIN_USER_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_farming(user: User) -> int:
    """Credit pending farm points. Returns the amount credited."""
    now = datetime.now(timezone.utc)
    last = user.last_farm_update
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    elapsed = max(0.0, (now - last).total_seconds())
    potential = int(elapsed * user.farm_rate)
    earned = min(potential, user.farm_remaining)

    if earned > 0:
        user.balance += earned
        user.farm_remaining -= earned

    user.last_farm_update = now
    return earned


def _reset_daily_ad_counter_if_needed(user: User) -> None:
    """Reset ads_watched_today if the calendar day has changed (UTC)."""
    now = datetime.now(timezone.utc)
    last = user.last_ad_reset
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last.date() < now.date():
        user.ads_watched_today = 0
        user.last_ad_reset = now


def _serialize_user(user: User) -> dict:
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "balance": user.balance,
        "farm_remaining": user.farm_remaining,
        "farm_cap": user.farm_cap,
        "farm_rate": user.farm_rate,
        "ads_watched_today": user.ads_watched_today,
        "daily_ad_limit": DAILY_AD_LIMIT,
        "referral_count": user.referral_count,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """Redirect the bare domain to the Mini App frontend."""
    return RedirectResponse(url="/app/index.html")


@app.get("/health")
async def health_check():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.post("/auth")
async def authenticate(
    auth_data: AuthRequest,
    session: Session = Depends(get_session),
):
    """Validate Telegram initData, create/return the user, issue a JWT."""
    parsed = validate_init_data(auth_data.init_data)
    info = extract_telegram_user(parsed)
    telegram_id = int(info["id"])

    user = session.exec(
        select(User).where(User.telegram_id == telegram_id)
    ).first()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=info.get("username"),
            first_name=info.get("first_name"),
            language_code=info.get("language_code"),
            balance=0,
            farm_cap=DEFAULT_FARM_CAP,
            farm_remaining=DEFAULT_FARM_CAP,
            farm_rate=DEFAULT_FARM_RATE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    else:
        _apply_farming(user)
        _reset_daily_ad_counter_if_needed(user)
        session.add(user)
        session.commit()
        session.refresh(user)

    token = create_access_token(data={"sub": telegram_id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _serialize_user(user),
    }


@app.get("/user/me")
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return the authenticated user's profile with regenerated farm points."""
    _apply_farming(current_user)
    _reset_daily_ad_counter_if_needed(current_user)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return _serialize_user(current_user)


@app.post("/tap")
async def handle_tap(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """
    Legacy tap endpoint. Kept for compatibility.
    The auto-farm frontend no longer uses this, but some clients may still call it.
    """
    _apply_farming(current_user)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return _serialize_user(current_user)


@app.post("/reward")
async def handle_reward(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Called after a rewarded ad completes. Resets the farm session and logs an impression."""
    _apply_farming(current_user)
    _reset_daily_ad_counter_if_needed(current_user)

    if current_user.ads_watched_today >= DAILY_AD_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily ad limit reached",
        )

    # Reset farm session
    current_user.farm_remaining = current_user.farm_cap
    current_user.last_farm_update = datetime.now(timezone.utc)
    current_user.ads_watched_today += 1

    impression = Impression(
        telegram_id=current_user.telegram_id,
        block_id="rewarded-video",
        created_at=datetime.now(timezone.utc),
    )
    session.add(impression)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return _serialize_user(current_user)


@app.post("/referral")
async def handle_referral(
    referral_data: ReferralRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Record a referral from the current user (they must be new)."""
    if current_user.referred_by is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already referred",
        )

    try:
        referrer_id = int(referral_data.referral_code)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid referral code",
        )

    if referrer_id == current_user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot refer yourself",
        )

    referrer = session.exec(
        select(User).where(User.telegram_id == referrer_id)
    ).first()

    if not referrer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referrer not found",
        )

    current_user.referred_by = referrer_id
    referrer.referral_count += 1
    referrer.balance += REFERRAL_BONUS

    session.add(current_user)
    session.add(referrer)
    session.commit()

    return {
        "message": "Referral recorded",
        "bonus": REFERRAL_BONUS,
    }


@app.get("/admin/stats")
async def admin_stats(
    _admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Summary stats for the admin bot to consume."""
    total_users = len(session.exec(select(User)).all())

    today_start = datetime.combine(
        date.today(), datetime.min.time()
    ).replace(tzinfo=timezone.utc)

    impressions_today = session.exec(
        select(Impression).where(Impression.created_at >= today_start)
    ).all()

    return {
        "total_users": total_users,
        "impressions_today": len(impressions_today),
        "daily_ad_limit": DAILY_AD_LIMIT,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
# ⚠️ MUST be the last line. Any route defined after this will be shadowed
# by the static mount.
app.mount("/app", StaticFiles(directory=".", html=True), name="static")
