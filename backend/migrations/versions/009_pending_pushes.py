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
            PERFORM pending_push_mark(
                'tenant', OLD.id::text,
                jsonb_build_object(
                    'name', OLD.name,
                    'external_org_id', OLD.external_org_id,
                    'external_org_provider_id', OLD.external_org_provider_id
                )
            );
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

# One trigger per row change; an UPDATE marks only when it changes a column a push
# carries (``json`` has no equality operator: its text is compared). An update that
# changes none of them (a link written by the adapter, ``updated_at``) is no change
# to push.
_CREATE_TRIGGERS = (
    "CREATE TRIGGER pending_push_write AFTER INSERT OR DELETE ON tenants "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_update AFTER UPDATE ON tenants FOR EACH ROW WHEN ("
    "OLD.name::text IS DISTINCT FROM NEW.name::text OR OLD.domains::text IS DISTINCT FROM NEW.domains::text "
    "OR OLD.status::text IS DISTINCT FROM NEW.status::text) EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_write AFTER INSERT OR DELETE ON users "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_update AFTER UPDATE ON users FOR EACH ROW WHEN ("
    "OLD.email::text IS DISTINCT FROM NEW.email::text OR OLD.given_name::text IS DISTINCT FROM NEW.given_name::text "
    "OR OLD.family_name::text IS DISTINCT FROM NEW.family_name::text "
    "OR OLD.status::text IS DISTINCT FROM NEW.status::text) EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_write AFTER INSERT OR DELETE ON permissions "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_update AFTER UPDATE ON permissions FOR EACH ROW WHEN ("
    "OLD.name::text IS DISTINCT FROM NEW.name::text "
    "OR OLD.description::text IS DISTINCT FROM NEW.description::text) EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_write AFTER INSERT OR DELETE ON roles "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_update AFTER UPDATE ON roles FOR EACH ROW WHEN ("
    "OLD.name::text IS DISTINCT FROM NEW.name::text OR OLD.description::text IS DISTINCT FROM NEW.description::text "
    "OR OLD.tenant_id::text IS DISTINCT FROM NEW.tenant_id::text) EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_write AFTER INSERT OR UPDATE OR DELETE ON role_permissions "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
    "CREATE TRIGGER pending_push_write AFTER INSERT OR UPDATE OR DELETE ON user_tenant_roles "
    "FOR EACH ROW EXECUTE FUNCTION pending_push_trigger()",
)

_DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS pending_push_write ON tenants",
    "DROP TRIGGER IF EXISTS pending_push_update ON tenants",
    "DROP TRIGGER IF EXISTS pending_push_write ON users",
    "DROP TRIGGER IF EXISTS pending_push_update ON users",
    "DROP TRIGGER IF EXISTS pending_push_write ON permissions",
    "DROP TRIGGER IF EXISTS pending_push_update ON permissions",
    "DROP TRIGGER IF EXISTS pending_push_write ON roles",
    "DROP TRIGGER IF EXISTS pending_push_update ON roles",
    "DROP TRIGGER IF EXISTS pending_push_write ON role_permissions",
    "DROP TRIGGER IF EXISTS pending_push_write ON user_tenant_roles",
)


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
    for statement in _CREATE_TRIGGERS:
        op.execute(statement)
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
    for statement in _DROP_TRIGGERS:
        op.execute(statement)
    op.execute("DROP FUNCTION pending_push_trigger()")
    op.execute("DROP FUNCTION pending_push_mark(text, text, jsonb)")
    op.drop_table("reconciliation_runs")
    op.drop_table("pending_pushes")
    op.execute("DROP SEQUENCE pending_push_seq")
