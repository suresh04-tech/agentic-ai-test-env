"""``GET /api/users`` — read path over PostgreSQL."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import fetch_users, get_db
from app.logging_config import get_logger
from app.metrics import app_records_fetched_total
from app.routes._common import db_http_exception

router = APIRouter(prefix="/api", tags=["users"])
logger = get_logger(__name__)


@router.get("/users", summary="List users")
def list_users(
    session: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500, description="Maximum rows to return"),
) -> dict:
    try:
        users = fetch_users(session, limit=limit)
    except Exception as exc:
        raise db_http_exception(exc, "select_users") from exc

    app_records_fetched_total.labels(entity="users").inc(len(users))
    logger.info(
        "Users fetched",
        extra={"operation": "select_users", "status": "success", "rows": len(users)},
    )
    return {"count": len(users), "users": [user.to_dict() for user in users]}
