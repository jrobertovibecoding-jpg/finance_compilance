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


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in TENANT_SCOPED_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY;')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table_name}";')
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON "{table_name}"
            USING (tenant_id = current_setting('app.current_tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in reversed(TENANT_SCOPED_TABLES):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table_name}";')
        op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY;')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY;')
