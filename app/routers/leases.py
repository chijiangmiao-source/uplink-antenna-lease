"""HTTP routes for antenna control leases."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import engine
from app.errors import APIError
from app.schemas import AcquireRequest, AcquireResponse
from app.services import acquire_lease, get_lease_by_token

router = APIRouter(tags=["leases"])


def get_session():
    # Plain transactional scope: the service layer decides commit/rollback
    # boundaries explicitly.
    with Session(engine, future=True) as session:
        yield session


@router.post(
    "/leases",
    response_model=AcquireResponse,
    status_code=200,
    summary="原子获取一副天线的控制租约",
)
def acquire(payload: AcquireRequest, session: Session = Depends(get_session)):
    try:
        result = acquire_lease(
            session.connection(),
            antenna_id=payload.antenna_id,
            controller=payload.controller,
            duration_seconds=payload.duration_seconds,
            idempotency_key=payload.idempotency_key,
        )
    except APIError:
        session.rollback()
        raise
    session.commit()
    return result


@router.get("/leases/{lease_token}", summary="按令牌查询租约状态")
def lease_status(lease_token: str, session: Session = Depends(get_session)):
    result = get_lease_by_token(session.connection(), lease_token)
    if result is None:
        session.rollback()
        raise APIError(
            404,
            "LEASE_NOT_FOUND",
            "未知租约令牌。",
            {"lease_token": lease_token},
        )
    session.commit()
    return {
        "antenna_id": result["antenna_id"],
        "controller": result["controller"],
        "lease_token": result["token"],
        "acquired_at": result["acquired_at"],
        "expires_at": result["expires_at"],
        "active": result["active"],
    }
