import enum
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class TenantIdSpace(str, enum.Enum):
    """Whose tenant a ``TenantResource.tenant_id`` names.

    A canonical tenant's id is its UUID. A Descope tenant's id is whatever
    Descope holds, and a Descope tenant can be created with any custom id,
    a canonical tenant's UUID among them: the id alone names no one tenant.
    """

    canonical = "canonical"
    descope = "descope"


class TenantResource(SQLModel, table=True):
    """A demo resource scoped to a specific tenant. Demonstrates data isolation."""

    __tablename__ = "tenant_resources"
    # The create route answers a duplicate name with 409 by catching IntegrityError,
    # but nothing in the schema ever raised one: without this constraint the second
    # insert simply succeeded and the 409 branch was unreachable. Scoped to the
    # tenant, its id space included, so two tenants may each hold a resource of the same name.
    __table_args__ = (
        sa.UniqueConstraint("tenant_space", "tenant_id", "name", name="uq_tenant_resources_tenant_space_id_name"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    tenant_space: TenantIdSpace = Field(
        sa_column=sa.Column(
            sa.Enum(TenantIdSpace, name="tenant_id_space", native_enum=False, length=16),
            nullable=False,
        ),
    )
    tenant_id: str = Field(index=True)
    name: str
    description: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
