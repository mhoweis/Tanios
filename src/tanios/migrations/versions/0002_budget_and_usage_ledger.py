"""Budget settings on users, and the usage ledger (milestone 2)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("monthly_cap_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column(
        "users",
        sa.Column("allow_free_for_private", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("users", sa.Column("cap_warned_month", sa.String(7), nullable=True))
    op.add_column("users", sa.Column("cap_reached_month", sa.String(7), nullable=True))

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_free", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("private", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("outcome", sa.String(16), nullable=False),
    )
    op.create_index("ix_usage_ledger_user_created", "usage_ledger", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_ledger_user_created", table_name="usage_ledger")
    op.drop_table("usage_ledger")
    op.drop_column("users", "cap_reached_month")
    op.drop_column("users", "cap_warned_month")
    op.drop_column("users", "allow_free_for_private")
    op.drop_column("users", "monthly_cap_usd")
