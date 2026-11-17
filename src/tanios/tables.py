"""Database tables. Every change here needs a migration in src/tanios/migrations."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    """Someone allowed to give Tanios instructions."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Digits only, with country code, as WhatsApp sends it.
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    # WhatsApp's business-scoped user ID. Learned from the first message that carries
    # both it and the phone number; later messages may carry only this.
    wa_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # US dollars per calendar month (UTC). Empty means no cap.
    monthly_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # R3: may free models see this user's email, notes and files?
    allow_free_for_private: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # The month ("2026-11") in which the 80% warning and the cap notice were last sent,
    # so each goes out once a month (R4).
    cap_warned_month: Mapped[str | None] = mapped_column(String(7))
    cap_reached_month: Mapped[str | None] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrangerRow(Base):
    """An unregistered sender who has already been told this is a private assistant (W3)."""

    __tablename__ = "strangers"

    sender_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProcessedMessageRow(Base):
    """WhatsApp message IDs already handled. Meta resends a webhook it thinks was missed."""

    __tablename__ = "processed_messages"

    wa_message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageRow(Base):
    """The usage ledger: one row for every model call, successful or not (R1)."""

    __tablename__ = "usage_ledger"
    __table_args__ = (Index("ix_usage_ledger_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(255))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    private: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # "ok", "failed_check" or "error"
    outcome: Mapped[str] = mapped_column(String(16))
