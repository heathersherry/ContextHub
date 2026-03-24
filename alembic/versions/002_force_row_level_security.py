"""Force RLS on tenant-scoped tables.

Revision ID: 002
Revises: 001
Create Date: 2026-03-24
"""

from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE contexts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teams FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_subscriptions FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE skill_subscriptions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teams NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE contexts NO FORCE ROW LEVEL SECURITY")
