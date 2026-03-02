# AI Calling Agent

**Production-ready real-time AI voice calling agent** using OpenAI Realtime API and Twilio.

Sub-second latency, natural voice, consent handling, structured Q&A, and encrypted data storage.

---

## Architecture

```
Customer Phone
       │
       ▼
Twilio (Phone Number + Media Streams)
       │
       ▼ WebSocket (G.711 μ-law audio)
       │
FastAPI Backend (WebSocket bridge)
       │
       ▼ WebSocket (bidirectional audio)
       │
OpenAI Realtime API (GPT-4o Realtime)
       │
       ▼ Function Calls
       │
PostgreSQL (encrypted data storage)
```

### Why This Architecture?

| Approach | Latency | Quality |
|----------|---------|---------|
| Whisper → GPT-4 → ElevenLabs (3-step) | 2-5 seconds | Good |
| **OpenAI Realtime API (this project)** | **< 500ms** | **Excellent** |

OpenAI Realtime API handles speech-to-speech in a single streaming WebSocket — no separate STT/TTS steps.

---

## Features

- **Ultra-low latency** — streaming audio, single WebSocket, no STT→LLM→TTS chain
- **Natural voice** — OpenAI's native voice models with interruption handling
- **Consent capture** — MANDATORY recording consent before any data collection
- **Structured Q&A** — collects 12+ fields in natural conversation
- **Function calling** — AI decides when to save data, record consent, end call
- **ACA explanation** — optional health insurance education
- **Encrypted storage** — sensitive fields (email, doctor, medicines) encrypted at rest
- **Call recording** — Twilio recording with URL stored in DB
- **Full transcript** — every utterance stored for audit
- **Outbound calls** — API endpoint to initiate calls
- **Inbound calls** — webhook handles incoming calls
- **Health monitoring** — health check endpoint with active call count
- **Docker deployment** — one-command deployment with docker-compose

---

## Call Flow

```
1. Call connects → Twilio Media Stream → WebSocket → FastAPI
2. FastAPI bridges to OpenAI Realtime API
3. AI says: "Hi, this call may be recorded... Do I have your consent?"
4. If NO  → Polite goodbye → end_call function → hang up
5. If YES → record_consent function → proceed
6. AI collects: Name → Email → Age → Zip → State → Country
                → Insurance status → Life events → Doctor → Medicines
                → Preferred time
7. AI offers ACA explanation (if wanted)
8. AI summarizes → save_customer_data function → goodbye → end_call
```

---

## Project Structure

```
aicallingagent/
├── app/
│   ├── __init__.py              # Package init
│   ├── __main__.py              # Entry point (python -m app)
│   ├── main.py                  # FastAPI app, all routes
│   ├── config.py                # Settings from .env
│   ├── database.py              # Async SQLAlchemy engine
│   ├── models.py                # ORM models (CallSession, CustomerData, Transcript)
│   ├── repository.py            # Database CRUD operations
│   ├── encryption.py            # Fernet field-level encryption
│   ├── prompts.py               # System prompt + function definitions
│   ├── openai_realtime.py       # OpenAI Realtime WebSocket client
│   ├── twilio_handler.py        # Twilio Media Stream handler
│   ├── twilio_service.py        # Twilio REST helpers (outbound calls, TwiML)
│   ├── call_manager.py          # Central orchestrator (bridges everything)
│   └── logging_config.py        # Structured logging setup
├── alembic/
│   ├── env.py                   # Alembic async config
│   ├── script.py.mako           # Migration template
│   └── versions/                # Migration files
├── deploy/
│   ├── nginx.conf               # Nginx reverse proxy config
│   └── aicallingagent.service   # Systemd service file
├── tests/
│   ├── test_encryption.py       # Encryption tests
│   ├── test_prompts.py          # Prompt/tool definition tests
│   └── test_api.py              # API endpoint tests
├── .env.example                 # Environment template
├── .gitignore
├── alembic.ini                  # Alembic config
├── docker-compose.yml           # Docker compose (app + PostgreSQL)
├── Dockerfile                   # Production Docker image
├── pyproject.toml               # Project metadata
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ (or Docker)
- OpenAI API key with Realtime API access
- Twilio account with:
  - A phone number (Voice-enabled)
  - Media Streams enabled
- A public domain with SSL (for Twilio webhooks)

### 1. Clone and Install

```bash
git clone <your-repo-url>
cd aicallingagent

# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (Linux/Mac)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
OPENAI_API_KEY=sk-your-key-here
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_PHONE_NUMBER=+1234567890
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/aicallingagent
BASE_URL=https://yourdomain.com
ENCRYPTION_KEY=your-32-char-encryption-key-here
```

### 3. Setup Database

**Option A: Docker (recommended)**
```bash
docker-compose up -d db
```

**Option B: Local PostgreSQL**
```bash
createdb aicallingagent
```

Run migrations:
```bash
alembic revision --autogenerate -m "Initial tables"
alembic upgrade head
```

Or let the app create tables on startup (development only).

### 4. Configure Twilio

1. Go to [Twilio Console](https://console.twilio.com)
2. Buy a phone number (or use existing)
3. Under your phone number → **Voice Configuration**:
   - **When a call comes in**: Webhook
   - **URL**: `https://yourdomain.com/api/webhooks/twilio-voice`
   - **Method**: `POST`
