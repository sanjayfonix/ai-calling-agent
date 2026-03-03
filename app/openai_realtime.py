"""
OpenAI Realtime API WebSocket client.

Manages a persistent WebSocket connection to OpenAI's Realtime API,
handles bidirectional audio streaming, function calls, and session lifecycle.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any, Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection
import structlog

from app.config import get_settings
from app.prompts import SYSTEM_PROMPT, TOOL_DEFINITIONS

logger = structlog.get_logger(__name__)

# OpenAI Realtime API endpoint
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


class OpenAIRealtimeClient:
    """
    Manages a WebSocket connection to OpenAI Realtime API.

    Lifecycle:
    1. connect() — establish WS connection & configure session
    2. send_audio() — stream audio chunks from Twilio
    3. Receive events via the listener task → call registered handlers
    4. disconnect() — close cleanly
    """

    def __init__(
        self,
        call_id: str,
        on_audio_delta: Callable[[str], Awaitable[None]] | None=None,
        on_transcript: Callable[[str, str], Awaitable[None]] | None=None,
        on_function_call: Callable[[str, dict], Awaitable[str]] | None=None,
        on_error: Callable[[str], Awaitable[None]] | None=None,
        on_session_end: Callable[[], Awaitable[None]] | None=None,
        on_speech_started: Callable[[], Awaitable[None]] | None=None,
        on_response_interrupted: Callable[[], Awaitable[None]] | None=None,
    ):
        self.call_id = call_id
        self.settings = get_settings()
        self._ws: ClientConnection | None = None
        self._listener_task: asyncio.Task | None = None
        self._connected = False
        self._session_ready = asyncio.Event()
        self._current_response_id: str | None = None

        # Event handlers
        self._on_audio_delta = on_audio_delta
        self._on_transcript = on_transcript
        self._on_function_call = on_function_call
        self._on_error = on_error
        self._on_session_end = on_session_end
        self._on_speech_started = on_speech_started
        self._on_response_interrupted = on_response_interrupted

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self) -> None:
        """Establish WebSocket connection and configure the session."""
        model = self.settings.openai_realtime_model
        url = f"{OPENAI_REALTIME_URL}?model={model}"

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        logger.info("openai_connecting", call_id=self.call_id, model=model)

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=2 ** 24,  # 16MB max message
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._connected = True
            logger.info("openai_connected", call_id=self.call_id)

            # Configure the session
            await self._configure_session()

            # Start listener
            self._listener_task = asyncio.create_task(
                self._listen(), name=f"openai-listener-{self.call_id}"
            )

        except Exception as e:
            logger.error("openai_connection_failed", call_id=self.call_id, error=str(e))
            raise

    async def _configure_session(self) -> None:
        """Send session configuration to OpenAI Realtime API."""
        session_config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": SYSTEM_PROMPT,
                "voice": self.settings.openai_realtime_voice,
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "input_audio_transcription": {
                    "model": "whisper-1",
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.3,
                    "prefix_padding_ms": 400,
                    "silence_duration_ms": 700,
                    "create_response": True,
                },
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "temperature": 0.6,
                "max_response_output_tokens": 1024,
            },
        }

        await self._send(session_config)
        logger.info("openai_session_configured", call_id=self.call_id)

    async def send_audio(self, audio_b64: str) -> None:
        """
        Send audio data to OpenAI Realtime API.
        Audio should be base64-encoded G.711 u-law string from Twilio.
        """
        if not self.is_connected:
            return

        event = {
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        }
        await self._send(event)

    async def send_text(self, text: str) -> None:
        """Send a text message (used for initial greeting trigger etc.)."""
        if not self.is_connected:
            return

        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": text,
                    }
                ],
            },
        }
        await self._send(event)
        # Trigger response generation
        await self._send({"type": "response.create"})

    async def trigger_response(self) -> None:
        """Manually trigger AI to generate a response (for initial greeting).
        Waits for session to be ready before triggering."""
        if not self.is_connected:
            logger.warning("trigger_response_not_connected", call_id=self.call_id)
            return
        # Wait up to 5 seconds for session.updated confirmation
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=5.0)
            logger.info("session_ready_confirmed", call_id=self.call_id)
        except asyncio.TimeoutError:
            logger.warning("openai_session_ready_timeout", call_id=self.call_id)
        logger.info("sending_response_create", call_id=self.call_id)
        await self._send({"type": "response.create"})

    async def cancel_response(self) -> None:
        """Cancel the current AI response (for barge-in/interruption).
        After cancelling, OpenAI will process customer audio and auto-respond."""
        if not self.is_connected:
            return
        await self._send({"type": "response.cancel"})
        # Also truncate the last assistant item so OpenAI knows to respond fresh
        logger.info("openai_response_cancelled", call_id=self.call_id)

    async def send_function_result(
        self, call_id: str, result: str
    ) -> None:
        """Send the result of a function call back to OpenAI."""
        if not self.is_connected:
            return

        event = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        }
        await self._send(event)
        # Trigger response after function result
        await self._send({"type": "response.create"})

    async def disconnect(self) -> None:
        """Close the WebSocket connection cleanly."""
        self._connected = False

        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("openai_disconnected", call_id=self.call_id)

    # ── Internal Methods ─────────────────────────────────────

    async def _send(self, event: dict[str, Any]) -> None:
        """Send a JSON event to OpenAI."""
        if self._ws:
            try:
                await self._ws.send(json.dumps(event))
            except Exception as e:
                logger.error(
                    "openai_send_error",
                    call_id=self.call_id,
                    event_type=event.get("type"),
                    error=str(e),
                )

    async def _listen(self) -> None:
        """Listen for events from OpenAI Realtime API and dispatch to handlers."""
        try:
            async for raw_message in self._ws:
                try:
                    event = json.loads(raw_message)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    logger.warning("openai_invalid_json", call_id=self.call_id)
                except Exception as e:
                    logger.error(
                        "openai_event_handler_error",
                        call_id=self.call_id,
                        error=str(e),
                    )
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("openai_connection_closed", call_id=self.call_id, code=e.code)
        except asyncio.CancelledError:
            logger.info("openai_listener_cancelled", call_id=self.call_id)
        except Exception as e:
            logger.error("openai_listener_error", call_id=self.call_id, error=str(e))
        finally:
            self._connected = False
            if self._on_session_end:
                await self._on_session_end()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Route OpenAI events to the appropriate handler."""
        event_type = event.get("type", "")

        match event_type:
            # ── Session Events ───────────────────────────
            case "session.created":
                logger.info(
                    "openai_session_created",
                    call_id=self.call_id,
                    session_id=event.get("session", {}).get("id"),
                )

            case "session.updated":
                logger.info("openai_session_updated", call_id=self.call_id)
                self._session_ready.set()  # Signal that session is configured

            # ── Audio Events ─────────────────────────────
            case "response.audio.delta":
                # Streaming audio from AI → pass base64 directly to Twilio
                audio_b64 = event.get("delta", "")
                if audio_b64 and self._on_audio_delta:
                    await self._on_audio_delta(audio_b64)

            case "response.audio.done":
                logger.debug("openai_audio_done", call_id=self.call_id)

            # ── Transcript Events ────────────────────────
            case "response.audio_transcript.delta":
                pass  # Partial transcript — ignore for now

            case "response.audio_transcript.done":
                transcript = event.get("transcript", "")
                if transcript and self._on_transcript:
                    await self._on_transcript("agent", transcript)

            case "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript and self._on_transcript:
                    await self._on_transcript("customer", transcript)

            # ── Function Call Events ─────────────────────
            case "response.function_call_arguments.done":
                fn_name = event.get("name", "")
                fn_call_id = event.get("call_id", "")
                fn_args_str = event.get("arguments", "{}")

                logger.info(
                    "openai_function_call",
                    call_id=self.call_id,
                    function=fn_name,
                )

                try:
                    fn_args = json.loads(fn_args_str)
                except json.JSONDecodeError:
                    fn_args = {}

                if self._on_function_call:
                    result = await self._on_function_call(fn_name, fn_args)
                    await self.send_function_result(fn_call_id, result)

            # ── Response Events ──────────────────────────
            case "response.created":
                self._current_response_id = event.get("response", {}).get("id")
                logger.info("openai_response_started", call_id=self.call_id, response_id=self._current_response_id)

            case "response.done":
                status = event.get("response", {}).get("status", "")
                response_id = event.get("response", {}).get("id", "")
                self._current_response_id = None
                logger.info(
                    "openai_response_done",
                    call_id=self.call_id,
                    status=status,
                    response_id=response_id,
                )
                # If response was cancelled (user interrupted), clear Twilio buffer
                # so stale audio stops playing immediately. OpenAI handles 
                # truncation of the conversation item internally.
                if status == "cancelled":
                    logger.info("openai_response_interrupted", call_id=self.call_id)
                    if self._on_response_interrupted:
                        await self._on_response_interrupted()
                # If response failed, log for debugging
                elif status == "failed":
                    error_info = event.get("response", {}).get("status_details", {})
                    logger.error(
                        "openai_response_failed",
                        call_id=self.call_id,
                        details=error_info,
                    )

            # ── Error Events ─────────────────────────────
            case "error":
                error_msg = event.get("error", {}).get("message", "Unknown error")
                error_code = event.get("error", {}).get("code", "")
                logger.error(
                    "openai_error",
                    call_id=self.call_id,
                    code=error_code,
                    message=error_msg,
                )
                if self._on_error:
                    await self._on_error(error_msg)

            # ── Rate Limit Events ────────────────────────
            case "rate_limits.updated":
                logger.debug(
                    "openai_rate_limits",
                    call_id=self.call_id,
                    limits=event.get("rate_limits"),
                )

            # ── Input Audio Buffer Events ────────────────
            case "input_audio_buffer.speech_started":
                logger.debug("customer_speaking", call_id=self.call_id)
                # Let OpenAI's server VAD handle interruption natively.
                # We only clear the Twilio audio buffer so stale audio stops playing.
                # Do NOT cancel the response — that causes voice cutting.
                if self._on_speech_started:
                    await self._on_speech_started()

            case "input_audio_buffer.speech_stopped":
                logger.debug("customer_stopped_speaking", call_id=self.call_id)

            case "input_audio_buffer.committed":
                logger.debug("audio_buffer_committed", call_id=self.call_id)

            # ── Response Cancelled (after interruption) ──
            case "response.cancelled":
                logger.info("openai_response_cancelled_event", call_id=self.call_id)
                # Response was cancelled due to interruption — that's fine,
                # OpenAI will process the customer's input and auto-respond

            # ── Catch-all ────────────────────────────────
            case _:
                logger.debug(
                    "openai_unhandled_event",
                    call_id=self.call_id,
                    event_type=event_type,
                )
