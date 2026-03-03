"""
Twilio Media Stream WebSocket handler.

Bridges audio between Twilio's Media Stream and OpenAI Realtime API.
Handles the Twilio-specific WebSocket protocol (connected, start, media, stop events).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect
import structlog

logger = structlog.get_logger(__name__)


class TwilioMediaStreamHandler:
    """
    Handles a single Twilio Media Stream WebSocket connection.

    Protocol:
    - Twilio sends JSON messages: connected, start, media, stop, mark
    - We send JSON messages back: media (audio from AI), mark, clear

    Audio format: G.711 μ-law, 8kHz, mono, base64-encoded
    """

    def __init__(
        self,
        websocket: WebSocket,
        on_audio_received: Any=None,
        on_call_start: Any=None,
        on_call_end: Any=None,
    ):
        self.ws = websocket
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self._connected = False

        # Callbacks
        self._on_audio_received = on_audio_received  # async fn(audio_base64: str)
        self._on_call_start = on_call_start  # async fn(call_sid: str, stream_sid: str)
        self._on_call_end = on_call_end  # async fn()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def handle(self) -> None:
        """Main loop: accept and process Twilio WS messages."""
        await self.ws.accept()
        self._connected = True
        logger.info("twilio_ws_accepted")

        try:
            while True:
                raw = await self.ws.receive_text()
                message = json.loads(raw)
                event = message.get("event", "")

                match event:
                    case "connected":
                        logger.info("twilio_connected", protocol=message.get("protocol"))

                    case "start":
                        await self._handle_start(message)

                    case "media":
                        await self._handle_media(message)

                    case "stop":
                        logger.info("twilio_stream_stopped", stream_sid=self.stream_sid)
                        break

                    case "mark":
                        # Mark event — used for synchronization
                        logger.debug(
                            "twilio_mark",
                            name=message.get("mark", {}).get("name"),
                        )

                    case _:
                        logger.debug("twilio_unknown_event", event=event)

        except WebSocketDisconnect:
            logger.info("twilio_ws_disconnected", stream_sid=self.stream_sid)
        except Exception as e:
            logger.error("twilio_ws_error", error=str(e), stream_sid=self.stream_sid)
        finally:
            self._connected = False
            if self._on_call_end:
                await self._on_call_end()

    async def _handle_start(self, message: dict) -> None:
        """Process the 'start' event from Twilio."""
        start_data = message.get("start", {})
        self.stream_sid = start_data.get("streamSid")
        self.call_sid = start_data.get("callSid")

        logger.info(
            "twilio_stream_started",
            stream_sid=self.stream_sid,
            call_sid=self.call_sid,
            media_format=start_data.get("mediaFormat"),
        )

        if self._on_call_start:
            await self._on_call_start(self.call_sid, self.stream_sid)

    async def _handle_media(self, message: dict) -> None:
        """Process audio from Twilio and forward to OpenAI.
        Only forwards inbound (customer) audio, ignores outbound (AI echo)."""
        media = message.get("media", {})
        track = media.get("track", "inbound")

        # Only forward customer's inbound audio, never our own outbound audio
        if track != "inbound":
            return

        payload = media.get("payload", "")  # base64-encoded G.711 μ-law

        if payload and self._on_audio_received:
            await self._on_audio_received(payload)

    async def send_audio(self, audio_bytes: bytes) -> None:
        """
        Send audio from OpenAI back to Twilio.
        Audio must be base64-encoded G.711 μ-law.
        """
        if not self._connected or not self.stream_sid:
            return

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await self.send_audio_b64(audio_b64)

    async def send_audio_b64(self, audio_b64: str) -> None:
        """
        Send pre-encoded base64 audio directly to Twilio.
        Avoids decode/re-encode when audio is already base64 from OpenAI.
        """
        if not self._connected or not self.stream_sid:
            return

        message = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {
                "payload": audio_b64,
            },
        }

        try:
            await self.ws.send_json(message)
        except Exception as e:
            logger.error("twilio_send_error", error=str(e))

    async def send_mark(self, name: str) -> None:
        """Send a mark event to Twilio for synchronization."""
        if not self._connected or not self.stream_sid:
            return

        message = {
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": name},
        }

        try:
            await self.ws.send_json(message)
        except Exception as e:
            logger.error("twilio_mark_error", error=str(e))

    async def clear_audio(self) -> None:
        """Clear any queued audio on Twilio's side (for interruption handling)."""
        if not self._connected or not self.stream_sid:
            return

        message = {
            "event": "clear",
            "streamSid": self.stream_sid,
        }

        try:
            await self.ws.send_json(message)
        except Exception as e:
            logger.error("twilio_clear_error", error=str(e))

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        try:
            await self.ws.close()
        except Exception:
            pass
