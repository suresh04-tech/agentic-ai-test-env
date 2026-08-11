"""``GET /api/orders`` — read path over PostgreSQL."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import fetch_orders, get_db
from app.logging_config import get_logger
from app.metrics import app_records_fetched_total
from app.models import ORDER_STATUSES
from app.routes._common import db_http_exception

router = APIRouter(prefix="/api", tags=["orders"])
logger = get_logger(__name__)


@router.get("/orders", summary="List orders")
def list_orders(
    session: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500, description="Maximum rows to return"),
    status: str | None = Query(
        None,
        description=f"Optional status filter. One of: {', '.join(ORDER_STATUSES)}",
    ),
) -> dict:
    # Validated against a fixed set so the value can never widen the label set
    # on any metric derived from it.
    if status is not None and status not in ORDER_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_status",
                "message": f"status must be one of: {', '.join(ORDER_STATUSES)}",
            },
        )

    try:
        orders = fetch_orders(session, limit=limit, status=status)
    except Exception as exc:
        raise db_http_exception(exc, "select_orders") from exc

    app_records_fetched_total.labels(entity="orders").inc(len(orders))
    logger.info(
        "Orders fetched",
        extra={"operation": "select_orders", "status": "success", "rows": len(orders)},
    )
    return {"count": len(orders), "orders": [order.to_dict() for order in orders]}
