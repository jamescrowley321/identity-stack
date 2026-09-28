"""Document.created_at round-trips as an aware UTC instant on Postgres (migrated schema)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.document import Document

UTC_OFFSET = timedelta(0)


async def test_created_at_round_trips_as_aware_utc_on_postgres(db_session):
    written = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone(timedelta(hours=-7)))
    doc = Document(tenant_id="tenant-ts", title="t", created_by="user-1", created_at=written)
    db_session.add(doc)
    await db_session.flush()
    db_session.expunge_all()

    loaded = (await db_session.execute(select(Document).where(Document.id == doc.id))).scalar_one()

    assert loaded.created_at.utcoffset() == UTC_OFFSET
    assert loaded.created_at == written


async def test_default_created_at_round_trips_as_aware_utc_on_postgres(db_session):
    doc = Document(tenant_id="tenant-ts", title="t", created_by="user-1")
    written = doc.created_at
    db_session.add(doc)
    await db_session.flush()
    db_session.expunge_all()

    loaded = (await db_session.execute(select(Document).where(Document.id == doc.id))).scalar_one()

    assert loaded.created_at.utcoffset() == UTC_OFFSET
    assert loaded.created_at == written
