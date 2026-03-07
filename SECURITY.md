# Security Documentation

## Overview
This document outlines the security measures implemented in the AI Calling Agent application.

## Security Features

### 1. API Authentication
All sensitive endpoints require API key authentication via Bearer token.

**Protected Endpoints:**
- `POST /api/calls/outbound` - Initiate outbound calls
- `POST /api/calls/outbound-dynamic` - Initiate dynamic outbound calls

**Usage:**
```bash
curl -X POST https://ai-calling-agent-wxdz.onrender.com/api/calls/outbound \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+1234567890",
    "agent_name": "John Doe"
  }'
```

**Setting API Key:**
Set `API_KEY` in your environment variables or `.env` file:
```env
API_KEY=api_FnS2cTWkmtomPcLrH2g_ysGy-wTInKy6S272lswZk8M
```

### 2. Rate Limiting
Rate limits protect against API abuse and DoS attacks.

**Limits:**
- Outbound calls: 10 requests/minute per IP
- General endpoints: Configurable via SlowAPI

**Rate Limit Response:**
```json
{
  "error": "Rate limit exceeded: 10 per 1 minute"
}
```

### 3. CORS Policy
Cross-Origin Resource Sharing is restricted to allowed domains only.

**Configuration:**
```env
ALLOWED_ORIGINS=https://yourdomain.com,https://admin.yourdomain.com
```

**Default:** All origins (`*`) - **Change this in production!**

### 4. Field-Level Encryption
Sensitive customer data is encrypted at rest in the database.

**Encrypted Fields:**
- `email`
- `phone_number`

**Algorithm:** Fernet (AES-128 in CBC mode)

**Key Generation:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Update `.env`:
```env
ENCRYPTION_KEY=your-generated-key-here
```

### 5. Twilio Webhook Signature Verification
All Twilio webhooks verify request authenticity using HMAC-SHA1 signatures.

**Verified Webhooks:**
- `/api/webhooks/twilio-voice`
- `/api/webhooks/call-status`
- `/api/webhooks/recording-status`

**How it works:**
1. Twilio signs each request with your Auth Token
2. Application validates signature using `X-Twilio-Signature` header
3. Invalid signatures are logged with warnings
4. Requests still process for backward compatibility

**Strict Mode (Recommended):**
To enforce strict validation (reject invalid signatures), modify `app/main.py`:
```python
is_valid = await verify_twilio_signature(request)
if not is_valid:
    raise HTTPException(status_code=403, detail="Invalid Twilio signature")
```

### 6. Input Validation
Phone numbers and other inputs are strictly validated.

**Phone Number Validation:**
- Must be in E.164 format (`+1234567890`)
- Validated using `phonenumbers` library
- Checks country code, number length, and validity

**Example:**
```python
from app.security import validate_phone_number_strict

is_valid, error = validate_phone_number_strict("+1234567890")
if not is_valid:
    print(f"Invalid: {error}")
```

### 7. Debug Endpoints Protection
Debug endpoints are automatically disabled in production.

**Debug Endpoints:**
- `/api/debug/twiml` - View generated TwiML
- `/api/debug/contexts` - View stored call contexts
- `/api/debug/dynamic-flow` - View dynamic questions

**Behavior:**
- `APP_ENV=production` → Returns 404 Not Found
- `APP_ENV=development` → Returns debug data

### 8. Secure Storage
All secrets are stored securely and never committed to version control.

**Environment Variables:**
```env
# Never commit these!
OPENAI_API_KEY=sk-proj-...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
SECRET_KEY=...
ENCRYPTION_KEY=...
API_KEY=...
```

**Secret Management:**
- Use Render's environment variables (production)
- Use `.env` file (development, gitignored)
- Rotate credentials regularly

## Security Best Practices

### For Production Deployment

1. **Rotate All Credentials**
   ```bash
   # Generate new API key
   python -c "import secrets; print('api_' + secrets.token_urlsafe(32))"
   
   # Generate new encryption key
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   
   # Generate new secret key
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Update Environment Variables**
   - OpenAI: https://platform.openai.com/api-keys
   - Twilio: https://console.twilio.com/
   - Update all keys in Render dashboard

3. **Restrict CORS Origins**
   ```env
   ALLOWED_ORIGINS=https://yourdomain.com,https://api.yourdomain.com
   ```

4. **Enable Strict Webhook Validation**
   Modify webhooks to reject invalid signatures (see section 5 above)

5. **Monitor Logs**
   ```bash
   # Check for security warnings
   grep "invalid_api_key_attempt" logs/app.log
   grep "invalid_twilio_signature" logs/app.log
   ```

6. **Set Strong Database Credentials**
   ```env
   DATABASE_URL=postgresql+asyncpg://user:STRONG_PASSWORD@host:port/db
   ```

## Vulnerability Reporting

If you discover a security vulnerability, please email security@yourdomain.com

**Please include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Security Checklist

Before going to production:

- [ ] All API keys rotated from defaults
- [ ] `API_KEY` set and enforced on sensitive endpoints
- [ ] `ALLOWED_ORIGINS` restricted to your domains
- [ ] `ENCRYPTION_KEY` generated cryptographically
- [ ] Twilio webhook signature validation enabled
- [ ] Debug endpoints disabled (`APP_ENV=production`)
- [ ] Database password is strong and unique
- [ ] `.env` file not committed to version control
- [ ] All secrets stored in Render environment variables
- [ ] Logging configured to not expose secrets
- [ ] Rate limiting enabled and tested

## Additional Security Measures

### Future Enhancements

1. **IP Whitelisting**
   - Restrict API access to known IPs
   - Configure in Render or via middleware

2. **Request Signing**
   - Implement HMAC request signing for Express backend
   - Verify signatures on incoming requests

3. **Audit Logging**
   - Log all API access attempts
   - Track failed authentication attempts
   - Monitor for suspicious patterns

4. **OAuth 2.0 / JWT**
   - Replace API keys with JWT tokens
   - Implement token expiration and refresh
   - Support multiple API consumers

5. **WAF (Web Application Firewall)**
   - Use Cloudflare or similar
   - DDoS protection
   - Bot mitigation

## Contact

For security questions or concerns:
- Email: security@yourdomain.com
- GitHub Issues: (for non-security bugs only)

---

**Last Updated:** March 7, 2026
**Version:** 1.0.0
