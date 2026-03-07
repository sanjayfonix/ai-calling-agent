"""
Twilio helper — generates TwiML responses and initiates outbound calls.
"""

from __future__ import annotations

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


def create_twilio_client() -> TwilioClient:
    """Create an authenticated Twilio REST client."""
    settings = get_settings()
    return TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)


def generate_media_stream_twiml(websocket_url: str, context_id: str | None = None) -> str:
    """
    Generate TwiML that tells Twilio to connect the call audio
    to our WebSocket endpoint via Media Streams.
    
    Args:
        websocket_url: WebSocket URL to connect to
        context_id: Optional context ID to pass as a Stream parameter
    """
    response = VoiceResponse()

    # Connect to our WebSocket for bidirectional audio (no pause — start immediately)
    connect = Connect()
    stream = Stream(url=websocket_url)
    stream.parameter(name="direction", value="both")
    
    # Add context_id as a custom parameter if provided
    if context_id:
        stream.parameter(name="context_id", value=context_id)
    
    connect.append(stream)
    response.append(connect)

    return str(response)


async def make_outbound_call(
    to_number: str,
    websocket_url: str,
    status_callback_url: str | None=None,
    record: bool=True,
) -> str:
    """
    Initiate an outbound call via Twilio.

    Returns the Call SID.
    """
    settings = get_settings()
    client = create_twilio_client()

    twiml = generate_media_stream_twiml(websocket_url)

    call_params = {
        "to": to_number,
        "from_": settings.twilio_phone_number,
        "twiml": twiml,
        "timeout": 30,
    }

    if record:
        call_params["record"] = True
        call_params["recording_channels"] = "dual"
        call_params["recording_status_callback"] = (
            f"{settings.base_url}/api/webhooks/recording-status"
        )
        call_params["recording_status_callback_event"] = "completed"

    if status_callback_url:
        call_params["status_callback"] = status_callback_url
        call_params["status_callback_event"] = [
            "initiated",
            "ringing",
            "answered",
            "completed",
        ]

    logger.info("initiating_outbound_call", to=to_number)

    call = client.calls.create(**call_params)

    logger.info(
        "outbound_call_created",
        call_sid=call.sid,
        to=to_number,
        status=call.status,
    )

    return call.sid
