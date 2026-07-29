"""rename card_last4 to card_ending

Autogenerate rendered this as add_column + drop_column, which would have
silently discarded every stored card value. It is a rename, so it is written as
one — alter_column preserves the data.

The column is also widened from 4 to 8. American Express prints a five-digit
account ending, and truncating it to four means the export no longer matches the
statement it is checked against.

Revision ID: 89bee9b8a115
Revises: 5997c47d8d5f
Create Date: 2026-07-28 22:32:26.783634
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '89bee9b8a115'
down_revision: str | None = '5997c47d8d5f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.alter_column(
            "card_last4",
            new_column_name="card_ending",
            existing_type=sa.VARCHAR(length=4),
            type_=sa.String(length=8),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Narrowing back to 4 would truncate five-digit Amex endings, so only the
    # name reverts; the stored values are left intact.
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.alter_column(
            "card_ending",
            new_column_name="card_last4",
            existing_type=sa.String(length=8),
            type_=sa.VARCHAR(length=4),
            existing_nullable=True,
        )
