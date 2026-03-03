"""
Call Manager — the central orchestrator.

Bridges Twilio Media Stream ↔ OpenAI Realtime API ↔ Database.
Handles function calls, consent logic, data saving, and call lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.websockets import WebSocket
from twilio.rest import Client as TwilioClient
import structlog

from app.config import get_settings
from app.database import async_session_factory
from app.encryption import get_encryptor
from app.models import CallStatus, ConsentStatus
from app.openai_realtime import OpenAIRealtimeClient
from app.repository import CallSessionRepository, CustomerDataRepository, TranscriptRepository
from app.twilio_handler import TwilioMediaStreamHandler

logger = structlog.get_logger(__name__)


class CallManager:
    """
    Manages a single call lifecycle:
    1. Twilio WS connects → accept & start streaming
    2. Connect to OpenAI Realtime → configure session
    3. Bridge audio: Twilio → OpenAI → Twilio
    4. Handle function calls (consent, save data, end call)
    5. Clean up on call end
    """

    # Track all active calls
    _active_calls: dict[str, "CallManager"] = {}

    def __init__(self, websocket: WebSocket):
        self.settings = get_settings()
        self.websocket = websocket
        self.call_id: str | None = None
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.db_call_id: uuid.UUID | None = None

        # Components
        self.twilio_handler: TwilioMediaStreamHandler | None = None
        self.openai_client: OpenAIRealtimeClient | None = None

        # State
        self._consent_received = False
        self._call_ended = False
        self._greeting_sent = False

    async def start(self) -> None:
        """Entry point — handle the complete call lifecycle."""
        self.call_id = str(uuid.uuid4())[:8]
        logger.info("call_manager_starting", call_id=self.call_id)

        # Initialize Twilio handler
        self.twilio_handler = TwilioMediaStreamHandler(
            websocket=self.websocket,
            on_audio_received=self._on_twilio_audio,
            on_call_start=self._on_call_started,
            on_call_end=self._on_call_ended,
        )

        try:
            # This blocks until the Twilio WS closes
            await self.twilio_handler.handle()
        except Exception as e:
            logger.error("call_manager_error", call_id=self.call_id, error=str(e))
        finally:
            await self._cleanup()

    async def _on_call_started(self, call_sid: str, stream_sid: str) -> None:
        """Called when Twilio sends the 'start' event."""
        self.call_sid = call_sid
        self.stream_sid = stream_sid

        logger.info(
            "call_started",
            call_id=self.call_id,
            call_sid=call_sid,
            stream_sid=stream_sid,
        )

        # Register in active calls
        CallManager._active_calls[call_sid] = self

        # Create database record (non-blocking — don't let DB failure kill the call)
        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CallSessionRepository(session, encryptor)
                call_record = await repo.create(
                    twilio_call_sid=call_sid,
                    from_number="inbound",
                    to_number=self.settings.twilio_phone_number,
                )
                self.db_call_id = call_record.id
                await repo.update_status(call_record.id, CallStatus.IN_PROGRESS)
                await session.commit()
        except Exception as e:
            logger.error("db_create_session_error", call_id=self.call_id, error=str(e))
            # Continue without DB — the call should still work

        # Connect to OpenAI Realtime with retry
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                self.openai_client = OpenAIRealtimeClient(
                    call_id=self.call_id,
                    on_audio_delta=self._on_openai_audio,
                    on_transcript=self._on_transcript,
                    on_function_call=self._on_function_call,
                    on_error=self._on_openai_error,
                    on_session_end=self._on_openai_session_end,
                    on_speech_started=self._on_customer_speech_started,
                    on_response_interrupted=self._on_response_interrupted,
                )
                await self.openai_client.connect()
                logger.info("openai_connected_successfully", call_id=self.call_id, attempt=attempt)
                break
            except Exception as e:
                logger.error(
                    "openai_connect_failed",
                    call_id=self.call_id,
                    attempt=attempt,
                    error=str(e),
                )
                if attempt == max_retries:
                    logger.error("openai_connect_all_retries_failed", call_id=self.call_id)
                    return  # Give up — Twilio WS stays open but no AI
                await asyncio.sleep(0.5)

        # Trigger greeting when session is ready (trigger_response waits internally)
        if not self._greeting_sent and self.openai_client and self.openai_client.is_connected:
            self._greeting_sent = True
            await self.openai_client.trigger_response()

    async def _on_twilio_audio(self, audio_b64: str) -> None:
        """Audio from Twilio (caller) → forward to OpenAI."""
        if self.openai_client and self.openai_client.is_connected:
            await self.openai_client.send_audio(audio_b64.encode("utf-8"))

    async def _on_customer_speech_started(self) -> None:
        """Customer started speaking — log only, let OpenAI VAD handle interruption natively.
        
        NOTE: We do NOT call clear_audio() here because OpenAI's server-side VAD
        already handles interruptions. The Twilio buffer is cleared later when we
        confirm the response was actually cancelled (see _on_response_interrupted).
        """
        logger.info("customer_speech_detected", call_id=self.call_id)

    async def _on_response_interrupted(self) -> None:
        """OpenAI confirmed a response was cancelled due to customer interruption.
        Clear Twilio's audio buffer so stale AI audio stops playing immediately."""
        logger.info("clearing_twilio_buffer_on_interruption", call_id=self.call_id)
        if self.twilio_handler and self.twilio_handler.is_connected:
            await self.twilio_handler.clear_audio()

    async def _on_openai_audio(self, audio_b64: str) -> None:
        """Audio from OpenAI (AI agent) → forward to Twilio as base64 directly."""
        if self.twilio_handler and self.twilio_handler.is_connected:
            await self.twilio_handler.send_audio_b64(audio_b64)

    async def _on_transcript(self, role: str, content: str) -> None:
        """Store transcript entries from both sides."""
        if not self.db_call_id or not content.strip():
            return

        logger.info(
            "transcript",
            call_id=self.call_id,
            role=role,
            content=content[:100],
        )

        try:
            async with async_session_factory() as session:
                repo = TranscriptRepository(session)
                await repo.add_entry(self.db_call_id, role, content)
                await session.commit()
        except Exception as e:
            logger.error("transcript_save_error", error=str(e))

    async def _on_function_call(self, fn_name: str, fn_args: dict) -> str:
        """Handle function calls from OpenAI."""
        logger.info(
            "function_call_received",
            call_id=self.call_id,
            function=fn_name,
            args_keys=list(fn_args.keys()),
        )

        match fn_name:
            case "record_consent":
                return await self._handle_consent(fn_args)
            case "save_customer_data":
                return await self._handle_save_data(fn_args)
            case "end_call":
                return await self._handle_end_call(fn_args)
            case _:
                logger.warning("unknown_function", function=fn_name)
                return json.dumps({"error": f"Unknown function: {fn_name}"})

    async def _handle_consent(self, args: dict) -> str:
        """Process consent decision."""
        consent_given = args.get("consent_given", False)

        if not self.db_call_id:
            return json.dumps({"error": "No active call session"})

        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CallSessionRepository(session, encryptor)

                if consent_given:
                    self._consent_received = True
                    await repo.update_consent(self.db_call_id, ConsentStatus.GRANTED)
                    logger.info("consent_granted", call_id=self.call_id)
                    return json.dumps({
                        "status": "success",
                        "consent": "granted",
                        "message": "Consent recorded. You may proceed with the conversation.",
                    })
                else:
                    await repo.update_consent(self.db_call_id, ConsentStatus.DENIED)
                    await repo.update_status(self.db_call_id, CallStatus.NO_CONSENT)
                    logger.info("consent_denied", call_id=self.call_id)
                    return json.dumps({
                        "status": "success",
                        "consent": "denied",
                        "message": "Consent denied. Please end the call politely.",
                    })

                await session.commit()    
        except Exception as e:
            logger.error("consent_error", error=str(e))
            return json.dumps({"error": str(e)})

    async def _handle_save_data(self, args: dict) -> str:
        """Save collected customer data to the database."""
        if not self.db_call_id:
            return json.dumps({"error": "No active call session"})

        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CustomerDataRepository(session, encryptor)

                await repo.create_or_update(self.db_call_id, args)

                # Check for missing required fields
                required_fields = ["full_name", "email", "age", "zipcode", "state"]
                missing = [f for f in required_fields if not args.get(f)]
                await repo.mark_complete(self.db_call_id, missing if missing else None)

                await session.commit()

            logger.info(
                "customer_data_saved",
                call_id=self.call_id,
                fields_count=len([v for v in args.values() if v is not None]),
                missing=missing if missing else None,
            )

            return json.dumps({
                "status": "success",
                "message": "Customer data saved successfully.",
                "missing_fields": missing if missing else [],
            })

        except Exception as e:
            logger.error("save_data_error", error=str(e))
            return json.dumps({"error": f"Failed to save data: {str(e)}"})

    async def _handle_end_call(self, args: dict) -> str:
        """End the call via Twilio API."""
        reason = args.get("reason", "completed")
        logger.info("end_call_requested", call_id=self.call_id, reason=reason)

        if self._call_ended:
            return json.dumps({"status": "already_ended"})

        self._call_ended = True

        # Update call status in DB
        if self.db_call_id:
            try:
                status_map = {
                    "completed": CallStatus.COMPLETED,
                    "no_consent": CallStatus.NO_CONSENT,
                    "customer_request": CallStatus.COMPLETED,
                    "error": CallStatus.FAILED,
                    "timeout": CallStatus.TIMEOUT,
                }
                status = status_map.get(reason, CallStatus.COMPLETED)

                async with async_session_factory() as session:
                    encryptor = get_encryptor()
                    repo = CallSessionRepository(session, encryptor)
                    await repo.update_status(self.db_call_id, status)
                    await session.commit()
            except Exception as e:
                logger.error("end_call_db_error", error=str(e))

        # Hang up via Twilio REST API (after a short delay to let goodbye play)
        asyncio.create_task(self._hangup_after_delay(3.0))

        return json.dumps({
            "status": "success",
            "message": "Call will be ended shortly.",
        })

    async def _hangup_after_delay(self, delay: float) -> None:
        """Hang up the Twilio call after a delay."""
        await asyncio.sleep(delay)

        if not self.call_sid:
            return

        try:
            client = TwilioClient(
                self.settings.twilio_account_sid,
                self.settings.twilio_auth_token,
            )
            client.calls(self.call_sid).update(status="completed")
            logger.info("twilio_call_ended", call_id=self.call_id, call_sid=self.call_sid)
        except Exception as e:
            logger.error("twilio_hangup_error", error=str(e))

    async def _on_openai_error(self, error_msg: str) -> None:
        """Handle OpenAI errors."""
        logger.error("openai_error_in_call", call_id=self.call_id, error=error_msg)

    async def _on_openai_session_end(self) -> None:
        """Called when OpenAI WS closes."""
        logger.info("openai_session_ended", call_id=self.call_id)

    async def _on_call_ended(self) -> None:
        """Called when Twilio WS closes."""
        logger.info("call_ended", call_id=self.call_id)
        # Don't call _cleanup here — it's called in the finally block of start()

    async def _cleanup(self) -> None:
        """Clean up all resources. Called once from start()'s finally block."""
        if self._call_ended:
            return  # Already cleaned up
        self._call_ended = True

        # Remove from active calls
        if self.call_sid and self.call_sid in CallManager._active_calls:
            del CallManager._active_calls[self.call_sid]

        # Disconnect OpenAI
        if self.openai_client:
            await self.openai_client.disconnect()

        # Final DB update if not already ended
        if self.db_call_id and not self._call_ended:
            try:
                async with async_session_factory() as session:
                    encryptor = get_encryptor()
                    repo = CallSessionRepository(session, encryptor)
                    await repo.update_status(self.db_call_id, CallStatus.COMPLETED)
                    await session.commit()
            except Exception as e:
                logger.error("cleanup_db_error", error=str(e))

        logger.info("call_cleaned_up", call_id=self.call_id)

    @classmethod
    def get_active_call(cls, call_sid: str) -> "CallManager | None":
        return cls._active_calls.get(call_sid)

    @classmethod
    def get_active_calls_count(cls) -> int:
        return len(cls._active_calls)
