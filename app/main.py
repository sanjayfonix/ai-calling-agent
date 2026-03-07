"""
FastAPI Application — Main entry point.

Routes:
- POST /api/webhooks/twilio-voice     → Inbound call webhook (returns TwiML)
- WS   /ws/media-stream               → Twilio Media Stream WebSocket
- POST /api/webhooks/call-status       → Call status updates from Twilio
- POST /api/webhooks/recording-status  → Recording completion webhook
- POST /api/calls/outbound             → Initiate an outbound call
- GET  /api/calls                      → List recent calls
- GET  /api/calls/{call_sid}           → Get call details
- GET  /api/health                     → Health check
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.config import get_settings
from app.database import init_db, close_db, async_session_factory
from app.encryption import get_encryptor
from app.logging_config import setup_logging
from app.call_manager import CallManager
from app.models import CallSession, CustomerData, CallTranscript, CallStatus
from app.repository import (
    CallSessionRepository,
    CustomerDataRepository,
    TranscriptRepository,
)
from app.twilio_service import generate_media_stream_twiml, make_outbound_call
from app.call_context import CallContext, store_call_context

logger = structlog.get_logger(__name__)


# ── Lifespan ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    setup_logging()
    logger.info("application_starting")

    # Initialize database tables
    await init_db()
    logger.info("database_initialized")

    yield

    # Cleanup
    await close_db()
    logger.info("application_shutdown")


# ── App Creation ─────────────────────────────────────────────
app = FastAPI(
    title="AI Calling Agent",
    description="Production-ready AI calling agent with OpenAI Realtime API and Twilio",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response Models ──────────────────────────────────
class OutboundCallRequest(BaseModel):
    to_number: str = Field(..., description="Phone number to call (E.164 format, e.g., +1234567890)")
    agent_id: int = Field(1, description="Agent ID")
    agent_name: str = Field("John Doe", description="Agent full name")
    agent_email: str = Field("agent@health.com", description="Agent email")
    agent_phone: str = Field("+1234567890", description="Agent phone number")
    agent_npn: str = Field("12345678", description="Agent NPN number")
    agent_role: str = Field("Agent", description="Agent role")
    plan_name: str = Field("STARTER", description="Plan name")
    slots: str = Field("", description="Comma-separated available slots (e.g., '2026-03-05|09:00,2026-03-05|09:30')")
    slots_count: int = Field(0, description="Number of available slots")
    record: bool = Field(True, description="Whether to record the call")
    callback_url: str = Field("https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete", description="URL to POST call results when call completes")


class OutboundCallResponse(BaseModel):
    call_sid: str
    status: str
    message: str


class CallDetailResponse(BaseModel):
    id: str
    call_sid: str
    from_number: str
    to_number: str
    status: str
    consent_status: str
    duration_seconds: int | None
    recording_url: str | None
    started_at: str | None
    ended_at: str | None
    customer_data: dict | None
    transcript: list[dict] | None


class HealthResponse(BaseModel):
    status: str
    version: str
    active_calls: int
    timestamp: str

# ══════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════


# ── Health Check ─────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "active_calls": CallManager.get_active_calls_count(),
        "audio_stats": CallManager.get_audio_stats(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Debug: Show TwiML and WebSocket URL ──────────────────────
@app.get("/api/debug/twiml")
async def debug_twiml():
    """Debug endpoint to verify TwiML generation and WebSocket URL."""
    settings = get_settings()
    ws_scheme = "wss" if settings.base_url.startswith("https") else "ws"
    host = settings.base_url.replace("https://", "").replace("http://", "")
    websocket_url = f"{ws_scheme}://{host}/ws/media-stream"
    twiml = generate_media_stream_twiml(websocket_url)
    return {
        "base_url": settings.base_url,
        "websocket_url": websocket_url,
        "openai_model": settings.openai_realtime_model,
        "openai_voice": settings.openai_realtime_voice,
        "twiml": twiml,
    }


# ── Debug: Show stored call contexts ──────────────────────
@app.get("/api/debug/contexts")
async def debug_contexts():
    """Debug endpoint to see stored call contexts."""
    from app.call_context import _call_contexts
    return {
        "stored_contexts": {
            call_sid: {
                "agent_name": ctx.agent_name,
                "agent_id": ctx.agent_id,
                "plan_name": ctx.plan_name,
                "slots_count": ctx.slots_count
            }
            for call_sid, ctx in _call_contexts.items()
        },
        "count": len(_call_contexts)
    }


# ── Dynamic Twilio Voice Webhook (from Express Backend) ──────
@app.get("/twilio/voice")
async def dynamic_twilio_voice_webhook(
    agent_id: int,
    agent_name: str,
    agent_email: str,
    agent_phone: str,
    agent_npn: str,
    agent_role: str,
    plan_name: str,
    slots: str,  # Comma-separated slots: "2026-03-05|09:00,2026-03-05|09:30,..."
    slots_count: int,
):
    """
    Endpoint called by Express backend with dynamic call context.
    Receives agent info and available slots, then returns TwiML to initiate the call.
    """
    settings = get_settings()
    
    # Parse slots from comma-separated string
    slots_list = [s.strip() for s in slots.split(",") if s.strip()]
    
    # Create call context
    context = CallContext(
        agent_id=agent_id,
        agent_name=agent_name,
        agent_email=agent_email,
        agent_phone=agent_phone,
        agent_npn=agent_npn,
        agent_role=agent_role,
        plan_name=plan_name,
        slots=slots_list,
        slots_count=slots_count,
    )
    
    # Generate a temporary call ID (will be replaced with actual call_sid)
    temp_call_id = f"temp_{uuid.uuid4()}"
    store_call_context(temp_call_id, context)
    
    logger.info(
        "dynamic_call_context_received",
        agent_id=agent_id,
        agent_name=agent_name,
        plan_name=plan_name,
        available_slots=len(slots_list),
    )
    
    # Build WebSocket URL (no query params needed - using Stream parameters instead)
    ws_scheme = "wss" if settings.base_url.startswith("https") else "ws"
    host = settings.base_url.replace("https://", "").replace("http://", "")
    websocket_url = f"{ws_scheme}://{host}/ws/media-stream"
    
    # Generate TwiML with context_id as a Stream parameter
    twiml = generate_media_stream_twiml(websocket_url, context_id=temp_call_id)
    
    return PlainTextResponse(content=twiml, media_type="application/xml")


# ── Twilio Inbound Voice Webhook ─────────────────────────────
@app.post("/api/webhooks/twilio-voice")
async def twilio_voice_webhook(request: Request):
    """
    Twilio calls this when an inbound call arrives.
    Returns TwiML that connects the call to our WebSocket for Media Streams.
    """
    settings = get_settings()

    # Build WebSocket URL (wss:// for production)
    ws_scheme = "wss" if settings.base_url.startswith("https") else "ws"
    host = settings.base_url.replace("https://", "").replace("http://", "")
    websocket_url = f"{ws_scheme}://{host}/ws/media-stream"

    logger.info("inbound_call_received", webhook_url=websocket_url)

    twiml = generate_media_stream_twiml(websocket_url)

    return PlainTextResponse(content=twiml, media_type="application/xml")


# ── Twilio Media Stream WebSocket ────────────────────────────
@app.websocket("/ws/media-stream")
async def media_stream_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for Twilio Media Streams.
    Each connection = one phone call.
    """
    # Extract context_id from query parameters (for Method 2)
    context_id = websocket.query_params.get("context_id")
    logger.info("media_stream_ws_connecting", 
                context_id=context_id,
                query_params=dict(websocket.query_params),
                url=str(websocket.url))

    manager = CallManager(websocket, temp_context_id=context_id)
    await manager.start()


