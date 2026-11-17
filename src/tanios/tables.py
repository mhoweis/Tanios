"""Database tables. Every change here needs a migration in src/tanios/migrations."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
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
