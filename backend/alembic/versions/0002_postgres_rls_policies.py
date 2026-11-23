"""enable postgresql row-level security policies

Revision ID: 0002_postgres_rls_policies
Revises: 0001_initial_saas_schema
Create Date: 2026-11-23
"""
from typing import Sequence, Union

from alembic import op
from app.db.models import TENANT_SCOPED_TABLES

revision: str = "0002_postgres_rls_policies"
down_revision: Union[str, None] = "0001_initial_saas_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AUTH_DIRECTORY_TABLES = [
    "tenants",
    "usuarios",
    "usuario_tenant_roles",
    "refresh_tokens",
]

STRICT_TENANT_TABLES = [
    t for t in TENANT_SCOPED_TABLES if t not in {"usuario_tenant_roles", "refresh_tokens"}
]


def _revoke_data_api_roles(table_name: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon', '{table_name}');
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM authenticated', '{table_name}');
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in STRICT_TENANT_TABLES:
        op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE public."{table_name}" FORCE ROW LEVEL SECURITY;')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON public."{table_name}";')
        _revoke_data_api_roles(table_name)
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'CREATE POLICY tenant_isolation ON public.%I FOR ALL TO %I USING (tenant_id = current_setting(''app.current_tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.current_tenant_id'', true))',
                    '{table_name}',
                    current_user
                );
            END $$;
            """
        )

    for table_name in AUTH_DIRECTORY_TABLES:
        op.execute(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE public."{table_name}" FORCE ROW LEVEL SECURITY;')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON public."{table_name}";')
        op.execute(f'DROP POLICY IF EXISTS backend_app_access ON public."{table_name}";')
        _revoke_data_api_roles(table_name)
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'CREATE POLICY backend_app_access ON public.%I FOR ALL TO %I USING (true) WITH CHECK (true)',
                    '{table_name}',
                    current_user
                );
            END $$;
            """
        )

    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.alembic_version') IS NOT NULL THEN
                ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    REVOKE ALL ON TABLE public.alembic_version FROM anon;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    REVOKE ALL ON TABLE public.alembic_version FROM authenticated;
                END IF;
                DROP POLICY IF EXISTS backend_app_access ON public.alembic_version;
                EXECUTE format(
                    'CREATE POLICY backend_app_access ON public.alembic_version FOR ALL TO %I USING (true) WITH CHECK (true)',
                    current_user
                );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.alembic_version') IS NOT NULL THEN
                DROP POLICY IF EXISTS backend_app_access ON public.alembic_version;
                ALTER TABLE public.alembic_version DISABLE ROW LEVEL SECURITY;
            END IF;
        END $$;
        """
    )

    for table_name in reversed(AUTH_DIRECTORY_TABLES):
        op.execute(f'DROP POLICY IF EXISTS backend_app_access ON public."{table_name}";')
        op.execute(f'ALTER TABLE public."{table_name}" NO FORCE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY;')

    for table_name in reversed(STRICT_TENANT_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON public."{table_name}";')
        op.execute(f'ALTER TABLE public."{table_name}" NO FORCE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE public."{table_name}" DISABLE ROW LEVEL SECURITY;')
