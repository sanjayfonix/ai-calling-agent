"""
Security utilities - API authentication, Twilio webhook verification, input validation.
"""

from __future__ import annotations

from typing import Optional
import phonenumbers
from fastapi import HTTPException, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from twilio.request_validator import RequestValidator
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

# HTTP Bearer token security
security = HTTPBearer()


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security)
) -> str:
    """
    Verify API key from Authorization header.
    
    Usage:
        @app.post("/api/calls/outbound")
        async def endpoint(api_key: str = Depends(verify_api_key)):
            # Protected endpoint
    
    Raises:
        HTTPException: 401 if API key is invalid
    """
    settings = get_settings()
    
    if not settings.api_key:
        # If no API key is configured, allow access (for backward compatibility)
        logger.warning("no_api_key_configured_allowing_access")
        return ""
    
    if credentials.credentials != settings.api_key:
        logger.warning(
            "invalid_api_key_attempt",
            provided_key_prefix=credentials.credentials[:10] + "..." if len(credentials.credentials) > 10 else "???"
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )
    
    return credentials.credentials


async def verify_twilio_signature(request: Request) -> bool:
    """
    Verify that a webhook request actually came from Twilio.
    
    Usage:
        @app.post("/api/webhooks/twilio-voice")
        async def webhook(request: Request):
            if not await verify_twilio_signature(request):
                raise HTTPException(status_code=403, detail="Invalid signature")
    
    Returns:
        bool: True if signature is valid, False otherwise
    """
    settings = get_settings()
    validator = RequestValidator(settings.twilio_auth_token)
    
    # Get the Twilio signature from the header
    signature = request.headers.get("X-Twilio-Signature", "")
    
    # Get the full URL (Twilio signs the full URL including query params)
    url = str(request.url)
    
    # Get form data from the request
    try:
        form_data = await request.form()
        params = dict(form_data)
    except:
        # If not form data, try JSON
        try:
            params = await request.json()
        except:
            params = {}
    
    # Validate the signature
    is_valid = validator.validate(url, params, signature)
    
    if not is_valid:
        logger.warning(
            "invalid_twilio_signature",
            url=url,
            has_signature=bool(signature)
        )
    
    return is_valid


def validate_phone_number(phone: str) -> bool:
    """
    Validate phone number using phonenumbers library.
    
    Args:
        phone: Phone number in E.164 format (e.g., +1234567890)
    
    Returns:
        bool: True if valid, False otherwise
    """
    try:
        parsed = phonenumbers.parse(phone, None)
        return phonenumbers.is_valid_number(parsed)
    except:
        return False


def validate_phone_number_strict(phone: str) -> tuple[bool, str]:
    """
    Strict phone number validation with error message.
    
    Args:
        phone: Phone number to validate
    
    Returns:
        tuple: (is_valid, error_message)
    """
    if not phone:
        return False, "Phone number is required"
    
    if not phone.startswith("+"):
        return False, "Phone number must start with '+' and country code (E.164 format)"
    
    try:
        parsed = phonenumbers.parse(phone, None)
        
        if not phonenumbers.is_valid_number(parsed):
            return False, "Invalid phone number"
        
        # Additional checks
        number_type = phonenumbers.number_type(parsed)
        if number_type == phonenumbers.PhoneNumberType.UNKNOWN:
            return False, "Unknown phone number type"
        
        return True, ""
    
    except phonenumbers.phonenumberutil.NumberParseException as e:
        return False, f"Invalid phone number format: {str(e)}"
    except Exception as e:
        return False, f"Phone validation error: {str(e)}"


def sanitize_string(value: str, max_length: int = 500) -> str:
    """
    Sanitize string input by removing potentially dangerous characters.
    
    Args:
        value: String to sanitize
        max_length: Maximum allowed length
    
    Returns:
        str: Sanitized string
    """
    if not value:
        return ""
    
    # Truncate to max length
    value = value[:max_length]
    
    # Remove null bytes
    value = value.replace("\x00", "")
    
    # Strip leading/trailing whitespace
    value = value.strip()
    
    return value
