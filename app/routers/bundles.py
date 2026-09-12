"""HTTP routes for dual-site coordinated uplink lease bundles."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import engine
from app.errors import APIError
from app.schemas import BundleAcquireRequest, BundleAcquireResponse
from app.services import acquire_lease_bundle

router = APIRouter(tags=["lease-bundles"])


def get_session():
    # Plain transactional scope: the service layer decides commit/rollback
    # boundaries explicitly.
    with Session(engine, future=True) as session:
        yield session


@router.post(
    "/lease-bundles",
    response_model=BundleAcquireResponse,
    status_code=200,
    summary="双站协同上行：原子获取两副天线的控制租约（全有或全无）",
)
def acquire_bundle(
    payload: BundleAcquireRequest, session: Session = Depends(get_session)
):
    try:
        result = acquire_lease_bundle(
            session.connection(),
            antenna_ids=payload.antenna_ids,
            controller=payload.controller,
            duration_seconds=payload.duration_seconds,
            idempotency_key=payload.idempotency_key,
        )
    except APIError:
        session.rollback()
        raise
    session.commit()
    return result
