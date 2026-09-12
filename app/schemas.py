"""Pydantic models for the antenna control lease API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import MAX_LEASE_SECONDS, MIN_LEASE_SECONDS


class AcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    antenna_id: str = Field(..., min_length=1, max_length=64)
    controller: str = Field(..., min_length=1, max_length=128)
    duration_seconds: int = Field(
        ...,
        ge=MIN_LEASE_SECONDS,
        le=MAX_LEASE_SECONDS,
        description=f"租约时长，闭区间 [{MIN_LEASE_SECONDS}, {MAX_LEASE_SECONDS}] 秒。",
    )
    idempotency_key: str = Field(..., min_length=1, max_length=128)

    @field_validator("antenna_id", "controller", "idempotency_key")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("不能为空或纯空白。")
        return value


class AcquireResponse(BaseModel):
    antenna_id: str
    controller: str
    lease_token: str
    acquired_at: datetime
    expires_at: datetime
    replay: bool = False