# ── Call Status Webhook ──────────────────────────────────────
@app.post("/api/webhooks/call-status")
async def call_status_webhook(request: Request):
    """Handle Twilio call status updates."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    call_status = form_data.get("CallStatus", "")
    duration = form_data.get("CallDuration", "0")
    from_number = form_data.get("From", "")
    to_number = form_data.get("To", "")

    logger.info(
        "call_status_update",
        call_sid=call_sid,
        status=call_status,
        duration=duration,
    )

    if call_status == "completed" and call_sid:
        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CallSessionRepository(session, encryptor)

                call = await repo.get_by_sid(call_sid)
                if call:
                    await repo.update_duration(call_sid, int(duration or 0))
                    # Update from/to if we have them
                    if from_number:
                        call.from_number = from_number
                    if to_number:
                        call.to_number = to_number
                    await session.commit()
        except Exception as e:
            logger.error("call_status_db_error", error=str(e))

    return PlainTextResponse("OK")


# ── Recording Status Webhook ─────────────────────────────────
@app.post("/api/webhooks/recording-status")
async def recording_status_webhook(request: Request):
    """Handle Twilio recording completion."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    recording_url = form_data.get("RecordingUrl", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_status = form_data.get("RecordingStatus", "")

    logger.info(
        "recording_status",
        call_sid=call_sid,
        recording_sid=recording_sid,
        status=recording_status,
    )

    if recording_status == "completed" and call_sid and recording_url:
        try:
            async with async_session_factory() as session:
                encryptor = get_encryptor()
                repo = CallSessionRepository(session, encryptor)
                await repo.update_recording(
                    call_sid,
                    f"{recording_url}.mp3",
                    recording_sid,
                )
                await session.commit()
        except Exception as e:
            logger.error("recording_db_error", error=str(e))

    return PlainTextResponse("OK")


