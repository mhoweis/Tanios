"""Users, strangers and processed messages (milestone 1)

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(32), nullable=False, unique=True),
        sa.Column("wa_user_id", sa.String(128), nullable=True, unique=True),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "strangers",
        sa.Column("sender_key", sa.String(128), primary_key=True),
        sa.Column(
            "notified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "processed_messages",
        sa.Column("wa_message_id", sa.String(255), primary_key=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_table("strangers")
    op.drop_table("users")
