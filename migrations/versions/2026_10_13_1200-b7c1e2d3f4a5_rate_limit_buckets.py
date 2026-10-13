"""rate limit buckets

Revision ID: b7c1e2d3f4a5
Revises: af03443f46a3
Create Date: 2026-10-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c1e2d3f4a5"
down_revision: str | Sequence[str] | None = "af03443f46a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_buckets")),
        prefixes=["UNLOGGED"],
    )
    # Retention deletes scans by age.
    op.create_index(op.f("ix_scans_created_at"), "scans", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_scans_created_at"), table_name="scans")
    op.drop_table("rate_limit_buckets")
