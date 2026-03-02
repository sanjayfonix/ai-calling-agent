"""
Database repository — async CRUD operations for call sessions and customer data.
Handles encryption of sensitive fields before storage.
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallSession, CallStatus, ConsentStatus, CustomerData, CallTranscript
from app.encryption import FieldEncryptor

import structlog

logger = structlog.get_logger(__name__)


class CallSessionRepository:
    """CRUD operations for CallSession."""

    def __init__(self, session: AsyncSession, encryptor: FieldEncryptor):
        self.session = session
        self.encryptor = encryptor

    async def create(
        self,
        twilio_call_sid: str,
        from_number: str,
        to_number: str,
    ) -> CallSession:
        call = CallSession(
            twilio_call_sid=twilio_call_sid,
            from_number=from_number,
            to_number=to_number,
            status=CallStatus.INITIATED,
            consent_status=ConsentStatus.PENDING,
        )
        self.session.add(call)
        await self.session.flush()
        logger.info("call_session_created", call_id=str(call.id), sid=twilio_call_sid)
        return call

    async def get_by_sid(self, twilio_call_sid: str) -> CallSession | None:
        result = await self.session.execute(
            select(CallSession).where(
                CallSession.twilio_call_sid == twilio_call_sid
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, call_id: uuid.UUID) -> CallSession | None:
        result = await self.session.execute(
            select(CallSession).where(CallSession.id == call_id)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        call_id: uuid.UUID,
        status: CallStatus,
        error_message: str | None=None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if error_message:
            values["error_message"] = error_message
        if status in (
            CallStatus.COMPLETED,
            CallStatus.FAILED,
            CallStatus.NO_CONSENT,
            CallStatus.TIMEOUT,
        ):
            values["ended_at"] = datetime.now(timezone.utc)

        await self.session.execute(
            update(CallSession).where(CallSession.id == call_id).values(**values)
        )
        await self.session.flush()
        logger.info("call_status_updated", call_id=str(call_id), status=status.value)

    async def update_consent(
        self,
        call_id: uuid.UUID,
        consent: ConsentStatus,
    ) -> None:
        await self.session.execute(
            update(CallSession)
            .where(CallSession.id == call_id)
            .values(
                consent_status=consent,
                consent_timestamp=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self.session.flush()
        logger.info("consent_updated", call_id=str(call_id), consent=consent.value)

    async def update_recording(
        self,
        twilio_call_sid: str,
        recording_url: str,
        recording_sid: str,
    ) -> None:
        await self.session.execute(
            update(CallSession)
            .where(CallSession.twilio_call_sid == twilio_call_sid)
            .values(
                call_recording_url=recording_url,
                call_recording_sid=recording_sid,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self.session.flush()
        logger.info("recording_saved", sid=twilio_call_sid)

    async def update_duration(
        self,
        twilio_call_sid: str,
        duration_seconds: int,
    ) -> None:
        await self.session.execute(
            update(CallSession)
            .where(CallSession.twilio_call_sid == twilio_call_sid)
            .values(
                call_duration_seconds=duration_seconds,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self.session.flush()


class CustomerDataRepository:
    """CRUD operations for CustomerData with encryption."""

    # Fields that must be encrypted at rest
    ENCRYPTED_FIELDS = {"email", "doctor_name", "medicines"}

    def __init__(self, session: AsyncSession, encryptor: FieldEncryptor):
        self.session = session
        self.encryptor = encryptor

    def _encrypt_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        """Encrypt sensitive fields before storage."""
        encrypted = dict(data)
        for field in self.ENCRYPTED_FIELDS:
            if field in encrypted and encrypted[field] is not None:
                encrypted[field] = self.encryptor.encrypt(str(encrypted[field]))
        return encrypted

    def _decrypt_fields(self, record: CustomerData) -> dict[str, Any]:
        """Decrypt sensitive fields when reading."""
        data: dict[str, Any] = {}
        for col in CustomerData.__table__.columns:
            val = getattr(record, col.name)
            if col.name in self.ENCRYPTED_FIELDS and val is not None:
                try:
                    data[col.name] = self.encryptor.decrypt(val)
                except Exception:
                    data[col.name] = val  # fallback if not encrypted
            else:
                data[col.name] = val
        return data

    async def create_or_update(
        self,
        call_session_id: uuid.UUID,
        collected_data: dict[str, Any],
    ) -> CustomerData:
        """Create or update customer data for a call session."""
        result = await self.session.execute(
            select(CustomerData).where(
                CustomerData.call_session_id == call_session_id
            )
        )
        existing = result.scalar_one_or_none()

        encrypted_data = self._encrypt_fields(collected_data)

        if existing:
            # Update only non-None fields
            for key, value in encrypted_data.items():
                if value is not None and hasattr(existing, key):
                    setattr(existing, key, value)
            existing.updated_at = datetime.now(timezone.utc)
            await self.session.flush()
            logger.info("customer_data_updated", call_session_id=str(call_session_id))
            return existing
        else:
            customer = CustomerData(
                call_session_id=call_session_id,
                **{k: v for k, v in encrypted_data.items() if hasattr(CustomerData, k)},
            )
            self.session.add(customer)
            await self.session.flush()
            logger.info("customer_data_created", call_session_id=str(call_session_id))
            return customer

    async def get_by_call_session(
        self, call_session_id: uuid.UUID
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(CustomerData).where(
                CustomerData.call_session_id == call_session_id
            )
        )
        record = result.scalar_one_or_none()
        if record:
            return self._decrypt_fields(record)
        return None

    async def mark_complete(
        self,
        call_session_id: uuid.UUID,
        missing_fields: list[str] | None=None,
    ) -> None:
        values: dict[str, Any] = {
            "data_complete": not bool(missing_fields),
            "updated_at": datetime.now(timezone.utc),
        }
        if missing_fields:
            values["missing_fields"] = json.dumps(missing_fields)
        await self.session.execute(
            update(CustomerData)
            .where(CustomerData.call_session_id == call_session_id)
            .values(**values)
        )
        await self.session.flush()


class TranscriptRepository:
    """Stores conversation transcript entries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_entry(
        self,
        call_session_id: uuid.UUID,
        role: str,
        content: str,
    ) -> None:
        entry = CallTranscript(
            call_session_id=call_session_id,
            role=role,
            content=content,
        )
        self.session.add(entry)
        await self.session.flush()

    async def get_transcript(
        self, call_session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        result = await self.session.execute(
            select(CallTranscript)
            .where(CallTranscript.call_session_id == call_session_id)
            .order_by(CallTranscript.timestamp)
        )
        entries = result.scalars().all()
        return [
            {
                "role": e.role,
                "content": e.content,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in entries
        ]
