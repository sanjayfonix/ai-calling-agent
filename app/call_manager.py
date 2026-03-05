"""
Call Manager -- central orchestrator for a single phone call.

Bridges: Twilio Media Stream <-> OpenAI Realtime API <-> Database.

Key design decisions for clean audio:
1. On speech_started: Clear Twilio audio buffer immediately. This stops AI audio
   playback on the customer's phone, breaking the echo loop (AI -> speaker -> mic -> OpenAI).
2. Do NOT manually cancel OpenAI responses. Let server VAD handle it natively.
3. DB errors never kill the call. Everything is wrapped in try/except.
4. Cleanup runs exactly once via _cleaned_up flag.
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
from app.call_context import CallContext, get_call_context, remove_call_context
from app.dynamic_prompts import generate_dynamic_system_prompt

logger = structlog.get_logger(__name__)


class CallManager:
    """Manages one phone call end-to-end."""

    _active_calls: dict[str, "CallManager"] = {}
    # Audio debug counters (class-level, reset per call)
    _audio_stats: dict[str, int] = {"openai_received": 0, "twilio_sent": 0, "skipped": 0}

    def __init__(self, websocket: WebSocket):
        self.settings = get_settings()
        self.websocket = websocket
        self.call_id: str | None = None
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.db_call_id: uuid.UUID | None = None
        self.call_context: CallContext | None = None

        self.twilio_handler: TwilioMediaStreamHandler | None = None
        self.openai_client: OpenAIRealtimeClient | None = None

        self._consent_received = False
        self._call_ended = False
        self._cleaned_up = False
        self._greeting_sent = False
        # Reset audio stats for this call
        CallManager._audio_stats = {"openai_received": 0, "twilio_sent": 0, "skipped": 0}

    async def start(self) -> None:
        """Entry point: handle the complete call lifecycle."""
        self.call_id = str(uuid.uuid4())[:8]
        logger.info("call_starting", call_id=self.call_id)

        self.twilio_handler = TwilioMediaStreamHandler(
            websocket=self.websocket,
            on_audio_received=self._on_twilio_audio,
            on_call_start=self._on_call_started,
            on_call_end=self._on_call_ended,
        )

        try:
            await self.twilio_handler.handle()
        except Exception as e:
            logger.error("call_error", call_id=self.call_id, error=str(e))
        finally:
            await self._cleanup()

    # ── Call Lifecycle ────────────────────────────────────────

    async def _on_call_started(self, call_sid: str, stream_sid: str) -> None:
        """Twilio stream connected. Set up OpenAI and start conversation."""
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        CallManager._active_calls[call_sid] = self

        logger.info("call_connected", call_id=self.call_id, call_sid=call_sid)

        # Retrieve call context by call_sid (stored when outbound call was initiated)
        self.call_context = get_call_context(call_sid)
        if self.call_context:
            logger.info("call_context_loaded", call_id=self.call_id, agent=self.call_context.agent_name)
        else:
            logger.info("no_call_context", call_id=self.call_id, using_default_prompt=True)

        # Generate dynamic system prompt based on context
        system_prompt = generate_dynamic_system_prompt(self.call_context)

        # Create DB record (non-fatal if fails)
        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CallSessionRepository(session, encryptor)
                record = await repo.create(
                    twilio_call_sid=call_sid,
                    from_number="outbound",
                    to_number=self.settings.twilio_phone_number,
                )
                self.db_call_id = record.id
                await repo.update_status(record.id, CallStatus.IN_PROGRESS)
                await session.commit()
        except Exception as e:
            logger.error("db_create_error", call_id=self.call_id, error=str(e))

        # Connect to OpenAI (retry once on failure)
        for attempt in range(2):
            try:
                self.openai_client = OpenAIRealtimeClient(
                    call_id=self.call_id,
                    on_audio_delta=self._on_openai_audio,
                    on_transcript=self._on_transcript,
                    on_function_call=self._on_function_call,
                    on_error=self._on_openai_error,
                    on_session_end=self._on_openai_session_end,
                    on_speech_started=self._on_customer_speech_started,
                    system_prompt=system_prompt,
                )
                await self.openai_client.connect()
                break
            except Exception as e:
                logger.error("openai_connect_failed", call_id=self.call_id, attempt=attempt, error=str(e))
                if attempt == 1:
                    return
                await asyncio.sleep(0.5)

        # Send initial greeting
        if self.openai_client and self.openai_client.is_connected and not self._greeting_sent:
            self._greeting_sent = True
            await self.openai_client.trigger_response()

    async def _on_call_ended(self) -> None:
        """Twilio WebSocket closed."""
        logger.info("call_ended", call_id=self.call_id)

    # ── Audio Pipeline ────────────────────────────────────────

    async def _on_twilio_audio(self, audio_b64: str) -> None:
        """Customer audio from Twilio -> forward to OpenAI."""
        if self.openai_client and self.openai_client.is_connected:
            await self.openai_client.send_audio(audio_b64)

    async def _on_openai_audio(self, audio_b64: str) -> None:
        """AI audio from OpenAI -> forward to Twilio."""
        CallManager._audio_stats["openai_received"] += 1
        logger.info("audio_forwarding", call_id=self.call_id, audio_len=len(audio_b64) if audio_b64 else 0, twilio_connected=self.twilio_handler.is_connected if self.twilio_handler else False, stream_sid=self.stream_sid)
        if self.twilio_handler and self.twilio_handler.is_connected:
            await self.twilio_handler.send_audio_b64(audio_b64)
            CallManager._audio_stats["twilio_sent"] += 1
        else:
            CallManager._audio_stats["skipped"] += 1
            logger.warning("audio_forward_skipped", call_id=self.call_id, has_handler=bool(self.twilio_handler), is_connected=self.twilio_handler.is_connected if self.twilio_handler else None)

    async def _on_customer_speech_started(self) -> None:
        """Customer started speaking (detected by OpenAI's server VAD).

        CRITICAL: Clear Twilio's audio buffer immediately.
        This stops AI audio from playing on the customer's phone, which:
        1. Lets the customer speak without hearing the AI talk over them
        2. Breaks the echo loop (AI audio -> phone speaker -> mic -> back to OpenAI)
        3. Ensures OpenAI only hears the customer's actual voice, not echoed AI audio

        We do NOT cancel the OpenAI response — the server VAD handles that automatically.
        """
        logger.info("customer_speaking_clear_buffer", call_id=self.call_id)
        if self.twilio_handler and self.twilio_handler.is_connected:
            await self.twilio_handler.clear_audio()

    # ── Transcripts ───────────────────────────────────────────

    async def _on_transcript(self, role: str, content: str) -> None:
        """Store transcript in DB."""
        if not content.strip() or not self.db_call_id:
            return
        logger.info("transcript", call_id=self.call_id, role=role, text=content[:80])
        try:
            async with async_session_factory() as session:
                repo = TranscriptRepository(session)
                await repo.add_entry(self.db_call_id, role, content)
                await session.commit()
        except Exception as e:
            logger.error("transcript_save_error", error=str(e))

    # ── Function Calls from OpenAI ───────────────────────────

    async def _on_function_call(self, fn_name: str, fn_args: dict) -> str:
        """Handle tool calls from the AI agent."""
        logger.info("function_call", call_id=self.call_id, fn=fn_name, args=fn_args)
        try:
            if fn_name == "record_consent":
                return await self._handle_consent(fn_args)
            elif fn_name == "save_customer_data":
                return await self._handle_save_data(fn_args)
            elif fn_name == "end_call":
                return await self._handle_end_call(fn_args)
            elif fn_name == "check_slot_availability":
                return await self._handle_check_slot(fn_args)
            else:
                return json.dumps({"error": f"Unknown function: {fn_name}"})
        except Exception as e:
            logger.error("function_call_error", fn=fn_name, error=str(e))
            return json.dumps({"error": str(e)})

    async def _handle_consent(self, args: dict) -> str:
        consent = args.get("consent_given", False)

        if consent:
            self._consent_received = True

        if self.db_call_id:
            try:
                async with async_session_factory() as session:
                    encryptor = get_encryptor()
                    repo = CallSessionRepository(session, encryptor)
                    status = ConsentStatus.GRANTED if consent else ConsentStatus.DENIED
                    await repo.update_consent(self.db_call_id, status)
                    if not consent:
                        await repo.update_status(self.db_call_id, CallStatus.NO_CONSENT)
                    await session.commit()
            except Exception as e:
                logger.error("consent_db_error", error=str(e))

        if consent:
            return json.dumps({"status": "success", "consent": "granted",
                               "message": "Consent recorded. Proceed with questions."})
        else:
            return json.dumps({"status": "success", "consent": "denied",
                               "message": "Consent denied. End the call politely."})

    async def _handle_save_data(self, args: dict) -> str:
        if not self.db_call_id:
            return json.dumps({"error": "No active session"})

        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CustomerDataRepository(session, encryptor)
                await repo.create_or_update(self.db_call_id, args)
                required = ["full_name", "email", "age", "zipcode", "state"]
                missing = [f for f in required if not args.get(f)]
                await repo.mark_complete(self.db_call_id, missing or None)
                await session.commit()

            return json.dumps({"status": "success", "message": "Data saved."})
        except Exception as e:
            logger.error("save_data_error", error=str(e))
            return json.dumps({"error": f"Save failed: {str(e)}"})

    async def _handle_check_slot(self, args: dict) -> str:
        """Check if a requested appointment slot is available."""
        requested_slot = args.get("requested_slot", "")
        
        if not self.call_context:
            return json.dumps({
                "available": True,
                "message": "No slot restrictions. Any time works.",
            })
        
        # Get available slots
        available_slots = self.call_context.available_slots
        
        if not available_slots:
            return json.dumps({
                "available": False,
                "message": "No slots currently available.",
                "alternatives": [],
            })
        
        # Simple check - just see if any slots exist
        # In a real implementation, would parse the requested_slot and match against available_slots
        # For now, return the first 3 available slots as alternatives
        alternatives = available_slots[:3]
        formatted_alternatives = []
        for slot in alternatives:
            date_str, time_str = self.call_context.parse_slot_datetime(slot)
            formatted_alternatives.append(f"{date_str} at {time_str}")
        
        return json.dumps({
            "available": True,
            "message": f"Available slots: {', '.join(formatted_alternatives)}",
            "alternatives": formatted_alternatives,
            "total_available": len(available_slots),
        })

    async def _handle_end_call(self, args: dict) -> str:
        reason = args.get("reason", "completed")
        if self._call_ended:
            return json.dumps({"status": "already_ended"})
        self._call_ended = True

        # Update DB status
        if self.db_call_id:
            try:
                status_map = {
                    "completed": CallStatus.COMPLETED,
                    "no_consent": CallStatus.NO_CONSENT,
                    "customer_request": CallStatus.COMPLETED,
                    "error": CallStatus.FAILED,
                    "timeout": CallStatus.TIMEOUT,
                }
                async with async_session_factory() as session:
                    encryptor = get_encryptor()
                    repo = CallSessionRepository(session, encryptor)
                    await repo.update_status(self.db_call_id, status_map.get(reason, CallStatus.COMPLETED))
                    await session.commit()
            except Exception as e:
                logger.error("end_call_db_error", error=str(e))

        # Hang up after delay (lets goodbye audio play)
        asyncio.create_task(self._hangup_after_delay(5.0))
        return json.dumps({"status": "success", "message": "Call ending."})

    async def _hangup_after_delay(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self.call_sid:
            return
        try:
            client = TwilioClient(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
            client.calls(self.call_sid).update(status="completed")
            logger.info("call_hung_up", call_id=self.call_id)
        except Exception as e:
            logger.error("hangup_error", error=str(e))

    # ── Error / Session End ───────────────────────────────────

    async def _on_openai_error(self, error_msg: str) -> None:
        logger.error("openai_error", call_id=self.call_id, error=error_msg)

    async def _on_openai_session_end(self) -> None:
        logger.info("openai_session_ended", call_id=self.call_id)

    # ── Cleanup ───────────────────────────────────────────────

    async def _cleanup(self) -> None:
        """Clean up all resources. Runs exactly once."""
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if self.call_sid and self.call_sid in CallManager._active_calls:
            del CallManager._active_calls[self.call_sid]
            # Clean up call context
            remove_call_context(self.call_sid)

        if self.openai_client:
            await self.openai_client.disconnect()

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

    @classmethod
    def get_audio_stats(cls) -> dict:
        return cls._audio_stats.copy()
