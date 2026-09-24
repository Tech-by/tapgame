# models.py
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True, unique=True)
    username: Optional[str] = None
    first_name: Optional[str] = None
    language_code: Optional[str] = None

    # Auto-farm
    balance: int = Field(default=0)
    farm_cap: int = Field(default=500)
    farm_remaining: int = Field(default=500)
    farm_rate: float = Field(default=1.0)          # points per second
    last_farm_update: datetime = Field(default_factory=_utcnow)

    # Ads
    ads_watched_today: int = Field(default=0)
    last_ad_reset: datetime = Field(default_factory=_utcnow)

    # Referral
    referred_by: Optional[int] = Field(default=None, index=True)
    referral_count: int = Field(default=0)

    created_at: datetime = Field(default_factory=_utcnow)


class Impression(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True)
    block_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
