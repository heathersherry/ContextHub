"""Add enforcement audit action for Phase 4.

Revision ID: 005
Revises: 004
Create Date: 2026-06-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check")
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check
            CHECK (action IN (
                'read','write','create','update','delete','search',
                'ls','stat','promote','publish','access_denied','policy_change',
                'feedback','lifecycle_transition','enforcement'
            ))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check")
    op.execute(
        """
        ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check
            CHECK (action IN (
                'read','write','create','update','delete','search',
                'ls','stat','promote','publish','access_denied','policy_change',
                'feedback','lifecycle_transition'
            ))
        """
    )
