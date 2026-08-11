"""Integration tests — require a real PostgreSQL database.

Opt in by exporting a connection string. Nothing here runs by default, and the
unit suite never reaches a real database.

    export INTEGRATION_DATABASE_URL='postgresql://user:pass@host:5432/db'
    pytest -m integration

These tests build their own engine rather than reusing the app's cached
settings, so the unit suite's in-memory SQLite configuration stays untouched.
They only create the app's own two tables, insert into them, and run a
read-only ``pg_sleep`` — nothing destructive.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.database import _normalise_url
from app.models import Base, Order, User

INTEGRATION_URL = os.getenv("INTEGRATION_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_URL,
        reason="set INTEGRATION_DATABASE_URL to run PostgreSQL integration tests",
    ),
]


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(
        _normalise_url(INTEGRATION_URL),
        future=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        yield engine
    finally:
        engine.dispose()


def test_connectivity(engine):
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_it_is_actually_postgresql(engine):
    assert engine.dialect.name == "postgresql"


def test_schema_creation_is_idempotent(engine):
    Base.metadata.create_all(bind=engine)
    Base.metadata.create_all(bind=engine)  # second run must be a no-op

    with Session(engine) as session:
        assert session.execute(select(func.count(User.id))).scalar_one() >= 0
        assert session.execute(select(func.count(Order.id))).scalar_one() >= 0


def test_insert_and_read_round_trip(engine):
    Base.metadata.create_all(bind=engine)
    marker = f"integration-{int(time.time())}@example.test"

    with Session(engine) as session:
        user = User(name="Integration Probe", email=marker)
        session.add(user)
        session.flush()
        session.add(Order(user_id=user.id, amount=42.50, status="paid"))
        session.commit()
        user_id = user.id

    with Session(engine) as session:
        found = session.get(User, user_id)
        assert found is not None and found.email == marker
        assert found.created_at is not None
        orders = list(
            session.execute(select(Order).where(Order.user_id == user_id)).scalars()
        )
        assert len(orders) == 1
        assert float(orders[0].amount) == 42.50

    # Clean up only the row this test created.
    with Session(engine) as session:
        session.execute(text("DELETE FROM orders WHERE user_id = :uid"), {"uid": user_id})
        session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
        session.commit()


def test_pg_sleep_produces_real_server_side_latency(engine):
    with Session(engine) as session:
        started = time.perf_counter()
        session.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": 1})
        elapsed = time.perf_counter() - started
    assert 0.9 <= elapsed < 10
