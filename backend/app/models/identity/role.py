import uuid

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Role(SQLModel, table=True):
    """Role definition — global (tenant_id=NULL) or tenant-scoped."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", "tenant_id", name="uq_roles_name_tenant"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    description: str | None = None
    tenant_id: uuid.UUID | None = Field(default=None, foreign_key="tenants.id")


class Permission(SQLModel, table=True):
    """Permission definition (e.g. 'documents.write')."""

    __tablename__ = "permissions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    description: str | None = None


class RolePermission(SQLModel, table=True):
    """Many-to-many role to permission mapping."""

    __tablename__ = "role_permissions"

    role_id: uuid.UUID = Field(foreign_key="roles.id", primary_key=True)
    permission_id: uuid.UUID = Field(foreign_key="permissions.id", primary_key=True)
