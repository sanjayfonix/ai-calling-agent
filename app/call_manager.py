"""
Call Manager -- central orchestrator for a single phone call.

Bridges: Twilio Media Stream <-> OpenAI Realtime API.
Database removed - all call data is sent to backend webhook after call completion.

Key design decisions for clean audio:
1. On speech_started: Clear Twilio audio buffer immediately. This stops AI audio
   playback on the customer's phone, breaking the echo loop (AI -> speaker -> mic -> OpenAI).
2. Do NOT manually cancel OpenAI responses. Let server VAD handle it natively.
3. Cleanup runs exactly once via _cleaned_up flag.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from starlette.websockets import WebSocket
from twilio.rest import Client as TwilioClient
import structlog

from app.config import get_settings
from app.openai_realtime import OpenAIRealtimeClient
from app.twilio_handler import TwilioMediaStreamHandler
from app.call_context import CallContext, get_call_context, remove_call_context, store_call_context
from app.dynamic_prompts import generate_dynamic_system_prompt
from app.dynamic_collection_flow import fetch_dynamic_collection_flow, build_dynamic_prompt

logger = structlog.get_logger(__name__)

REQUIRED_CALLBACK_URL = "https://phpstack-1472627-5654843.cloudwaysapps.com/api/ai-call/call-complete"


class CallManager:
    """Manages one phone call end-to-end."""

    _active_calls: dict[str, "CallManager"] = {}
    # Audio debug counters (class-level, reset per call)
    _audio_stats: dict[str, int] = {"openai_received": 0, "twilio_sent": 0, "skipped": 0}
    # Canonical customer payload schema expected by callback consumers.
    _CUSTOMER_DATA_FIELDS: list[str] = [
        "full_name",
        "email",
        "phone_number",
        "date_of_birth",
        "address",
        "country",
        "zipcode",
        "state",
        "age",
        "income_range",
        "household_size",
        "currently_insured",
        "life_event",
        "life_event_details",
        "sep_reason",
        "preferred_contact_time",
        "wants_aca_explanation",
        "aca_explained",
        "doctor_name",
        "doctor_specialty",
        "medication_name",
        "wants_meeting",
        "scheduled_meeting_datetime",
    ]
    # Backward-compat keys that may still arrive from prompt/tool extraction.
    _CUSTOMER_DATA_ALIASES: dict[str, str] = {
        "tax_household_size": "household_size",
        "preferred_time_slot": "preferred_contact_time",
        "household_income": "income_range",
    }

    def __init__(self, websocket: WebSocket, temp_context_id: str | None=None):
        self.settings = get_settings()
        self.websocket = websocket
        self.call_id: str | None = None
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.db_call_id: uuid.UUID | None = None
        self.call_context: CallContext | None = None
        self.temp_context_id = temp_context_id  # For Method 2 (/twilio/voice)

        self.twilio_handler: TwilioMediaStreamHandler | None = None
        self.openai_client: OpenAIRealtimeClient | None = None

        self._consent_received = False
        self._call_ended = False
        self._cleaned_up = False
        self._greeting_sent = False
        # In-memory tracking of collected data (survives even if save_customer_data is never called)
        self._collected_data: dict[str, Any] = {}
        self._transcript_buffer: list[dict] = []  # In-memory transcript backup
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

    async def _on_call_started(self, call_sid: str, stream_sid: str, context_id: str | None=None) -> None:
        """Twilio stream connected. Set up OpenAI and start conversation."""
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        CallManager._active_calls[call_sid] = self

        logger.info("call_connected", call_id=self.call_id, call_sid=call_sid, context_id_from_twilio=context_id)

        # Retrieve call context by call_sid (stored when outbound call was initiated - Method 1)
        self.call_context = get_call_context(call_sid)
        
        # If no context found, try context_id from Twilio Stream parameters (Method 2)
        if not self.call_context and context_id:
            logger.info("checking_context_from_twilio_param", call_id=self.call_id, context_id=context_id)
            temp_context = get_call_context(context_id)
            if temp_context:
                # Map temp context to real call_sid
                store_call_context(call_sid, temp_context)
                self.call_context = temp_context
                # Clean up temp entry
                remove_call_context(context_id)
                logger.info("call_context_mapped", call_id=self.call_id, from_context_id=context_id, to_call_sid=call_sid)
        
        # Fallback to temp_context_id from WebSocket init (backup method)
        if not self.call_context and self.temp_context_id:
            logger.info("checking_temp_context", call_id=self.call_id, temp_id=self.temp_context_id)
            temp_context = get_call_context(self.temp_context_id)
            if temp_context:
                store_call_context(call_sid, temp_context)
                self.call_context = temp_context
                remove_call_context(self.temp_context_id)
                logger.info("call_context_mapped_from_websocket", call_id=self.call_id, from_temp_id=self.temp_context_id, to_call_sid=call_sid)
        
        if self.call_context:
            logger.info("call_context_loaded", call_id=self.call_id, agent=self.call_context.agent_name)
        else:
            logger.info("no_call_context", call_id=self.call_id, using_default_prompt=True)

        # Fetch dynamic collection flow from backend API
        flow_data = await fetch_dynamic_collection_flow()
        
        # Generate base dynamic system prompt based on context
        base_prompt = generate_dynamic_system_prompt(self.call_context)
        
        # Apply dynamic questions from flow data (if available)
        system_prompt = build_dynamic_prompt(flow_data, base_prompt)

        # Database removed - data sent to backend webhook instead
        # Create DB record (non-fatal if fails)
        # try:
        #     async with async_session_factory() as session:
        #         encryptor = get_encryptor()
        #         repo = CallSessionRepository(session, encryptor)
        #         record = await repo.create(
        #             twilio_call_sid=call_sid,
        #             from_number="outbound",
        #             to_number=self.settings.twilio_phone_number,
        #         )
        #         self.db_call_id = record.id
        #         await repo.update_status(record.id, CallStatus.IN_PROGRESS)
        #         await session.commit()
        # except Exception as e:
        #     logger.error("db_create_error", call_id=self.call_id, error=str(e))

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
                logger.info("openai_connected_ok", call_id=self.call_id, attempt=attempt)
                break
            except Exception as e:
                logger.error("openai_connect_failed", call_id=self.call_id, attempt=attempt, error=str(e))
                if attempt == 1:
                    return
                await asyncio.sleep(0.5)

        # Wait for session config to be confirmed by OpenAI before triggering greeting
        # Reduced timeout to 1.5 seconds to minimize call start delay
        if self.openai_client and self.openai_client.is_connected:
            session_ready = await self.openai_client.wait_for_session_ready(timeout=1.5)
            if not session_ready:
                logger.warning("session_not_ready_proceeding_anyway", call_id=self.call_id)

        # Send initial greeting immediately
        if self.openai_client and self.openai_client.is_connected and not self._greeting_sent:
            self._greeting_sent = True
            logger.info("triggering_initial_greeting", call_id=self.call_id)
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
        """Store transcript in DB and in-memory buffer."""
        if not content.strip():
            return
        # Always keep in-memory copy
        self._transcript_buffer.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if not self.db_call_id:
            return
        logger.info("transcript", call_id=self.call_id, role=role, text=content[:80])
        # Database removed - transcript sent to backend webhook instead
        # try:
        #     async with async_session_factory() as session:
        #         repo = TranscriptRepository(session)
        #         await repo.add_entry(self.db_call_id, role, content)
        #         await session.commit()
        # except Exception as e:
        #     logger.error("transcript_save_error", error=str(e))

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
        
        # Track consent in collected data
        self._collected_data["consent_given"] = consent

        # Database removed - consent sent to backend webhook instead
        # if self.db_call_id:
        #     try:
        #         async with async_session_factory() as session:
        #             encryptor = get_encryptor()
        #             repo = CallSessionRepository(session, encryptor)
        #             status = ConsentStatus.GRANTED if consent else ConsentStatus.DENIED
        #             await repo.update_consent(self.db_call_id, status)
        #             if not consent:
        #                 await repo.update_status(self.db_call_id, CallStatus.NO_CONSENT)
        #             await session.commit()
        #     except Exception as e:
        #         logger.error("consent_db_error", error=str(e))

        if consent:
            return json.dumps({"status": "success", "consent": "granted",
                               "message": "Consent recorded. Proceed with questions."})
        else:
            return json.dumps({"status": "success", "consent": "denied",
                               "message": "Consent denied. End the call politely."})

    async def _handle_save_data(self, args: dict) -> str:
        # Always store in memory first (survives even if DB fails).
        # Do not keep null/blank values because they can overwrite real values
        # extracted from transcript later during webhook payload assembly.
        clean_args = {
            k: v
            for k, v in args.items()
            if v is not None and (not isinstance(v, str) or v.strip())
        }
        self._collected_data.update(clean_args)
        logger.info("customer_data_collected", call_id=self.call_id, fields=list(clean_args.keys()))

        # Database removed - data sent to backend webhook instead
        # if not self.db_call_id:
        #     return json.dumps({"error": "No active session"})

        # try:
        #     async with async_session_factory() as session:
        #         encryptor = get_encryptor()
        #         repo = CustomerDataRepository(session, encryptor)
        #         await repo.create_or_update(self.db_call_id, args)
        #         required = ["full_name", "email", "age", "zipcode", "state"]
        #         missing = [f for f in required if not args.get(f)]
        #         await repo.mark_complete(self.db_call_id, missing or None)
        #         await session.commit()

        #     return json.dumps({"status": "success", "message": "Data saved."})
        # except Exception as e:
        #     logger.error("save_data_error", error=str(e))
        #     return json.dumps({"error": f"Save failed: {str(e)}"})
        
        return json.dumps({"status": "success", "message": "Data saved to memory and will be sent to webhook."})

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
        # Database removed - call status sent to backend webhook instead
        # if self.db_call_id:
        #     try:
        #         status_map = {
        #             "completed": CallStatus.COMPLETED,
        #             "no_consent": CallStatus.NO_CONSENT,
        #             "customer_request": CallStatus.COMPLETED,
        #             "error": CallStatus.FAILED,
        #             "timeout": CallStatus.TIMEOUT,
        #         }
        #         async with async_session_factory() as session:
        #             encryptor = get_encryptor()
        #             repo = CallSessionRepository(session, encryptor)
        #             await repo.update_status(self.db_call_id, status_map.get(reason, CallStatus.COMPLETED))
        #             await session.commit()
        #     except Exception as e:
        #         logger.error("end_call_db_error", error=str(e))

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

        # Log final audio stats
        logger.info("call_audio_stats", call_id=self.call_id, stats=CallManager._audio_stats)

        # Database removed - all data sent to backend webhook instead
        # Auto-save any in-memory collected data to database before webhook
        # if self._collected_data and self.db_call_id:
        #     try:
        #         async with async_session_factory() as session:
        #             encryptor = get_encryptor()
        #             repo = CustomerDataRepository(session, encryptor)
        #             # Remove internal tracking fields
        #             clean_data = {k: v for k, v in self._collected_data.items() if k != "consent_given"}
        #             if clean_data:
        #                 await repo.create_or_update(self.db_call_id, clean_data)
        #                 await session.commit()
        #                 logger.info("auto_saved_collected_data", call_id=self.call_id, fields=list(clean_data.keys()))
        #     except Exception as e:
        #         logger.error("auto_save_data_error", call_id=self.call_id, error=str(e))

        # Send call results to Express backend
        await self._send_call_complete_webhook()

        if self.call_sid and self.call_sid in CallManager._active_calls:
            del CallManager._active_calls[self.call_sid]
            # Clean up call context
            remove_call_context(self.call_sid)

        if self.openai_client:
            await self.openai_client.disconnect()

        # Database removed - call status sent to backend webhook instead
        # if self.db_call_id and not self._call_ended:
        #     try:
        #         async with async_session_factory() as session:
        #             encryptor = get_encryptor()
        #             repo = CallSessionRepository(session, encryptor)
        #             await repo.update_status(self.db_call_id, CallStatus.COMPLETED)
        #             await session.commit()
        #     except Exception as e:
        #         logger.error("cleanup_db_error", error=str(e))

        logger.info("call_cleaned_up", call_id=self.call_id)

    async def _send_call_complete_webhook(self) -> None:
        """Send call results to the backend when call completes."""
        # Try callback URL from call context first, then fallback to configured backend URL
        callback_url = ""
        if self.call_context and self.call_context.callback_url:
            callback_url = self.call_context.callback_url
        else:
            callback_url = self.settings.backend_webhook_url

        # Always send call-complete payloads to the required Cloudways endpoint.
        if not callback_url:
            logger.warning(
                "callback_url_missing_using_required",
                call_id=self.call_id,
                new_url=REQUIRED_CALLBACK_URL,
            )
            callback_url = REQUIRED_CALLBACK_URL
        elif callback_url.rstrip("/") != REQUIRED_CALLBACK_URL:
            logger.warning(
                "callback_url_forced_to_required",
                call_id=self.call_id,
                old_url=callback_url,
                new_url=REQUIRED_CALLBACK_URL,
            )
            callback_url = REQUIRED_CALLBACK_URL
        
        if not callback_url:
            logger.info("no_callback_url_configured", call_id=self.call_id)
            return

        # Gather all call data
        customer_data = None
        transcript = []
        consent_status = "granted" if self._consent_received else "pending"
        recording_url = None

        # Database removed - use only in-memory data
        # if self.db_call_id:
        #     try:
        #         async with async_session_factory() as session:
        #             encryptor = get_encryptor()
        #             
        #             # Get call session
        #             call_repo = CallSessionRepository(session, encryptor)
        #             call_record = await call_repo.get_by_id(self.db_call_id)
        #             if call_record:
        #                 consent_status = call_record.consent_status.value
        #                 recording_url = call_record.call_recording_url
        #             
        #             # Get customer data from DB
        #             cust_repo = CustomerDataRepository(session, encryptor)
        #             customer_data = await cust_repo.get_by_call_session(self.db_call_id)
        #             
        #             # Get transcript from DB
        #             trans_repo = TranscriptRepository(session)
        #             transcript = await trans_repo.get_transcript(self.db_call_id)
        #     except Exception as e:
        #         logger.error("webhook_data_gather_error", call_id=self.call_id, error=str(e))

        # Use in-memory transcript
        if self._transcript_buffer:
            transcript = self._transcript_buffer

        # BUILD CUSTOMER DATA: use in-memory collected data + transcript extraction
        final_customer_data = {}

        # Layer 1: Extract from transcript (lowest priority, fallback)
        extracted = self._extract_data_from_transcript(transcript)
        if extracted:
            final_customer_data.update(extracted)

        # Layer 2: In-memory collected data from save_customer_data calls
        if self._collected_data:
            # Remove internal tracking fields
            clean_collected = {
                k: v
                for k, v in self._collected_data.items()
                if k != "consent_given" and v is not None and (not isinstance(v, str) or v.strip())
            }
            final_customer_data.update(clean_collected)

        # Layer 3: DB customer data (highest priority, overwrites)
        if customer_data:
            final_customer_data.update({k: v for k, v in customer_data.items() if v is not None})

        # Build payload with complete agent context for linking
        agent_context = {}
        if self.call_context:
            agent_context = {
                "agent_id": self.call_context.agent_id,
                "agent_name": self.call_context.agent_name,
                "agent_email": self.call_context.agent_email,
                "agent_phone": self.call_context.agent_phone,
                "agent_npn": self.call_context.agent_npn,
                "agent_role": self.call_context.agent_role,
                "plan_name": self.call_context.plan_name,
                "to_number": self.call_context.to_number,
            }
        
        payload = {
            "call_sid": self.call_sid,
            "status": "completed",
            "consent_status": consent_status,
            "recording_url": recording_url,
            "agent_context": agent_context,  # All agent data for linking
            "customer_data": self._normalize_customer_data(final_customer_data),  # Customer responses with complete schema
            "transcript": transcript,
        }

        non_null_customer_data = {
            key: value for key, value in payload["customer_data"].items() if value is not None
        }

        logger.info(
            "sending_call_complete_webhook",
            call_id=self.call_id,
            url=callback_url,
            has_customer_data=bool(non_null_customer_data),
            customer_fields=list(non_null_customer_data.keys()),
            customer_data_preview=non_null_customer_data if non_null_customer_data else None,
        )

        parsed_callback = urlparse(callback_url)
        logger.info(
            "call_complete_webhook_target",
            call_id=self.call_id,
            method="POST",
            callback_host=parsed_callback.netloc,
            callback_path=parsed_callback.path,
            callback_query=parsed_callback.query or None,
        )

        logger.info(
            "call_complete_webhook_data_summary",
            call_id=self.call_id,
            call_sid=payload.get("call_sid"),
            status=payload.get("status"),
            consent_status=payload.get("consent_status"),
            agent_context=agent_context,
            customer_data=non_null_customer_data,
            transcript_entries=len(transcript),
            transcript_preview=transcript[:2],
        )

        # Explicit payload log for callback debugging in Node.js integration.
        logger.info(
            "call_complete_webhook_payload",
            call_id=self.call_id,
            url=callback_url,
            payload=payload,
            transcript_entries=len(transcript),
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(callback_url, json=payload)
                if response.status_code >= 400:
                    logger.error(
                        "call_complete_webhook_failed",
                        call_id=self.call_id,
                        url=callback_url,
                        status_code=response.status_code,
                        callback_hit=True,
                        response=response.text[:500] if response.text else "",
                    )
                else:
                    logger.info(
                        "call_complete_webhook_sent",
                        call_id=self.call_id,
                        url=callback_url,
                        status_code=response.status_code,
                        callback_hit=True,
                        response=response.text[:500] if response.text else "",
                    )
        except Exception as e:
            logger.error(
                "call_complete_webhook_error",
                call_id=self.call_id,
                url=callback_url,
                callback_hit=False,
                error=str(e),
            )

    def _normalize_customer_data(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Return a stable customer_data payload with all expected keys.

        Missing fields are explicitly set to None so the backend can persist a
        complete row shape without guessing absent keys.
        """
        normalized = {field: None for field in self._CUSTOMER_DATA_FIELDS}

        for key, value in (raw_data or {}).items():
            if key in normalized:
                normalized[key] = value
                continue

            alias_target = self._CUSTOMER_DATA_ALIASES.get(key)
            if alias_target and normalized.get(alias_target) is None:
                normalized[alias_target] = value

        # Treat blank strings as missing values.
        for key, value in normalized.items():
            if isinstance(value, str) and not value.strip():
                normalized[key] = None

        return normalized

    def _extract_data_from_transcript(self, transcript: list[dict]) -> dict:
        """Extract customer data from conversation transcript as a fallback.
        Parses customer responses to identify name, email, age, zip, state etc."""
        extracted = {}
        if not transcript:
            return extracted

        noise_tokens = {
            "bye",
            "bye.",
            "bye everyone",
            "bye, everyone.",
            "thank you",
            "thank you.",
            "thank you very much",
            "thank you very much.",
            "cheers",
            "peace",
            "but",
        }

        customer_messages = [t["content"] for t in transcript if t.get("role") == "customer"]
        agent_messages = [t["content"] for t in transcript if t.get("role") == "agent"]
        all_text = " ".join(customer_messages).lower()

        # Extract email (look for @ pattern)
        for msg in customer_messages:
            email_match = re.search(r'[\w.+-]+\s*(?:at the rate|@)\s*[\w.-]+\.[a-z]{2,}', msg, re.IGNORECASE)
            if email_match:
                email = email_match.group(0)
                # Normalize "at the rate" to @
                email = re.sub(r'\s*at the rate\s*', '@', email, flags=re.IGNORECASE)
                email = email.replace(' ', '')
                extracted["email"] = email
                break

        # Also check agent confirmation for email
        for msg in agent_messages:
            email_match = re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', msg, re.IGNORECASE)
            if email_match:
                extracted["email"] = email_match.group(0)

        # Extract full name ("my name is X" or "I'm X" patterns)
        for msg in customer_messages:
            name_match = re.search(r'(?:my name is|i\'?m|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', msg, re.IGNORECASE)
            if name_match:
                extracted["full_name"] = name_match.group(1).title()
                break

        # Fallback: parse agent confirmations (e.g., "I have your name as Zuber")
        if "full_name" not in extracted:
            for msg in agent_messages:
                name_match = re.search(r'i have your name as\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)', msg, re.IGNORECASE)
                if name_match:
                    extracted["full_name"] = name_match.group(1).strip().title()
                    break

        if "full_name" not in extracted:
            for msg in agent_messages:
                name_match = re.search(r'thank you,\s*([A-Za-z]+(?:\s+[A-Za-z]+)?)', msg, re.IGNORECASE)
                if name_match:
                    candidate = name_match.group(1).strip()
                    if candidate.lower() not in {"everyone", "sir", "maam", "ma\"am"}:
                        extracted["full_name"] = candidate.title()
                        break

        # Extract age ("I am X years old" or just a number in context)
        for msg in customer_messages:
            age_match = re.search(r'(?:i\'?m|i am|age is|yeah|yes)?\s*(\d{1,3})(?:\s*years?(?:\s*old)?)', msg, re.IGNORECASE)
            if age_match:
                age = int(age_match.group(1))
                if 18 <= age <= 120:
                    extracted["age"] = age
                    break

        # Fallback: parse age from agent confirmation/question context
        if "age" not in extracted:
            for msg in agent_messages:
                age_match = re.search(r'\b(\d{1,3})\s*years?\s*old\b', msg, re.IGNORECASE)
                if age_match:
                    age = int(age_match.group(1))
                    if 18 <= age <= 120:
                        extracted["age"] = age
                        break

        # Extract date of birth
        for msg in customer_messages:
            dob_match = re.search(
                r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?)\b',
                msg,
                re.IGNORECASE,
            )
            if dob_match:
                extracted["date_of_birth"] = dob_match.group(1)
                break

        # Extract zip code (5 digit number)
        for msg in customer_messages:
            zip_match = re.search(r'\b(\d{5})\b', msg)
            if zip_match:
                extracted["zipcode"] = zip_match.group(1)
                break

        # Extract state from customer messages
        us_states = [
            "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
            "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
            "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
            "maine", "maryland", "massachusetts", "michigan", "minnesota",
            "mississippi", "missouri", "montana", "nebraska", "nevada",
            "new hampshire", "new jersey", "new mexico", "new york",
            "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
            "pennsylvania", "rhode island", "south carolina", "south dakota",
            "tennessee", "texas", "utah", "vermont", "virginia", "washington",
            "west virginia", "wisconsin", "wyoming"
        ]
        for msg in customer_messages:
            msg_lower = msg.lower()
            for state in us_states:
                if state in msg_lower:
                    extracted["state"] = state.title()
                    break
            if "state" in extracted:
                break

        # Fallback: parse state from agent confirmation (e.g., "Great, Florida")
        if "state" not in extracted:
            for msg in agent_messages:
                msg_lower = msg.lower()
                for state in us_states:
                    if re.search(rf'\b{re.escape(state)}\b', msg_lower):
                        extracted["state"] = state.title()
                        break
                if "state" in extracted:
                    break

        # Extract state abbreviation when customer gives short form (e.g., "CA")
        if "state" not in extracted:
            state_abbrev = {
                "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
                "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
                "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
                "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
                "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
                "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
                "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
                "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
                "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
                "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
            }
            for i, t in enumerate(transcript):
                if t.get("role") == "agent" and "state" in t.get("content", "").lower():
                    for j in range(i + 1, min(i + 3, len(transcript))):
                        if transcript[j].get("role") == "customer":
                            resp = transcript[j].get("content", "").strip().upper()
                            if resp in state_abbrev:
                                extracted["state"] = state_abbrev[resp]
                            break
                if "state" in extracted:
                    break

        # Extract address (look for street address patterns)
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "address" in t["content"].lower():
                # Check next customer response for address
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        msg = transcript[j]["content"]
                        # Look for street address patterns (numbers followed by street names)
                        address_match = re.search(r'\d+\s+[\w\s,.-]+(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|way|court|ct|place|pl|apt|apartment|unit|#)', msg, re.IGNORECASE)
                        if address_match:
                            extracted["address"] = address_match.group(0).strip()
                        elif len(msg) > 10 and any(word in msg.lower() for word in ["street", "avenue", "road", "drive", "lane"]):
                            # If pattern doesn't match but contains address keywords, capture the whole message
                            extracted["address"] = msg[:200]
                        break
                if "address" in extracted:
                    break

        # Extract insurance status
        # Look at customer responses after agent asks about insurance
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "insurance" in t["content"].lower() and "coverage" in t["content"].lower():
                # Check next customer responses
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        resp = transcript[j]["content"].lower()
                        if any(w in resp for w in ["no", "don't", "not", "nope"]):
                            extracted["currently_insured"] = False
                        elif any(w in resp for w in ["yes", "yeah", "yep", "i do", "i have"]):
                            extracted["currently_insured"] = True
                        break

        # Extract country
        for msg in customer_messages:
            msg_lower = msg.lower()
            if any(token in msg_lower for token in ["united states", "usa", "us", "u.s."]):
                extracted["country"] = "USA"
                break

        # Extract phone number
        for msg in customer_messages:
            phone_match = re.search(r'(?:\+?1?[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', msg)
            if phone_match:
                extracted["phone_number"] = phone_match.group(0).strip()
                break

        if "phone_number" not in extracted:
            for msg in agent_messages:
                phone_match = re.search(r'(?:\+?1?[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', msg)
                if phone_match:
                    extracted["phone_number"] = re.sub(r'\D', '', phone_match.group(0))[-10:]
                    break

        # Extract household income
        for msg in customer_messages:
            income_match = re.search(r'(?:income|make|earn|salary)[^\d]*(\$?[\d,]+(?:\.\d{2})?[kK]?)', msg, re.IGNORECASE)
            if income_match:
                extracted["household_income"] = income_match.group(1)
                break
            # Also look for standalone dollar amounts
            dollar_match = re.search(r'\$([\d,]+(?:\.\d{2})?[kK]?)', msg)
            if dollar_match:
                extracted["household_income"] = dollar_match.group(0)
                break

        # Extract explicit income range (e.g., "30000-50000", "30k to 50k")
        for msg in customer_messages:
            range_match = re.search(r'\b(\$?\d{2,6}[kK]?)\s*(?:-|to)\s*(\$?\d{2,6}[kK]?)\b', msg, re.IGNORECASE)
            if range_match:
                extracted["income_range"] = f"{range_match.group(1)}-{range_match.group(2)}"
                break

        # Extract tax household size
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "household" in t["content"].lower() and "how many" in t["content"].lower():
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        response_text = transcript[j]["content"].strip().lower()
                        if response_text in noise_tokens:
                            continue
                        size_match = re.search(r'\b(\d{1,2})\b', transcript[j]["content"])
                        if size_match:
                            size = int(size_match.group(1))
                            if 1 <= size <= 10:
                                extracted["household_size"] = size
                        break

        # Extract life event
        life_events = {
            "job loss": "job_loss",
            "lost my job": "job_loss",
            "unemployed": "job_loss",
            "marriage": "marriage",
            "married": "marriage",
            "getting married": "marriage",
            "baby": "baby",
            "pregnant": "baby",
            "expecting": "baby",
            "newborn": "baby",
            "moving": "moving",
            "moved": "moving",
            "relocating": "moving",
        }
        for msg in customer_messages:
            msg_lower = msg.lower()
            for phrase, event_type in life_events.items():
                if phrase in msg_lower:
                    extracted["life_event"] = event_type
                    # Try to extract more details from the same message
                    if len(msg) > len(phrase) + 20:
                        extracted["life_event_details"] = msg[:200]
                    break
            if "life_event" in extracted:
                break

        if extracted.get("life_event") == "moving":
            extracted["sep_reason"] = "Relocation"
        elif extracted.get("life_event") == "job_loss":
            extracted["sep_reason"] = "Loss of coverage"

        # Extract preferred time slot from transcript
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "appointment" in t["content"].lower():
                # Check next customer response for time preference
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        msg = transcript[j]["content"]
                        if msg.strip().lower() in noise_tokens:
                            continue
                        # Look for date/time patterns
                        time_match = re.search(r'(morning|afternoon|evening|night|\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))', msg, re.IGNORECASE)
                        if time_match:
                            extracted["preferred_contact_time"] = msg[:100]
                        break
                if "preferred_contact_time" in extracted:
                    break

        # Parse meeting confirmation from agent summary lines
        for msg in agent_messages:
            lowered = msg.lower()
            if any(token in lowered for token in ["i have you down for", "you'll speak with", "scheduled time"]):
                extracted["wants_meeting"] = True
                time_phrase = re.search(
                    r'(tomorrow\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}(?::\d{2})?\s*(?:am|pm)|morning|afternoon|evening|night)',
                    msg,
                    re.IGNORECASE,
                )
                if time_phrase and "preferred_contact_time" not in extracted:
                    extracted["preferred_contact_time"] = time_phrase.group(1)
                break

        # Extract doctor information
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "doctor" in t.get("content", "").lower():
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        msg = transcript[j].get("content", "")
                        doc_match = re.search(r'(?:dr\.?\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', msg)
                        if doc_match:
                            extracted["doctor_name"] = doc_match.group(1)
                        break
                if "doctor_name" in extracted:
                    break

        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "specialty" in t.get("content", "").lower():
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        msg = transcript[j].get("content", "").strip()
                        if msg:
                            extracted["doctor_specialty"] = msg[:80]
                        break
                if "doctor_specialty" in extracted:
                    break

        # Extract medication
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and "medication" in t.get("content", "").lower():
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        msg = transcript[j].get("content", "")
                        med_match = re.search(r'([A-Za-z][A-Za-z0-9\-]{2,})', msg)
                        if med_match and msg.lower() not in {"none", "no", "not taking any"}:
                            extracted["medication_name"] = med_match.group(1)
                        break
                if "medication_name" in extracted:
                    break

        # Extract meeting preference and scheduled datetime
        for i, t in enumerate(transcript):
            agent_text = t.get("content", "").lower()
            if t.get("role") == "agent" and any(k in agent_text for k in ["follow-up", "appointment", "schedule", "meeting"]):
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        resp = transcript[j].get("content", "")
                        resp_lower = resp.lower()
                        if any(w in resp_lower for w in ["yes", "sure", "okay", "works", "fine"]):
                            extracted["wants_meeting"] = True
                        elif any(w in resp_lower for w in ["no", "not now", "later"]):
                            extracted["wants_meeting"] = False
                        iso_match = re.search(r'\b\d{4}-\d{2}-\d{2}t\d{2}:\d{2}(?::\d{2})?z?\b', resp, re.IGNORECASE)
                        if iso_match:
                            extracted["scheduled_meeting_datetime"] = iso_match.group(0)
                        break

        # Extract ACA preference and whether explanation was provided
        for i, t in enumerate(transcript):
            if t.get("role") == "agent" and any(k in t.get("content", "").lower() for k in ["affordable care act", "aca"]):
                for j in range(i + 1, min(i + 3, len(transcript))):
                    if transcript[j].get("role") == "customer":
                        resp = transcript[j].get("content", "").lower()
                        if any(w in resp for w in ["yes", "sure", "okay", "please"]):
                            extracted["wants_aca_explanation"] = True
                        elif any(w in resp for w in ["no", "nope", "not needed"]):
                            extracted["wants_aca_explanation"] = False
                        break

        if any(any(k in t.get("content", "").lower() for k in ["affordable care act", "marketplace", "bronze", "silver", "gold", "platinum"]) for t in transcript if t.get("role") == "agent"):
            extracted["aca_explained"] = True

        # Extract ACA-related info
        for i, t in enumerate(transcript):
            if t.get("role") == "customer" and "aca" in t["content"].lower():
                extracted["wants_aca_explanation"] = True
                # Check if agent explained it in next few messages
                for j in range(i + 1, min(i + 5, len(transcript))):
                    if transcript[j].get("role") == "agent" and len(transcript[j]["content"]) > 100:
                        extracted["aca_explained"] = True
                        break
                break

        logger.info("transcript_data_extracted", call_id=self.call_id, fields=list(extracted.keys()), extracted_data=extracted)
        return extracted

    @classmethod
    def get_active_call(cls, call_sid: str) -> "CallManager | None":
        return cls._active_calls.get(call_sid)

    @classmethod
    def get_active_calls_count(cls) -> int:
        return len(cls._active_calls)

    @classmethod
    def get_audio_stats(cls) -> dict:
        return cls._audio_stats.copy()
