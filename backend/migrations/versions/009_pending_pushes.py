"""Add pending_pushes, the canonical changes not yet confirmed in the push provider, and reconciliation_runs.

A trigger on each canonical table the sync adapters push (tenants, users,
permissions, roles, role_permissions, user_tenant_roles) marks the entity it
changes in ``pending_pushes``, in the transaction that changes it. A
reconciliation pass pushes each marked entity's current state and removes the
mark once the push is delivered, unless a newer change marked it again (``seq``).
While an entity is marked, reconciliation does not overwrite it with the
provider's state. See ``docs/keycloak.md#reconciliation``.

Writes that bring the canonical model to the provider's state (the Keycloak
event pull, reconciliation itself) set ``identity_stack.sync_origin`` to
``provider`` for their transaction: they mark nothing new, and only renew the
``seq`` of a mark already there, so a pass that read the entity before them
does not remove that mark.

Every entity that exists when this migration runs is marked: nothing confirms
it was ever pushed.

``reconciliation_runs`` keeps the outcome of each provider's last pass.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_pending_pushes"
down_revision: Union[str, None] = "008_keycloak_pull_state"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_MARK_FUNCTION = """
CREATE FUNCTION pending_push_mark(p_kind text, p_key text, p_snapshot jsonb) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(current_setting('identity_stack.sync_origin', true), '') = 'provider' THEN
        UPDATE pending_pushes SET seq = nextval('pending_push_seq') WHERE kind = p_kind AND key = p_key;
        RETURN;
    END IF;
    INSERT INTO pending_pushes (kind, key, seq, snapshot, marked_at)
    VALUES (p_kind, p_key, nextval('pending_push_seq'), p_snapshot, clock_timestamp())
    ON CONFLICT (kind, key) DO UPDATE
        SET seq = nextval('pending_push_seq'),
            snapshot = coalesce(EXCLUDED.snapshot, pending_pushes.snapshot),
            marked_at = EXCLUDED.marked_at;
END
$$;
"""

# The ``nextval`` of an existing mark is taken in the UPDATE, after the row lock:
# of two transactions marking one entity, the one that commits later always holds
# the higher ``seq``.

_TRIGGER_FUNCTION = """
CREATE FUNCTION pending_push_trigger() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'tenants' THEN
        IF TG_OP = 'DELETE' THEN
            PERFORM pending_push_mark('tenant', OLD.id::text, jsonb_build_object('name', OLD.name));
        ELSE
            PERFORM pending_push_mark('tenant', NEW.id::text, NULL);
        END IF;
    ELSIF TG_TABLE_NAME = 'users' THEN
        IF TG_OP = 'DELETE' THEN
            PERFORM pending_push_mark('user', OLD.id::text, jsonb_build_object('email', OLD.email));
        ELSE
            PERFORM pending_push_mark('user', NEW.id::text, NULL);
        END IF;
    ELSIF TG_TABLE_NAME = 'permissions' THEN
        IF TG_OP = 'DELETE' THEN
            PERFORM pending_push_mark('permission', OLD.id::text, jsonb_build_object('name', OLD.name));
        ELSE
            PERFORM pending_push_mark('permission', NEW.id::text, NULL);
        END IF;
    ELSIF TG_TABLE_NAME = 'roles' THEN
        IF TG_OP = 'DELETE' THEN
            PERFORM pending_push_mark(
                'role', OLD.id::text, jsonb_build_object('name', OLD.name, 'tenant_id', OLD.tenant_id)
            );
        ELSE
            PERFORM pending_push_mark('role', NEW.id::text, NULL);
        END IF;
    ELSIF TG_TABLE_NAME = 'role_permissions' THEN
        IF TG_OP IN ('DELETE', 'UPDATE') THEN
            PERFORM pending_push_mark('role', OLD.role_id::text, NULL);
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            PERFORM pending_push_mark('role', NEW.role_id::text, NULL);
        END IF;
    ELSIF TG_TABLE_NAME = 'user_tenant_roles' THEN
        IF TG_OP IN ('DELETE', 'UPDATE') THEN
            PERFORM pending_push_mark(
                'membership', OLD.user_id::text || ':' || OLD.tenant_id::text,
                jsonb_build_object('role_id', OLD.role_id)
            );
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            PERFORM pending_push_mark('membership', NEW.user_id::text || ':' || NEW.tenant_id::text, NULL);
        END IF;
    END IF;
    RETURN NULL;
END
$$;
"""

# table -> the columns a push carries: an update that changes none of them (a link
# written by the adapter, ``updated_at``) is not a change to push.
_PUSHED_COLUMNS: dict[str, tuple[str, ...]] = {
    "tenants": ("name", "domains", "status"),
    "users": ("email", "given_name", "family_name", "status"),
    "permissions": ("name", "description"),
    "roles": ("name", "description", "tenant_id"),
}
_ROW_TABLES = ("role_permissions", "user_tenant_roles")


def _distinct(column: str) -> str:
    # ``json`` has no equality operator: compare its text.
    return f"OLD.{column}::text IS DISTINCT FROM NEW.{column}::text"


def upgrade() -> None:
    op.execute("CREATE SEQUENCE pending_push_seq")
    op.create_table(
        "pending_pushes",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "reconciliation_runs",
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.execute(_MARK_FUNCTION)
    op.execute(_TRIGGER_FUNCTION)
    for table, columns in _PUSHED_COLUMNS.items():
        changed = " OR ".join(_distinct(column) for column in columns)
        op.execute(
            f"CREATE TRIGGER pending_push_write AFTER INSERT OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()"
        )
        op.execute(
            f"CREATE TRIGGER pending_push_update AFTER UPDATE ON {table} "
            f"FOR EACH ROW WHEN ({changed}) EXECUTE FUNCTION pending_push_trigger()"
        )
    for table in _ROW_TABLES:
        op.execute(
            f"CREATE TRIGGER pending_push_write AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()"
        )
    op.execute(
        """
        INSERT INTO pending_pushes (kind, key, seq)
        SELECT kind, key, nextval('pending_push_seq') FROM (
            SELECT 'tenant' AS kind, id::text AS key FROM tenants
            UNION SELECT 'user', id::text FROM users
            UNION SELECT 'permission', id::text FROM permissions
            UNION SELECT 'role', id::text FROM roles
            UNION SELECT DISTINCT 'membership', user_id::text || ':' || tenant_id::text FROM user_tenant_roles
        ) AS existing
        """
    )


def downgrade() -> None:
    for table in (*_PUSHED_COLUMNS, *_ROW_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS pending_push_write ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS pending_push_update ON {table}")
    op.execute("DROP FUNCTION pending_push_trigger()")
    op.execute("DROP FUNCTION pending_push_mark(text, text, jsonb)")
    op.drop_table("reconciliation_runs")
    op.drop_table("pending_pushes")
    op.execute("DROP SEQUENCE pending_push_seq")
