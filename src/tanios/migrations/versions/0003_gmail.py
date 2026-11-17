"""Gmail: connections, sign-in links, sorted emails and pending drafts (milestone 5)

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _user_id(primary_key: bool = False) -> sa.Column:
    return sa.Column(
        "user_id",
        sa.Integer(),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


def upgrade() -> None:
    op.add_column("users", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "gmail_connections",
        _user_id(primary_key=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("sealed_token", sa.Text(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("digest_times", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_digest_key", sa.String(32), nullable=True),
    )

    op.create_table(
        "gmail_links",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        _user_id(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_id(),
        sa.Column("gmail_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("sender", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("list_number", sa.Integer(), nullable=True),
        sa.UniqueConstraint("user_id", "gmail_id", name="uq_emails_user_gmail_id"),
    )

    op.create_table(
        "email_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_id(),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("gmail_draft_id", sa.String(64), nullable=False),
        sa.Column("gmail_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("to_addresses", sa.Text(), nullable=False),
        sa.Column("cc_addresses", sa.Text(), nullable=False),
        sa.Column("thread_addresses", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "number", name="uq_email_drafts_user_number"),
    )


def downgrade() -> None:
    op.drop_table("email_drafts")
    op.drop_table("emails")
    op.drop_table("gmail_links")
    op.drop_table("gmail_connections")
    op.drop_column("users", "last_message_at")