class DynamicCallRequest(BaseModel):
    to_number: str = Field(..., description="Phone number to call (E.164 format)")
    agent_id: int
    agent_name: str
    agent_email: str
    agent_phone: str
    agent_npn: str
    agent_role: str
    plan_name: str
    slots: str = Field(..., description="Comma-separated slots (e.g., '2026-03-05|09:00,2026-03-05|09:30')")
    slots_count: int
    record: bool = True
    callback_url: str = Field("https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete", description="URL to POST call results when call completes")


# ── Dynamic Outbound Call (with Agent Context) ──────────────
@app.post("/api/calls/outbound-dynamic")
async def initiate_dynamic_outbound_call(req: DynamicCallRequest):
    """Initiate an outbound call with dynamic agent context and appointment slots."""
    settings = get_settings()

    # Validate phone number format
    if not req.to_number.startswith("+"):
        raise HTTPException(
            status_code=400,
            detail="Phone number must be in E.164 format (e.g., +1234567890)",
        )

    # Parse slots from comma-separated string
    slots_list = [s.strip() for s in req.slots.split(",") if s.strip()]
    
    # Create call context
    context = CallContext(
        agent_id=req.agent_id,
        agent_name=req.agent_name,
        agent_email=req.agent_email,
        agent_phone=req.agent_phone,
        agent_npn=req.agent_npn,
        agent_role=req.agent_role,
        plan_name=req.plan_name,
        slots=slots_list,
        slots_count=req.slots_count,
        callback_url=req.callback_url,
        to_number=req.to_number,
    )
    
    logger.info(
        "dynamic_outbound_call_initiated",
        agent_id=req.agent_id,
        agent_name=req.agent_name,
        to_number=req.to_number,
        available_slots=len(slots_list),
    )
    
    ws_scheme = "wss" if settings.base_url.startswith("https") else "ws"
    host = settings.base_url.replace("https://", "").replace("http://", "")
    websocket_url = f"{ws_scheme}://{host}/ws/media-stream"
    status_callback = f"{settings.base_url}/api/webhooks/call-status"

    try:
        call_sid = await make_outbound_call(
            to_number=req.to_number,
            websocket_url=websocket_url,
            status_callback_url=status_callback,
            record=req.record,
        )
        
        # Store context with the actual call_sid now that we have it
        store_call_context(call_sid, context)
        
        logger.info(
            "call_context_stored",
            call_sid=call_sid,
            agent_name=req.agent_name,
        )

        return {
            "call_sid": call_sid,
            "status": "initiated",
            "message": f"Dynamic call initiated to {req.to_number}",
            "agent_name": req.agent_name,
            "available_slots": len(slots_list),
        }
    except Exception as e:
        logger.error("dynamic_outbound_call_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")


