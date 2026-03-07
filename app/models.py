"""
SQLAlchemy ORM models for the AI Calling Agent.

All sensitive fields (email, doctor info, medicines) are stored encrypted at rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    Enum as SAEnum,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ── Enums ───────────────────────────────────────────────────
import enum


class CallStatus(str, enum.Enum):
    INITIATED = "initiated"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_CONSENT = "no_consent"
    TIMEOUT = "timeout"


class ConsentStatus(str, enum.Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


# ── Call Session Model ──────────────────────────────────────
class CallSession(Base):
    """Tracks each phone call session."""

    __tablename__ = "call_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    twilio_call_sid: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    from_number: Mapped[str] = mapped_column(String(20), nullable=False)
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[CallStatus] = mapped_column(
        SAEnum(CallStatus), default=CallStatus.INITIATED, nullable=False
    )
    consent_status: Mapped[ConsentStatus] = mapped_column(
        SAEnum(ConsentStatus), default=ConsentStatus.PENDING, nullable=False
    )
    consent_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    call_duration_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    call_recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_recording_sid: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_call_sessions_status", "status"),
        Index("ix_call_sessions_created_at", "created_at"),
    )


# ── Customer Data Model ─────────────────────────────────────
class CustomerData(Base):
    """
    Stores structured data collected during the call.
    Sensitive fields should be encrypted before storage
    (handled at the application layer in the repository).
    """

    __tablename__ = "customer_data"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    call_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # ── Personal Info ───────────────────────────────────────
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_of_birth: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    phone_number: Mapped[str | None] = mapped_column(String(20), nullable=True)  # encrypted
    zipcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Household Info ──────────────────────────────────────
    tax_household_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Insurance Info ──────────────────────────────────────
    currently_insured: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    life_event: Mapped[str | None] = mapped_column(Text, nullable=True)
    life_event_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Preferences ─────────────────────────────────────────
    preferred_time_slot: Mapped[str | None] = mapped_column(Text, nullable=True)
    wants_aca_explanation: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    aca_explained: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Data quality ────────────────────────────────────────
    data_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_fields: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ── Call Transcript Model ───────────────────────────────────
class CallTranscript(Base):
    """Stores the conversation transcript for audit and review."""

    __tablename__ = "call_transcripts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    call_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'agent' or 'customer'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_call_transcripts_session_ts", "call_session_id", "timestamp"),
    )
