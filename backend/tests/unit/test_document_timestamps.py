"""Document.created_at must be a timezone-aware UTC instant end to end.

Regression for the unit-test break when sqlmodel 0.0.47 began mapping ``datetime``
fields to a timezone-aware column that rejects naive values: the model defaulted
to ``datetime.utcnow()`` (naive), so every INSERT into ``documents`` raised
``StatementError: Datetime values must have timezone information``. These tests
pin the behaviour to the model itself rather than to whichever sqlmodel version
happens to be installed.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel

from app.models.document import Document

UTC_OFFSET = timedelta(0)


@pytest.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


def _doc(**overrides) -> Document:
    fields = {"tenant_id": "tenant-abc", "title": "t", "created_by": "user-1"}
    fields.update(overrides)
    return Document(**fields)


def test_default_created_at_is_timezone_aware_utc():
    doc = _doc()
    assert doc.created_at.tzinfo is not None
    assert doc.created_at.utcoffset() == UTC_OFFSET


def test_created_at_column_matches_baseline_migration():
    """Migration 001 creates documents.created_at as DateTime(timezone=True) NOT NULL."""
    column = Document.__table__.c.created_at
    assert column.type.timezone is True
    assert column.nullable is False
    assert column.server_default is not None


async def test_created_at_round_trips_as_aware_utc_on_sqlite(sqlite_engine):
    doc = _doc()
    written = doc.created_at
    doc_id = doc.id
    async with AsyncSession(sqlite_engine) as session:
        session.add(doc)
        await session.commit()

    async with AsyncSession(sqlite_engine) as session:
        loaded = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()

    assert loaded.created_at.utcoffset() == UTC_OFFSET
    assert loaded.created_at == written


async def test_non_utc_aware_value_is_stored_as_the_same_instant_in_utc(sqlite_engine):
    plus_five = timezone(timedelta(hours=5))
    written = datetime(2026, 1, 2, 3, 4, 5, tzinfo=plus_five)
    doc = _doc(created_at=written)
    doc_id = doc.id
    async with AsyncSession(sqlite_engine) as session:
        session.add(doc)
        await session.commit()

    async with AsyncSession(sqlite_engine) as session:
        loaded = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()

    assert loaded.created_at.utcoffset() == UTC_OFFSET
    assert loaded.created_at == written


async def test_naive_created_at_is_rejected(sqlite_engine):
    """A naive value is ambiguous (which zone?), so it must not be silently stored."""
    doc = _doc(created_at=datetime(2026, 1, 2, 3, 4, 5))
    async with AsyncSession(sqlite_engine) as session:
        session.add(doc)
        with pytest.raises(StatementError, match="timezone"):
            await session.commit()
