"""
OpenAI Realtime API WebSocket client.

Clean, production-ready implementation for bidirectional audio streaming.
Handles session config, audio I/O, function calls, and interruptions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection
import structlog

from app.config import get_settings
from app.prompts import SYSTEM_PROMPT, TOOL_DEFINITIONS

logger = structlog.get_logger(__name__)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


class OpenAIRealtimeClient:
    """
    WebSocket client for OpenAI Realtime API.

    Audio flow:
      Twilio (G.711 ulaw base64) -> send_audio() -> OpenAI
      OpenAI -> on_audio_delta callback -> Twilio

    Interruption flow (handled by OpenAI server VAD):
      Customer speaks -> speech_started -> we clear Twilio buffer
      Customer stops  -> OpenAI auto-creates response
      If AI was speaking -> response.done(cancelled) -> we clear buffer again
    """

    def __init__(
        self,
        call_id: str,
        on_audio_delta: Callable[[str], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, str], Awaitable[None]] | None = None,
        on_function_call: Callable[[str, dict], Awaitable[str]] | None = None,
        on_error: Callable[[str], Awaitable[None]] | None = None,
        on_session_end: Callable[[], Awaitable[None]] | None = None,
        on_speech_started: Callable[[], Awaitable[None]] | None = None,
    ):
        self.call_id = call_id
        self.settings = get_settings()
        self._ws: ClientConnection | None = None
        self._listener_task: asyncio.Task | None = None
        self._connected = False
        self._session_ready = asyncio.Event()

        # Callbacks
        self._on_audio_delta = on_audio_delta
        self._on_transcript = on_transcript
        self._on_function_call = on_function_call
        self._on_error = on_error
        self._on_session_end = on_session_end
        self._on_speech_started = on_speech_started

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API and configure session."""
        model = self.settings.openai_realtime_model
        url = f"{OPENAI_REALTIME_URL}?model={model}"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        logger.info("openai_connecting", call_id=self.call_id, model=model)

        self._ws = await websockets.connect(
            url,
            additional_headers=headers,
            max_size=2 ** 24,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True
        logger.info("openai_connected", call_id=self.call_id)

        await self._configure_session()

        self._listener_task = asyncio.create_task(
            self._listen(), name=f"openai-listener-{self.call_id}"
        )

    async def _configure_session(self) -> None:
        """Configure OpenAI session with VAD, voice, and tools.

        KEY SETTINGS for clean phone calls:
        - threshold 0.8: Very high — only triggers on clear human speech, ignores
          background noise, TV, echo from speaker, breathing, etc.
        - silence_duration_ms 1200: Waits 1.2 seconds of silence before considering
          the customer done speaking. Prevents cutting off mid-sentence.
        - prefix_padding_ms 300: Captures 300ms of audio before speech was detected
          to avoid clipping the start of words.
        - create_response true: OpenAI auto-generates response when customer stops.
          No manual response.create needed for normal conversation turns.
        """
        await self._send({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": SYSTEM_PROMPT,
                "voice": self.settings.openai_realtime_voice,
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.8,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 1200,
                    "create_response": True,
                },
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "temperature": 0.6,
                "max_response_output_tokens": 500,
            },
        })
        logger.info("openai_session_configured", call_id=self.call_id)

    async def send_audio(self, audio_b64: str) -> None:
        """Forward base64 G.711 ulaw audio from Twilio to OpenAI."""
        if not self.is_connected:
            return
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        })

    async def trigger_response(self) -> None:
        """Trigger the initial greeting. Waits for session to be configured."""
        if not self.is_connected:
            return
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("session_ready_timeout", call_id=self.call_id)
        await self._send({"type": "response.create"})

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Return function call result to OpenAI and trigger next response."""
        if not self.is_connected:
            return
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        })
        await self._send({"type": "response.create"})

    async def disconnect(self) -> None:
        """Close connection cleanly."""
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

    async def _send(self, event: dict[str, Any]) -> None:
        """Send JSON event to OpenAI."""
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
        """Listen for events from OpenAI and dispatch to handlers."""
        try:
            async for raw_message in self._ws:
                try:
                    event = json.loads(raw_message)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error("openai_event_error", call_id=self.call_id, error=str(e))
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("openai_ws_closed", call_id=self.call_id, code=e.code)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("openai_listener_error", call_id=self.call_id, error=str(e))
        finally:
            self._connected = False
            if self._on_session_end:
                await self._on_session_end()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Route OpenAI Realtime events."""
        t = event.get("type", "")

        # -- Session lifecycle --
        if t == "session.created":
            logger.info("openai_session_created", call_id=self.call_id)

        elif t == "session.updated":
            logger.info("openai_session_updated", call_id=self.call_id)
            self._session_ready.set()

        # -- Audio from AI -> Twilio --
        elif t == "response.audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64 and self._on_audio_delta:
                await self._on_audio_delta(audio_b64)

        # -- Transcripts (for logging/DB) --
        elif t == "response.audio_transcript.done":
            text = event.get("transcript", "")
            if text and self._on_transcript:
                await self._on_transcript("agent", text)

        elif t == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            if text and self._on_transcript:
                await self._on_transcript("customer", text)

        # -- Function calls --
        elif t == "response.function_call_arguments.done":
            fn_name = event.get("name", "")
            fn_call_id = event.get("call_id", "")
            try:
                fn_args = json.loads(event.get("arguments", "{}"))
            except json.JSONDecodeError:
                fn_args = {}
            logger.info("openai_function_call", call_id=self.call_id, function=fn_name)
            if self._on_function_call:
                result = await self._on_function_call(fn_name, fn_args)
                await self.send_function_result(fn_call_id, result)

        # -- Customer speech detected (VAD) --
        # CRITICAL: Clear Twilio buffer so AI audio stops playing immediately.
        # This breaks the echo loop (AI audio -> phone speaker -> mic -> OpenAI).
        # Do NOT cancel the response — let OpenAI's VAD handle it natively.
        elif t == "input_audio_buffer.speech_started":
            logger.info("speech_started", call_id=self.call_id)
            if self._on_speech_started:
                await self._on_speech_started()

        # -- Response lifecycle --
        elif t == "response.done":
            status = event.get("response", {}).get("status", "")
            if status == "cancelled":
                # Customer interrupted — OpenAI cancelled the response.
                # Twilio buffer already cleared on speech_started.
                logger.info("response_interrupted", call_id=self.call_id)
            elif status == "failed":
                details = event.get("response", {}).get("status_details", {})
                logger.error("response_failed", call_id=self.call_id, details=details)

        # -- Errors --
        elif t == "error":
            msg = event.get("error", {}).get("message", "Unknown")
            code = event.get("error", {}).get("code", "")
            logger.error("openai_error", call_id=self.call_id, code=code, message=msg)
            if self._on_error:
                await self._on_error(msg)
