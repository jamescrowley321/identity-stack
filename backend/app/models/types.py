"""Column types shared by the ORM models."""

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine.interfaces import Dialect


class UTCDateTime(sa.types.TypeDecorator[datetime]):
    """A ``DateTime(timezone=True)`` column that always holds an aware UTC instant.

    Writes: naive values are rejected (the zone they were meant in is unknowable),
    aware values are normalised to UTC. Reads: Postgres ``timestamptz`` already
    comes back aware and is normalised to UTC; SQLite has no zone support and hands
    back the stored UTC wall-clock naive, so UTC is re-attached. The DDL is plain
    ``DateTime(timezone=True)``, so this matches existing migrations unchanged.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("naive datetime rejected: pass a timezone-aware value, e.g. datetime.now(timezone.utc)")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