4. Enable **Media Streams** on your account

### 5. Run the Application

**Development:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Production:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 16777216 --timeout-keep-alive 120
```

**With Docker:**
```bash
docker-compose up -d
```

### 6. Test with ngrok (Development)

If you don't have a public domain yet:

```bash
ngrok http 8000
```

Use the ngrok URL as your `BASE_URL` and Twilio webhook URL.

---

## API Endpoints

### Health Check
```
GET /api/health
```
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "active_calls": 0,
  "timestamp": "2026-03-02T10:00:00Z"
}
```

### Initiate Outbound Call
```
POST /api/calls/outbound
Content-Type: application/json

{
  "to_number": "+1234567890",
  "record": true
}
```
```json
{
  "call_sid": "CAxxxxxxxxxxxxxxxx",
  "status": "initiated",
  "message": "Call initiated to +1234567890"
}
```

### List Calls
```
GET /api/calls?limit=20&offset=0&status=completed
```

### Get Call Details
```
GET /api/calls/{call_sid}
```
Returns: call info, customer data (decrypted), full transcript.

### Webhooks (Twilio → Your Server)
| Endpoint | Purpose |
|----------|---------|
| `POST /api/webhooks/twilio-voice` | Inbound call → returns TwiML for Media Stream |
| `WS /ws/media-stream` | Bidirectional audio streaming |
| `POST /api/webhooks/call-status` | Call status updates |
| `POST /api/webhooks/recording-status` | Recording completion |

---

## Data Storage

### Tables

**call_sessions** — tracks each phone call
| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| twilio_call_sid | String | Unique, indexed |
| status | Enum | initiated/in_progress/completed/failed/no_consent/timeout |
| consent_status | Enum | pending/granted/denied |
| call_recording_url | Text | Twilio recording URL |

**customer_data** — structured info from conversation
| Field | Type | Encrypted? |
|-------|------|-----------|
| full_name | Text | No |
| email | Text | **Yes** |
| age | Integer | No |
| zipcode | String | No |
| state | String | No |
| country | String | No |
| currently_insured | Boolean | No |
| life_event | Text | No |
| doctor_name | Text | **Yes** |
| doctor_specialty | Text | No |
| medicines | Text | **Yes** |
| preferred_time_slot | Text | No |

**call_transcripts** — full conversation log
| Field | Type |
|-------|------|
| role | String (agent/customer) |
| content | Text |
| timestamp | DateTime |

---

## Security & Compliance

### Encryption
- Sensitive fields encrypted at rest using Fernet (AES-128-CBC)
- Encryption key in environment variable (never in code)

### Consent
- Call recording consent captured BEFORE any data collection
- Consent status + timestamp stored in database
- Call automatically ends if consent is denied

### HIPAA Considerations
If handling Protected Health Information (PHI):
- Use encrypted database connections (SSL)
- Enable audit logging
- Sign BAA with OpenAI and Twilio
- Use HIPAA-eligible hosting
- Implement access controls

### TCPA Compliance
- Obtain consent before calling
- Honor do-not-call requests
- Identify yourself at call start
- Call during permitted hours only

---

## Production Deployment

### With Docker Compose
```bash
# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f app

# Stop
docker-compose down
```

### With Systemd (VPS)
```bash
# Copy service file
sudo cp deploy/aicallingagent.service /etc/systemd/system/

# Copy nginx config
sudo cp deploy/nginx.conf /etc/nginx/sites-available/aicallingagent
sudo ln -s /etc/nginx/sites-available/aicallingagent /etc/nginx/sites-enabled/

# Get SSL certificate
sudo certbot --nginx -d yourdomain.com

# Start services
sudo systemctl daemon-reload
sudo systemctl enable aicallingagent
sudo systemctl start aicallingagent
sudo systemctl restart nginx
```

### SSL Certificate (Required)
Twilio requires HTTPS for webhooks and WSS for WebSockets:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Twilio 11200 error | Check webhook URL is reachable over HTTPS |
| No audio from AI | Verify OpenAI API key has Realtime access |
| High latency | Ensure WebSocket (not HTTP) for audio; check server location |
| Call drops immediately | Check Twilio phone number has Voice enabled |
| Database connection error | Verify `DATABASE_URL` and PostgreSQL is running |
| WebSocket disconnects | Check nginx `proxy_read_timeout` is high enough (~3600s) |
| Recording not saved | Check recording webhook URL is correct |

### Useful Commands
```bash
# Check logs
tail -f logs/app.log | python -m json.tool

# Test health
curl https://yourdomain.com/api/health

# Make test outbound call
curl -X POST https://yourdomain.com/api/calls/outbound \
  -H "Content-Type: application/json" \
  -d '{"to_number": "+1234567890"}'

# Check active calls
curl https://yourdomain.com/api/calls?status=in_progress
```

---

## Cost Estimates

| Service | Approximate Cost |
|---------|-----------------|
| Twilio phone number | $1/month |
| Twilio voice (per minute) | $0.013 inbound, $0.014 outbound |
| Twilio recording storage | $0.0025/min |
| OpenAI Realtime API | ~$0.06/min (audio input) + ~$0.24/min (audio output) |
| PostgreSQL (managed) | $5-15/month |
| VPS hosting | $5-20/month |

**Average cost per 5-minute call: ~$1.50-2.00**

---

## License

Proprietary — all rights reserved.