# ── Outbound Call ────────────────────────────────────────────
@app.post("/api/calls/outbound", response_model=OutboundCallResponse)
async def initiate_outbound_call(req: OutboundCallRequest):
    """Initiate an outbound call with dynamic agent context."""
    settings = get_settings()

    # Validate phone number format
    if not req.to_number.startswith("+"):
        raise HTTPException(
            status_code=400,
            detail="Phone number must be in E.164 format (e.g., +1234567890)",
        )

    # Parse slots from comma-separated string
    slots_list = [s.strip() for s in req.slots.split(",") if s.strip()] if req.slots else []
    
    # Create call context with agent information
    context = CallContext(
        agent_id=req.agent_id,
        agent_name=req.agent_name,
        agent_email=req.agent_email,
        agent_phone=req.agent_phone,
        agent_npn=req.agent_npn,
        agent_role=req.agent_role,
        plan_name=req.plan_name,
        slots=slots_list,
        slots_count=req.slots_count,
        callback_url=req.callback_url,
        to_number=req.to_number,
    )

    logger.info(
        "outbound_call_with_context",
        agent_id=req.agent_id,
        agent_name=req.agent_name,
        to_number=req.to_number,
        available_slots=len(slots_list),
    )

    ws_scheme = "wss" if settings.base_url.startswith("https") else "ws"
    host = settings.base_url.replace("https://", "").replace("http://", "")
    websocket_url = f"{ws_scheme}://{host}/ws/media-stream"
    status_callback = f"{settings.base_url}/api/webhooks/call-status"

    try:
        call_sid = await make_outbound_call(
            to_number=req.to_number,
            websocket_url=websocket_url,
            status_callback_url=status_callback,
            record=req.record,
        )
        
        # Store context with the actual call_sid
        store_call_context(call_sid, context)
        
        logger.info(
            "call_context_stored",
            call_sid=call_sid,
            agent_name=req.agent_name,
        )

        return OutboundCallResponse(
            call_sid=call_sid,
            status="initiated",
            message=f"Call initiated to {req.to_number} on behalf of {req.agent_name}",
        )
    except Exception as e:
        logger.error("outbound_call_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")


# ── List Calls ──────────────────────────────────────────────
@app.get("/api/calls")
async def list_calls(
    limit: int=Query(20, ge=1, le=100),
    offset: int=Query(0, ge=0),
    status: str | None=Query(None),
):
    """List recent calls with optional status filter."""
    try:
        async with async_session_factory() as session:
            query = select(CallSession).order_by(desc(CallSession.created_at))

            if status:
                try:
                    status_enum = CallStatus(status)
                    query = query.where(CallSession.status == status_enum)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

            query = query.offset(offset).limit(limit)
            result = await session.execute(query)
            calls = result.scalars().all()

            return {
                "calls": [
                    {
                        "id": str(c.id),
                        "call_sid": c.twilio_call_sid,
                        "from_number": c.from_number,
                        "to_number": c.to_number,
                        "status": c.status.value,
                        "consent_status": c.consent_status.value,
                        "duration_seconds": c.call_duration_seconds,
                        "started_at": c.started_at.isoformat() if c.started_at else None,
                        "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                        "created_at": c.created_at.isoformat(),
                    }
                    for c in calls
                ],
                "total": len(calls),
                "limit": limit,
                "offset": offset,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_calls_error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to fetch calls")


# ── Get Call Details ─────────────────────────────────────────
@app.get("/api/calls/{call_sid}")
async def get_call_details(call_sid: str):
    """Get detailed information about a specific call including customer data and transcript."""
    try:
        async with async_session_factory() as session:
            encryptor = get_encryptor()

            # Get call session
            call_repo = CallSessionRepository(session, encryptor)
            call = await call_repo.get_by_sid(call_sid)

            if not call:
                raise HTTPException(status_code=404, detail="Call not found")

            # Get customer data
            customer_repo = CustomerDataRepository(session, encryptor)
            customer_data = await customer_repo.get_by_call_session(call.id)

            # Get transcript
            transcript_repo = TranscriptRepository(session)
            transcript = await transcript_repo.get_transcript(call.id)

            # Clean customer data for response (remove internal fields)
            clean_customer = None
            if customer_data:
                exclude_keys = {"id", "call_session_id", "created_at", "updated_at"}
                clean_customer = {
                    k: v for k, v in customer_data.items()
                    if k not in exclude_keys and v is not None
                }

            return {
                "id": str(call.id),
                "call_sid": call.twilio_call_sid,
                "from_number": call.from_number,
                "to_number": call.to_number,
                "status": call.status.value,
                "consent_status": call.consent_status.value,
                "consent_timestamp": (
                    call.consent_timestamp.isoformat() if call.consent_timestamp else None
                ),
                "duration_seconds": call.call_duration_seconds,
                "recording_url": call.call_recording_url,
                "started_at": call.started_at.isoformat() if call.started_at else None,
                "ended_at": call.ended_at.isoformat() if call.ended_at else None,
                "customer_data": clean_customer,
                "transcript": transcript,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_call_error", error=str(e), call_sid=call_sid)
        raise HTTPException(status_code=500, detail="Failed to fetch call details")
