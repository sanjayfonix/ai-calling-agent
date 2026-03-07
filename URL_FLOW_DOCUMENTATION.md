# URL Flow Documentation

## Method 1: Direct Outbound Call (`/api/calls/outbound`)

### Step-by-Step URL Hits:

1. **Your System → Render API**
   ```
   POST https://ai-calling-agent-wxdz.onrender.com/api/calls/outbound
   Body: {agent_id, agent_name, agent_email, ...agent data..., to_number}
   ```
   - Agent data: ✅ Sent in request body
   - Response: `{call_sid, status, message}`

2. **Render → Twilio API**
   ```
   POST https://api.twilio.com/...
   Body: {from, to, twiml: "<Response><Connect><Stream.../></Stream></Connect></Response>"}
   ```
   - Twilio initiates call with embedded TwiML

3. **Twilio → Render WebSocket**
   ```
   WSS wss://ai-calling-agent-wxdz.onrender.com/ws/media-stream
   ```
   - Audio streaming starts

4. **CallManager → Backend API (when call starts)**
   ```
   GET https://xd363v4j-5000.inc1.devtunnels.ms/api/v1/admin/ai-collection-flows/getActiveAiCollectionFlows
   ```
   - ✅ Fetches dynamic questions

5. **CallManager → Backend API (when call ends)**
   ```
   POST https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete
   Body: {
     call_sid,
     agent_context: {agent_id, agent_name, ...},
     customer_data: {full_name, email, ...},
     transcript: [...]
   }
   ```
   - ✅ Sends all collected data

---

## Method 2: Twilio Voice Webhook (`/twilio/voice`)

### Step-by-Step URL Hits:

1. **Your Express Backend → Twilio API**
   ```
   POST https://api.twilio.com/...
   Body: {
     from, 
     to, 
     url: "https://ai-calling-agent-wxdz.onrender.com/twilio/voice?agent_id=8&agent_name=Himanshu+Mathis&..."
   }
   ```
   - Agent data: ✅ Passed as URL query parameters
   - Twilio will fetch TwiML from this URL

2. **Twilio → Render API (to get TwiML)**
   ```
   GET https://ai-calling-agent-wxdz.onrender.com/twilio/voice?agent_id=8&agent_name=...&slots=...
   ```
   - ❌ Does NOT fetch agent data from elsewhere
   - ✅ Receives agent data as query parameters
   - Stores agent context temporarily
   - Returns TwiML: `<Response><Connect><Stream url="wss://...?context_id=temp_123"/></Connect></Response>`

3. **Twilio → Render WebSocket**
   ```
   WSS wss://ai-calling-agent-wxdz.onrender.com/ws/media-stream?context_id=temp_abc123
   ```
   - Audio streaming starts
   - Context ID maps temp agent data to real call_sid

4. **CallManager → Backend API (when call starts)**
   ```
   GET https://xd363v4j-5000.inc1.devtunnels.ms/api/v1/admin/ai-collection-flows/getActiveAiCollectionFlows
   ```
   - ✅ Fetches dynamic questions

5. **CallManager → Backend API (when call ends)**
   ```
   POST https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete
   Body: {
     call_sid,
     agent_context: {agent_id, agent_name, ...},
     customer_data: {full_name, email, ...},
     transcript: [...]
   }
   ```
   - ✅ Sends all collected data

---

## Summary Table

| Step | Method 1 | Method 2 |
|------|----------|----------|
| **Agent Data Source** | POST body to `/api/calls/outbound` | Query params to `/twilio/voice` |
| **Who Calls Twilio** | Render (Python) | Express Backend |
| **TwiML Delivery** | Embedded in Twilio API call | Fetched by Twilio via GET |
| **Questions API** | ✅ Fetched on call start | ✅ Fetched on call start |
| **Webhook API** | ✅ Sent on call end | ✅ Sent on call end |

---

## Important Notes

### URLs That ARE Hit:
✅ **Dynamic Questions**: `https://xd363v4j-5000.inc1.devtunnels.ms/api/v1/admin/ai-collection-flows/getActiveAiCollectionFlows`
   - Fetched: Once per call (cached 5 min)
   - When: Call starts
   - Contains: Question list

✅ **Call Complete Webhook**: `https://xd363v4j-5000.inc1.devtunnels.ms/api/ai-call/call-complete`
   - Sent: Once per call
   - When: Call ends
   - Contains: Agent context + Customer data + Transcript

### URLs That Are NOT Hit:
❌ **Agent Data**: Never fetched from a URL
   - Method 1: Sent in request body
   - Method 2: Sent as query parameters
   - Both: Stored in memory during call

### Context Flow (Method 2):
```
Express Backend passes agent data → /twilio/voice receives it → Stores as temp_abc123
                                                                           ↓
Twilio connects WebSocket with context_id=temp_abc123 → Maps to real call_sid
                                                                           ↓
                                           Agent context used throughout call
```

---

## Testing

**Method 1**:
```powershell
$body = @{to_number="+918949968414"; agent_id=8; agent_name="Himanshu Mathis"; ...} | ConvertTo-Json
Invoke-RestMethod -Uri "https://ai-calling-agent-wxdz.onrender.com/api/calls/outbound" -Method Post -Body $body -ContentType "application/json"
```

**Method 2**:
```python
python test_twilio_method2.py
```
